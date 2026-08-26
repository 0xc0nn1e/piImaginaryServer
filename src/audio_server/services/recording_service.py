from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath, PurePosixPath
from typing import BinaryIO

from sqlalchemy import Select, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from audio_server.activity.repository import append_activity
from audio_server.api.schemas import (
    AnalysisResultV2,
    ClientRecordingMetadata,
    TranscriptSegmentUpdate,
)
from audio_server.db.activity_models import (
    ProcessingActivity,
    ProcessingActivityType,
)
from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    JobKind,
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
from audio_server.services.bookmark_service import detach_recording_bookmarks
from audio_server.services.storage import (
    StagedDeletion,
    StagedUpload,
    StorageBackend,
    StorageConflictError,
    StorageError,
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

    def ingest_web(
        self,
        *,
        source: BinaryIO,
        filename: str,
        started_at: datetime | None,
    ) -> IngestResult:
        """Ingest a browser MP3/WAV/M4A upload without trusting its filename or MIME type."""

        if started_at is not None and (
            started_at.tzinfo is None or started_at.utcoffset() is None
        ):
            raise RecordingServiceError(
                "started_at_invalid",
                "Recording start time must include a timezone offset.",
                status_code=422,
            )
        try:
            staged = self._storage.create_staged_upload(source, max_bytes=self._max_upload_bytes)
        except UploadTooLargeError as exc:
            raise RecordingServiceError(
                "upload_too_large",
                "Uploaded audio exceeds the configured size limit.",
                status_code=413,
            ) from exc

        identifier = uuid.uuid4()
        upload_time = datetime.now(UTC)
        final_key: str | None = None
        try:
            probe = self._probe(staged)
            if probe.preferred_extension not in {".mp3", ".wav", ".m4a"}:
                raise RecordingServiceError(
                    "web_audio_type_unsupported",
                    "Browser uploads must be valid MP3, WAV, or M4A audio.",
                    status_code=415,
                )
            with self._session_factory() as session:
                duplicate = self._find_web_duplicate(session, staged.sha256)
                if duplicate is not None:
                    self._preserve_duplicate_upload(staged, duplicate)
                    logger.info(
                        "web recording upload deduplicated",
                        extra={"recording_id": str(duplicate.id)},
                    )
                    return IngestResult(recording=duplicate, duplicate=True)

            effective_start = started_at or upload_time
            final_key = original_storage_key(identifier, effective_start, probe.preferred_extension)
            self._storage.put_original(staged, final_key)
            try:
                result = self._create_web_recording_and_job(
                    identifier=identifier,
                    filename=filename,
                    started_at=effective_start,
                    upload_time=upload_time,
                    staged=staged,
                    probe=probe,
                    storage_key=final_key,
                )
            except Exception:
                # This UUID-derived key cannot belong to another recording. If
                # durable metadata/job creation fails, do not leave an orphaned
                # original behind after returning an error to the browser.
                self._storage.delete(final_key)
                raise
            logger.info(
                "web recording uploaded",
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
        checked: bool | None = None,
    ) -> list[Recording]:
        statement: Select[tuple[Recording]] = select(Recording)
        if device_id is not None:
            statement = statement.where(Recording.device_id == device_id)
        if status is not None:
            statement = statement.where(Recording.processing_status == status)
        if checked is not None:
            statement = statement.where(Recording.checked == checked)
        statement = statement.order_by(Recording.created_at.desc(), Recording.id.desc())
        statement = statement.limit(limit).offset(offset)
        with self._session_factory() as session:
            return list(session.scalars(statement))

    def set_checked(self, recording_id: uuid.UUID, *, checked: bool) -> Recording:
        """Mark a recording as reviewed by the administrator, or clear that mark."""

        with self._session_factory.begin() as session:
            recording = session.get(Recording, recording_id, with_for_update=True)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            recording.checked = checked
            session.flush()
            # updated_at is refreshed by the database, and the factory keeps
            # values readable after the block commits.
            session.refresh(recording)
            return recording

    def get_recording(self, recording_id: uuid.UUID) -> Recording:
        with self._session_factory() as session:
            recording = session.get(Recording, recording_id)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            return recording

    def get_audio_file(self, recording_id: uuid.UUID) -> tuple[Recording, Path, os.stat_result]:
        """Resolve a private original for an authenticated range-capable response."""

        recording = self.get_recording(recording_id)
        try:
            with self._storage.materialize(recording.storage_key) as path:
                stat_result = path.stat()
                return recording, path, stat_result
        except (FileNotFoundError, OSError) as exc:
            raise RecordingServiceError(
                "audio_unavailable",
                "The original audio file is unavailable.",
                status_code=404,
            ) from exc

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

    def list_active_jobs(
        self, *, limit: int = 50, kind: JobKind | None = None
    ) -> list[tuple[Recording, ProcessingJob]]:
        """Return unfinished jobs in the order a worker would claim them.

        Processing jobs come first so the view leads with what is running now;
        the rest follow by `available_at`, which is the queue's claim order.
        """

        with self._session_factory() as session:
            rows = session.execute(
                select(Recording, ProcessingJob)
                .join(ProcessingJob, ProcessingJob.recording_id == Recording.id)
                .where(
                    ProcessingJob.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING)),
                    *(() if kind is None else (ProcessingJob.kind == kind,)),
                )
                .order_by(
                    ProcessingJob.status != JobStatus.PROCESSING,
                    ProcessingJob.available_at,
                    ProcessingJob.created_at,
                    ProcessingJob.id,
                )
                .limit(limit)
            ).all()
            return [(recording, job) for recording, job in rows]

    def count_active_jobs(self) -> tuple[int, int]:
        """Return (processing, queued) across every unfinished job.

        The listing is capped, so these totals must be counted separately or a
        backlog larger than the cap would be reported as the cap itself.
        """

        with self._session_factory() as session:
            rows = session.execute(
                select(ProcessingJob.status, func.count())
                .where(ProcessingJob.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING)))
                .group_by(ProcessingJob.status)
            ).all()
            counts = {status: total for status, total in rows}
            return counts.get(JobStatus.PROCESSING, 0), counts.get(JobStatus.QUEUED, 0)

    def get_analysis_state(
        self, recording_id: uuid.UUID
    ) -> tuple[Recording, Analysis, ProcessingJob | None]:
        with self._session_factory() as session:
            recording = session.get(Recording, recording_id)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            analysis = session.scalar(
                select(Analysis).where(Analysis.recording_id == recording_id).limit(1)
            )
            if analysis is None:
                raise RecordingServiceError(
                    "analysis_not_ready", "Analysis is not available yet.", status_code=409
                )
            job = session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.recording_id == recording_id,
                    ProcessingJob.kind == JobKind.ANALYSIS,
                )
                .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                .limit(1)
            )
            if job is not None and job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                is_current_result_job = job.id == analysis.job_id
                failed_after_result = (
                    job.status is JobStatus.FAILED
                    and job.finished_at is not None
                    and (
                        analysis.completed_at is None
                        or job.finished_at > analysis.completed_at
                    )
                )
                if not is_current_result_job and not failed_after_result:
                    job = None
            return recording, analysis, job

    def update_transcript(
        self,
        recording_id: uuid.UUID,
        *,
        expected_revision: int,
        updates: list[TranscriptSegmentUpdate],
    ) -> tuple[Recording, list[TranscriptSegment]]:
        with self._session_factory.begin() as session:
            recording = session.get(Recording, recording_id, with_for_update=True)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            if recording.transcript_revision != expected_revision:
                raise RecordingServiceError(
                    "transcript_revision_conflict",
                    "The transcript changed in another session. Reload before saving.",
                    status_code=409,
                )
            self._reject_active_job(session, recording_id)
            segments = list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.recording_id == recording_id)
                    .with_for_update()
                )
            )
            existing_by_id = {segment.id: segment for segment in segments}
            update_ids = [item.id for item in updates]
            if len(update_ids) != len(set(update_ids)) or set(update_ids) != set(existing_by_id):
                raise RecordingServiceError(
                    "transcript_segments_incomplete",
                    "The complete current transcript segment list is required.",
                    status_code=422,
                )
            ordered = sorted(
                updates, key=lambda item: (item.start_time, item.end_time, str(item.id))
            )
            if any(item.end_time > recording.duration_seconds for item in ordered):
                raise RecordingServiceError(
                    "transcript_time_out_of_range",
                    "Transcript timestamps must stay within the recording duration.",
                    status_code=422,
                )
            for temporary_sequence, segment in enumerate(segments, start=1):
                segment.sequence = -temporary_sequence
            session.flush()
            overlaps = _overlap_flags(ordered)
            for sequence, item in enumerate(ordered):
                segment = existing_by_id[item.id]
                segment.sequence = sequence
                segment.speaker_label = item.speaker_label
                segment.start_time = item.start_time
                segment.end_time = item.end_time
                segment.text = item.text
                segment.confidence = None
                segment.has_overlap = overlaps[sequence]
            recording.transcript_revision += 1
            analysis = session.scalar(
                select(Analysis).where(Analysis.recording_id == recording_id).with_for_update()
            )
            if analysis is not None:
                analysis.status = AnalysisStatus.STALE
                analysis.error_code = None
                analysis.error_message = None
                recording.analysis_revision += 1
            session.flush()
            return recording, sorted(segments, key=lambda segment: segment.sequence)

    def update_analysis(
        self,
        recording_id: uuid.UUID,
        *,
        expected_revision: int,
        result: AnalysisResultV2,
    ) -> tuple[Recording, Analysis]:
        with self._session_factory.begin() as session:
            recording = session.get(Recording, recording_id, with_for_update=True)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            if recording.analysis_revision != expected_revision:
                raise RecordingServiceError(
                    "analysis_revision_conflict",
                    "The analysis changed in another session. Reload before saving.",
                    status_code=409,
                )
            self._reject_active_job(session, recording_id)
            segments = list(
                session.scalars(
                    select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
                )
            )
            grounded = _ground_analysis_result(result, segments)
            analysis = session.scalar(
                select(Analysis).where(Analysis.recording_id == recording_id).with_for_update()
            )
            if analysis is None:
                latest_job = session.scalar(
                    select(ProcessingJob)
                    .where(ProcessingJob.recording_id == recording_id)
                    .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                    .limit(1)
                )
                if latest_job is None:
                    raise RecordingServiceError(
                        "analysis_not_ready", "Analysis is not available yet.", status_code=409
                    )
                analysis = Analysis(
                    recording_id=recording_id,
                    job_id=latest_job.id,
                    provider="manual",
                    schema_version="2",
                    status=AnalysisStatus.COMPLETED,
                )
                session.add(analysis)
            analysis.provider = "manual"
            analysis.model = None
            analysis.schema_version = "2"
            analysis.status = AnalysisStatus.COMPLETED
            analysis.result = grounded.model_dump(mode="json")
            analysis.error_code = None
            analysis.error_message = None
            analysis.completed_at = datetime.now(UTC)
            recording.analysis_revision += 1
            session.flush()
            return recording, analysis

    @staticmethod
    def _reject_active_job(session: Session, recording_id: uuid.UUID) -> None:
        active_job = session.scalar(
            select(ProcessingJob.id)
            .where(
                ProcessingJob.recording_id == recording_id,
                ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]),
            )
            .limit(1)
        )
        if active_job is not None:
            raise RecordingServiceError(
                "job_already_active",
                "Wait for the active processing job to finish before saving edits.",
                status_code=409,
            )

    def retry(self, recording_id: uuid.UUID) -> ProcessingJob:
        return self._enqueue_processing(
            recording_id,
            allowed_statuses=frozenset({RecordingStatus.FAILED}),
            disallowed_code="recording_not_retryable",
            disallowed_message="Only a failed recording can be retried.",
        )

    def reprocess(self, recording_id: uuid.UUID) -> ProcessingJob:
        """Queue a fresh job while preserving the last good result until replacement."""

        return self._enqueue_processing(
            recording_id,
            allowed_statuses=frozenset(
                {
                    RecordingStatus.COMPLETED,
                    RecordingStatus.FAILED,
                }
            ),
            disallowed_code="recording_not_reprocessable",
            disallowed_message="Only a completed or failed recording can be reprocessed.",
            kind=JobKind.FULL,
        )

    def reprocess_analysis(self, recording_id: uuid.UUID) -> ProcessingJob:
        return self._enqueue_processing(
            recording_id,
            allowed_statuses=frozenset({RecordingStatus.COMPLETED}),
            disallowed_code="analysis_not_reprocessable",
            disallowed_message="Analysis can be queued after transcription completes.",
            kind=JobKind.ANALYSIS,
        )

    def delete_recording(self, recording_id: uuid.UUID) -> None:
        """Delete terminal recording data with a reversible local-file handoff."""

        deletion: StagedDeletion | None = None
        job_ids: list[uuid.UUID] = []
        with self._session_factory() as session:
            try:
                recording = session.get(Recording, recording_id, with_for_update=True)
                if recording is None:
                    raise RecordingServiceError(
                        "recording_not_found", "Recording was not found.", status_code=404
                    )
                active = session.scalar(
                    select(ProcessingJob.id).where(
                        ProcessingJob.recording_id == recording_id,
                        ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]),
                    )
                )
                if active is not None:
                    raise RecordingServiceError(
                        "recording_delete_active",
                        "A queued or processing recording cannot be deleted.",
                        status_code=409,
                    )
                job_ids = list(
                    session.scalars(
                        select(ProcessingJob.id).where(ProcessingJob.recording_id == recording_id)
                    )
                )
                deletion = self._storage.stage_delete(recording.storage_key)
                # Saved quotes outlive their source audio, so detach them
                # before the recording row goes.
                detach_recording_bookmarks(session, recording_id=recording_id)
                session.execute(
                    sql_delete(ProcessingActivity).where(
                        ProcessingActivity.recording_id == recording_id
                    )
                )
                session.execute(
                    sql_delete(TranscriptSegment).where(
                        TranscriptSegment.recording_id == recording_id
                    )
                )
                session.execute(sql_delete(Analysis).where(Analysis.recording_id == recording_id))
                session.execute(
                    sql_delete(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
                )
                session.execute(sql_delete(Recording).where(Recording.id == recording_id))
                session.commit()
            except RecordingServiceError:
                session.rollback()
                raise
            except Exception as exc:
                session.rollback()
                if deletion is not None:
                    try:
                        self._storage.restore_staged_delete(deletion)
                    except Exception as restore_exc:
                        logger.error(
                            "recording deletion rollback failed",
                            extra={
                                "recording_id": str(recording_id),
                                "error_type": type(restore_exc).__name__,
                            },
                        )
                        raise RecordingServiceError(
                            "recording_delete_rollback_failed",
                            "The recording could not be deleted safely.",
                            status_code=500,
                        ) from None
                if isinstance(exc, StorageError):
                    raise RecordingServiceError(
                        "recording_storage_delete_failed",
                        "The stored audio could not be prepared for deletion.",
                        status_code=500,
                    ) from None
                raise

        assert deletion is not None
        cleanup_steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("original", lambda: self._storage.finalize_staged_delete(deletion)),
            ("workspaces", lambda: self._storage.delete_workspaces(job_ids)),
        )
        for cleanup_step, cleanup in cleanup_steps:
            try:
                cleanup()
            except Exception as exc:
                # The database no longer exposes the recording. Keep ordinary
                # logs free of storage paths while surfacing private cleanup.
                logger.error(
                    "recording deletion cleanup failed",
                    extra={
                        "recording_id": str(recording_id),
                        "cleanup_step": cleanup_step,
                        "error_type": type(exc).__name__,
                    },
                )
        logger.info("recording deleted", extra={"recording_id": str(recording_id)})

    def _enqueue_processing(
        self,
        recording_id: uuid.UUID,
        *,
        allowed_statuses: frozenset[RecordingStatus],
        disallowed_code: str,
        disallowed_message: str,
        kind: JobKind = JobKind.FULL,
        require_transcript: bool = False,
    ) -> ProcessingJob:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            recording = session.get(Recording, recording_id, with_for_update=True)
            if recording is None:
                raise RecordingServiceError(
                    "recording_not_found", "Recording was not found.", status_code=404
                )
            if recording.processing_status not in allowed_statuses:
                raise RecordingServiceError(
                    disallowed_code,
                    disallowed_message,
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
            if require_transcript:
                has_transcript = session.scalar(
                    select(TranscriptSegment.id)
                    .where(TranscriptSegment.recording_id == recording_id)
                    .limit(1)
                )
                if has_transcript is None:
                    raise RecordingServiceError(
                        "transcript_not_ready",
                        "Transcript is not available yet.",
                        status_code=409,
                    )
            job = ProcessingJob(
                recording_id=recording.id,
                kind=kind,
                status=JobStatus.QUEUED,
                stage=JobStage.QUEUED,
                max_attempts=self._max_attempts,
                available_at=now,
            )
            if kind is JobKind.FULL:
                recording.processing_status = RecordingStatus.QUEUED
            session.add(job)
            session.flush()
            append_activity(
                session,
                recording_id=recording.id,
                job_id=job.id,
                job_kind=kind,
                event_type=ProcessingActivityType.MANUAL_RETRY_QUEUED,
                job_status=JobStatus.QUEUED,
                stage=JobStage.QUEUED,
                attempt_count=0,
                max_attempts=job.max_attempts,
                occurred_at=now,
            )
            logger.info(
                "processing job queued",
                extra={
                    "recording_id": str(recording.id),
                    "job_id": str(job.id),
                    "job_kind": kind.value,
                },
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
            kind=JobKind.FULL,
            status=JobStatus.QUEUED,
            stage=JobStage.QUEUED,
            max_attempts=self._max_attempts,
            available_at=now,
        )
        try:
            with self._session_factory.begin() as session:
                session.add_all([recording, job])
                session.flush()
                append_activity(
                    session,
                    recording_id=recording.id,
                    job_id=job.id,
                    job_kind=JobKind.FULL,
                    event_type=ProcessingActivityType.JOB_QUEUED,
                    job_status=JobStatus.QUEUED,
                    stage=JobStage.QUEUED,
                    attempt_count=0,
                    max_attempts=job.max_attempts,
                    occurred_at=now,
                )
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

    @staticmethod
    def _find_web_duplicate(session: Session, sha256: str) -> Recording | None:
        return session.scalar(
            select(Recording).where(
                Recording.device_id == "web-upload",
                Recording.sha256 == sha256,
            )
        )

    def _create_web_recording_and_job(
        self,
        *,
        identifier: uuid.UUID,
        filename: str,
        started_at: datetime,
        upload_time: datetime,
        staged: StagedUpload,
        probe: AudioProbe,
        storage_key: str,
    ) -> IngestResult:
        recording = Recording(
            id=identifier,
            device_id="web-upload",
            original_filename=_safe_filename(filename),
            storage_key=storage_key,
            mime_type=probe.mime_type,
            audio_format=probe.preferred_extension.lstrip("."),
            file_size=staged.file_size,
            sha256=staged.sha256,
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=probe.duration_seconds),
            duration_seconds=probe.duration_seconds,
            sample_rate=probe.sample_rate,
            channels=probe.channels,
            client_metadata={"source": "web-upload", "uploaded_at": upload_time.isoformat()},
            processing_status=RecordingStatus.QUEUED,
            audio_delete_after=_retention_date(upload_time, self._audio_retention_days),
            transcript_delete_after=_retention_date(
                upload_time, self._transcript_retention_days
            ),
        )
        job = ProcessingJob(
            recording_id=identifier,
            kind=JobKind.FULL,
            status=JobStatus.QUEUED,
            stage=JobStage.QUEUED,
            max_attempts=self._max_attempts,
            available_at=upload_time,
        )
        try:
            with self._session_factory.begin() as session:
                session.add_all([recording, job])
                session.flush()
                append_activity(
                    session,
                    recording_id=recording.id,
                    job_id=job.id,
                    job_kind=JobKind.FULL,
                    event_type=ProcessingActivityType.JOB_QUEUED,
                    job_status=JobStatus.QUEUED,
                    stage=JobStage.QUEUED,
                    attempt_count=0,
                    max_attempts=job.max_attempts,
                    occurred_at=upload_time,
                )
        except IntegrityError:
            with self._session_factory() as session:
                duplicate = self._find_web_duplicate(session, staged.sha256)
                if duplicate is not None:
                    self._delete_if_unreferenced(session, storage_key)
                    return IngestResult(recording=duplicate, duplicate=True)
                self._delete_if_unreferenced(session, storage_key)
            raise
        return IngestResult(recording=recording, duplicate=False)

    def _delete_if_unreferenced(self, session: Session, storage_key: str) -> None:
        owner_id = session.scalar(select(Recording.id).where(Recording.storage_key == storage_key))
        if owner_id is None:
            self._storage.delete(storage_key)


def _overlap_flags(ordered: list[TranscriptSegmentUpdate]) -> list[bool]:
    """Flag every segment that shares time with another, in one linear sweep.

    ``ordered`` is sorted by start time, so a segment overlaps something earlier
    exactly when the largest preceding end time is past its own start, and
    overlaps something later exactly when the next segment starts before its own
    end. Comparing each segment against the whole list instead is quadratic, and
    a dense multi-hour transcript holds tens of thousands of segments.
    """

    flags: list[bool] = []
    max_end_before = float("-inf")
    last_index = len(ordered) - 1
    for index, item in enumerate(ordered):
        overlaps_earlier = max_end_before > item.start_time
        overlaps_later = (
            index < last_index and ordered[index + 1].start_time < item.end_time
        )
        flags.append(overlaps_earlier or overlaps_later)
        max_end_before = max(max_end_before, item.end_time)
    return flags


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    basename = PurePosixPath(normalized).name or PurePath(normalized).name
    cleaned = "".join(character for character in basename if character.isprintable())
    return cleaned[:255] or "audio"


def _retention_date(now: datetime, days: int | None) -> datetime | None:
    return None if days is None else now + timedelta(days=days)


def _ground_analysis_result(
    result: AnalysisResultV2, segments: list[TranscriptSegment]
) -> AnalysisResultV2:
    segment_by_sequence = {segment.sequence: segment for segment in segments}
    payload = result.model_dump(mode="json")
    for collection_name in ("natural_expressions", "highlights"):
        for item in payload[collection_name]:
            segment = segment_by_sequence.get(item["segment_sequence"])
            if segment is None or item["original_ja"] not in segment.text:
                raise RecordingServiceError(
                    "analysis_quote_not_grounded",
                    "Every Japanese quote must occur in its referenced transcript segment.",
                    status_code=422,
                )
            item["start_time"] = segment.start_time
            item["speaker_label"] = segment.speaker_label
    return AnalysisResultV2.model_validate(payload)
