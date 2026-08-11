from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath, PurePosixPath
from typing import BinaryIO

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from audio_server.api.schemas import ClientRecordingMetadata
from audio_server.db.models import (
    Analysis,
    JobStage,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
    TranscriptSegment,
)
from audio_server.processing.contracts import AudioPreprocessor, AudioProbe
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)
from audio_server.services.storage import (
    StagedUpload,
    StorageBackend,
    StorageConflictError,
    UploadTooLargeError,
    original_storage_key,
)

logger = logging.getLogger(__name__)


class RecordingServiceError(Exception):
    def __init__(self, code: str, safe_message: str, *, status_code: int) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code


@dataclass(frozen=True)
class IngestResult:
    recording: Recording
    duplicate: bool


class RecordingService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: StorageBackend,
        audio_preprocessor: AudioPreprocessor,
        max_upload_bytes: int,
        max_metadata_bytes: int,
        max_audio_duration_seconds: float,
        max_attempts: int,
        audio_retention_days: int | None = None,
        transcript_retention_days: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._audio = audio_preprocessor
        self._max_upload_bytes = max_upload_bytes
        self._max_metadata_bytes = max_metadata_bytes
        self._max_audio_duration_seconds = max_audio_duration_seconds
        self._max_attempts = max_attempts
        self._audio_retention_days = audio_retention_days
        self._transcript_retention_days = transcript_retention_days

    @property
    def max_metadata_bytes(self) -> int:
        return self._max_metadata_bytes

    def ingest(
        self,
        *,
        source: BinaryIO,
        metadata: ClientRecordingMetadata,
        idempotency_key: uuid.UUID,
        header_device_id: str,
        header_sha256: str,
        form_checksum: str,
    ) -> IngestResult:
        expected_checksum = self._validate_identity(
            metadata=metadata,
            idempotency_key=idempotency_key,
            header_device_id=header_device_id,
            header_sha256=header_sha256,
            form_checksum=form_checksum,
        )

        try:
            staged = self._storage.create_staged_upload(source, max_bytes=self._max_upload_bytes)
        except UploadTooLargeError as exc:
            raise RecordingServiceError(
                "upload_too_large",
                "Uploaded audio exceeds the configured size limit.",
                status_code=413,
            ) from exc

        final_key: str | None = None
        try:
            self._validate_staged_identity(staged, metadata, expected_checksum)
            probe = self._probe(staged)
            final_key = original_storage_key(
                metadata.id, metadata.recording_start_time, probe.preferred_extension
            )

            with self._session_factory() as session:
                duplicate = self._find_duplicate(session, metadata)
                if duplicate is not None:
                    self._preserve_duplicate_upload(staged, duplicate)
                    logger.info(
                        "recording upload deduplicated",
                        extra={"recording_id": str(duplicate.id)},
                    )
                    return IngestResult(recording=duplicate, duplicate=True)

            self._storage.put_original(staged, final_key)
            result = self._create_recording_and_job(metadata, probe, final_key)
            logger.info(
                "recording uploaded",
                extra={"recording_id": str(result.recording.id)},
            )
            return result
        except RecordingServiceError:
            self._storage.discard_staged(staged)
            raise
        except StorageConflictError as exc:
            self._storage.discard_staged(staged)
            raise RecordingServiceError(
                "storage_identity_conflict",
                "Stored audio conflicts with the uploaded recording identity.",
                status_code=409,
            ) from exc
        except Exception:
            self._storage.discard_staged(staged)
            raise

    def list_recordings(
        self,
        *,
        limit: int,
        offset: int,
        device_id: str | None,
        status: RecordingStatus | None,
    ) -> list[Recording]:
        statement: Select[tuple[Recording]] = select(Recording)
        if device_id is not None:
            statement = statement.where(Recording.device_id == device_id)
        if status is not None:
            statement = statement.where(Recording.processing_status == status)
        statement = statement.order_by(Recording.created_at.desc(), Recording.id.desc())
        statement = statement.limit(limit).offset(offset)
        with self._session_factory() as session:
            return list(session.scalars(statement))

    def get_recording(self, recording_id: uuid.UUID) -> Recording:
        with self._session_factory() as session:
            recording = session.get(Recording, recording_id)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            return recording

    def get_status(self, recording_id: uuid.UUID) -> tuple[Recording, ProcessingJob | None]:
        with self._session_factory() as session:
            recording = session.get(Recording, recording_id)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            job = session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.recording_id == recording_id)
                .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                .limit(1)
            )
            return recording, job

    def get_transcript(self, recording_id: uuid.UUID) -> tuple[Recording, list[TranscriptSegment]]:
        with self._session_factory() as session:
            recording = session.get(Recording, recording_id)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            segments = list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.recording_id == recording_id)
                    .order_by(TranscriptSegment.sequence)
                )
            )
            if not segments and recording.processing_status is not RecordingStatus.COMPLETED:
                raise RecordingServiceError(
                    "transcript_not_ready",
                    "Transcript is not available yet.",
                    status_code=409,
                )
            return recording, segments

    def get_analysis(self, recording_id: uuid.UUID) -> Analysis:
        with self._session_factory() as session:
            if session.get(Recording, recording_id) is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            analysis = session.scalar(
                select(Analysis)
                .where(Analysis.recording_id == recording_id)
                .order_by(Analysis.created_at.desc(), Analysis.id.desc())
                .limit(1)
            )
            if analysis is None:
                raise RecordingServiceError(
                    "analysis_not_ready", "Analysis is not available yet.", status_code=409
                )
            return analysis

    def retry(self, recording_id: uuid.UUID) -> ProcessingJob:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            recording = session.get(Recording, recording_id, with_for_update=True)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            if recording.processing_status is not RecordingStatus.FAILED:
                raise RecordingServiceError(
                    "recording_not_retryable",
                    "Only a failed recording can be retried.",
                    status_code=409,
                )
            active = session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.recording_id == recording_id,
                    ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]),
                )
            )
            if active is not None:
                raise RecordingServiceError(
                    "job_already_active",
                    "A processing job is already active for this recording.",
                    status_code=409,
                )
            job = ProcessingJob(
                recording_id=recording.id,
                status=JobStatus.QUEUED,
                stage=JobStage.QUEUED,
                max_attempts=self._max_attempts,
                available_at=now,
            )
            recording.processing_status = RecordingStatus.QUEUED
            session.add(job)
            session.flush()
            logger.info(
                "processing retry queued",
                extra={"recording_id": str(recording.id), "job_id": str(job.id)},
            )
            return job

    def _validate_identity(
        self,
        *,
        metadata: ClientRecordingMetadata,
        idempotency_key: uuid.UUID,
        header_device_id: str,
        header_sha256: str,
        form_checksum: str,
    ) -> str:
        if metadata.id != idempotency_key:
            raise RecordingServiceError(
                "idempotency_key_mismatch",
                "Idempotency-Key does not match metadata.id.",
                status_code=422,
            )
        if metadata.device_id != header_device_id:
            raise RecordingServiceError(
                "device_id_mismatch",
                "X-Device-ID does not match metadata.device_id.",
                status_code=422,
            )
        checksums = {metadata.checksum_sha256, header_sha256.lower(), form_checksum.lower()}
        if len(checksums) != 1 or any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in checksums
        ):
            raise RecordingServiceError(
                "checksum_claim_mismatch",
                "Checksum values supplied with the upload do not match.",
                status_code=422,
            )
        return metadata.checksum_sha256

    @staticmethod
    def _validate_staged_identity(
        staged: StagedUpload, metadata: ClientRecordingMetadata, expected_checksum: str
    ) -> None:
        if staged.file_size != metadata.file_size:
            raise RecordingServiceError(
                "file_size_mismatch",
                "Uploaded audio size does not match metadata.file_size.",
                status_code=422,
            )
        if staged.sha256 != expected_checksum:
            raise RecordingServiceError(
                "checksum_mismatch",
                "Uploaded audio checksum does not match.",
                status_code=422,
            )

    def _probe(self, staged: StagedUpload) -> AudioProbe:
        try:
            probe = self._audio.probe(staged.path)
        except ProviderConfigurationError as exc:
            raise RecordingServiceError(
                exc.code,
                "Audio validation is unavailable because the server is not configured correctly.",
                status_code=503,
            ) from exc
        except RetryableProcessingError as exc:
            raise RecordingServiceError(
                exc.code,
                "Audio validation is temporarily unavailable.",
                status_code=503,
            ) from exc
        except PermanentProcessingError as exc:
            raise RecordingServiceError(exc.code, exc.safe_message, status_code=415) from exc
        except Exception as exc:
            raise RecordingServiceError(
                "audio_validation_unavailable",
                "Audio validation could not be completed.",
                status_code=500,
            ) from exc
        if probe.duration_seconds > self._max_audio_duration_seconds:
            raise RecordingServiceError(
                "audio_too_long",
                "Uploaded audio exceeds the configured duration limit.",
                status_code=413,
            )
        return probe

    @staticmethod
    def _find_duplicate(session: Session, metadata: ClientRecordingMetadata) -> Recording | None:
        by_id = session.get(Recording, metadata.id)
        if by_id is not None:
            if (
                by_id.device_id != metadata.device_id
                or by_id.sha256 != metadata.checksum_sha256
                or by_id.file_size != metadata.file_size
            ):
                raise RecordingServiceError(
                    "recording_identity_conflict",
                    "Recording ID is already associated with different audio.",
                    status_code=409,
                )
            return by_id
        return session.scalar(
            select(Recording).where(
                Recording.device_id == metadata.device_id,
                Recording.sha256 == metadata.checksum_sha256,
            )
        )

    def _preserve_duplicate_upload(self, staged: StagedUpload, recording: Recording) -> None:
        self._storage.put_original(staged, recording.storage_key)

    def _create_recording_and_job(
        self, metadata: ClientRecordingMetadata, probe: AudioProbe, storage_key: str
    ) -> IngestResult:
        now = datetime.now(UTC)
        recording = Recording(
            id=metadata.id,
            device_id=metadata.device_id,
            original_filename=_safe_filename(metadata.filename),
            storage_key=storage_key,
            mime_type=probe.mime_type,
            audio_format=probe.preferred_extension.lstrip("."),
            file_size=metadata.file_size,
            sha256=metadata.checksum_sha256,
            started_at=metadata.recording_start_time,
            ended_at=metadata.recording_end_time,
            duration_seconds=probe.duration_seconds,
            sample_rate=probe.sample_rate,
            channels=probe.channels,
            client_metadata={
                "claimed_duration_seconds": metadata.duration_seconds,
                "claimed_audio_format": metadata.audio_format,
                "claimed_sample_rate": metadata.sample_rate,
                "claimed_channels": metadata.channels,
                "extra": metadata.extra,
            },
            processing_status=RecordingStatus.QUEUED,
            audio_delete_after=_retention_date(now, self._audio_retention_days),
            transcript_delete_after=_retention_date(now, self._transcript_retention_days),
        )
        job = ProcessingJob(
            recording_id=recording.id,
            status=JobStatus.QUEUED,
            stage=JobStage.QUEUED,
            max_attempts=self._max_attempts,
            available_at=now,
        )
        try:
            with self._session_factory.begin() as session:
                session.add_all([recording, job])
                session.flush()
        except IntegrityError:
            with self._session_factory() as session:
                try:
                    duplicate = self._find_duplicate(session, metadata)
                except RecordingServiceError:
                    self._delete_if_unreferenced(session, storage_key)
                    raise
                if duplicate is not None:
                    if duplicate.storage_key != storage_key:
                        self._delete_if_unreferenced(session, storage_key)
                    return IngestResult(recording=duplicate, duplicate=True)
                self._delete_if_unreferenced(session, storage_key)
            raise
        logger.info(
            "processing job queued",
            extra={"recording_id": str(recording.id), "job_id": str(job.id)},
        )
        return IngestResult(recording=recording, duplicate=False)

    def _delete_if_unreferenced(self, session: Session, storage_key: str) -> None:
        owner_id = session.scalar(select(Recording.id).where(Recording.storage_key == storage_key))
        if owner_id is None:
            self._storage.delete(storage_key)


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    basename = PurePosixPath(normalized).name or PurePath(normalized).name
    cleaned = "".join(character for character in basename if character.isprintable())
    return cleaned[:255] or "audio"


def _retention_date(now: datetime, days: int | None) -> datetime | None:
    return None if days is None else now + timedelta(days=days)
