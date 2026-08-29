"""A day summary covers every analysed recording of one Japan-time day.

The recordings are Japanese conversation, so the day boundary is Tokyo's, not
UTC's. The job that produces a summary is scoped to a date rather than to a
recording, which is the one place the queue carries something other than a
recording id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.activity_models import ProcessingActivity
from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    DailySummary,
    JobKind,
    JobStage,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
)
from audio_server.jobs.queue import (
    InvalidRetryStateError,
    JobQueue,
    RetryPolicy,
    create_daily_summary_job,
)
from audio_server.processing.contracts import AnalysisResult, DailyRecordingDigest
from audio_server.processing.contracts import AnalysisStatus as PipelineAnalysisStatus
from audio_server.services.daily_service import (
    DailyService,
    DailyServiceError,
    day_of,
    day_window,
)
from audio_server.worker_runtime import PipelineJobProcessor
from tests.conftest import TEST_API_TOKEN

# The summary for 27 August becomes claimable once that Tokyo day is over.
_AVAILABLE_AT = datetime(2026, 8, 27, 16, 0, tzinfo=UTC)


def _analysis_result(description_ja: str = "朝の打ち合わせ。") -> dict[str, Any]:
    return {
        "description": {"ja": description_ja, "zh_hk": "朝早開會。"},
        "summary": {"ja": "進捗の共有。", "zh_hk": "分享進度。"},
        "tags": [{"ja": "会議", "zh_hk": "會議"}],
        "natural_expressions": [],
        "highlights": [
            {
                "segment_sequence": 0,
                "start_time": 0.0,
                "end_time": 1.0,
                "speaker_label": "SPEAKER_00",
                "original_ja": "よろしくお願いします。",
                "translation_zh_hk": "麻煩晒。",
                "reason_ja": "定番の挨拶。",
                "reason_zh_hk": "常用問候。",
            }
        ],
    }


def _seed_recording(
    session_factory: sessionmaker[Session],
    *,
    started_at: datetime,
    analysed: bool = True,
    analysis_revision: int = 1,
    filename: str = "meeting.wav",
) -> uuid.UUID:
    recording_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            Recording(
                id=recording_id,
                device_id="test-device",
                original_filename=filename,
                storage_key=f"recordings/{recording_id}/original.wav",
                mime_type="audio/wav",
                audio_format="wav",
                file_size=1024,
                sha256=recording_id.hex * 2,
                started_at=started_at,
                ended_at=started_at + timedelta(seconds=1),
                duration_seconds=1.0,
                sample_rate=16_000,
                channels=1,
                processing_status=RecordingStatus.COMPLETED,
                analysis_revision=analysis_revision,
            )
        )
        job_id = uuid.uuid4()
        session.add(
            ProcessingJob(
                id=job_id,
                recording_id=recording_id,
                kind=JobKind.FULL,
                status=JobStatus.COMPLETED,
                available_at=started_at,
            )
        )
        if analysed:
            session.add(
                Analysis(
                    recording_id=recording_id,
                    job_id=job_id,
                    provider="lmstudio",
                    schema_version="2",
                    status=AnalysisStatus.COMPLETED,
                    result=_analysis_result(),
                )
            )
    return recording_id


def _queue(session_factory: sessionmaker[Session]) -> JobQueue:
    return JobQueue(
        session_factory,
        worker_id="test-worker",
        lease_duration=timedelta(seconds=300),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=30), max_delay=timedelta(seconds=900)
        ),
    )


class StubDailyProvider:
    """Returns a fixed structured summary without touching LM Studio."""

    def __init__(self, status: PipelineAnalysisStatus = PipelineAnalysisStatus.COMPLETED) -> None:
        self.status = status
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    @property
    def name(self) -> str:
        return "stub"

    def summarize(
        self, summary_date: str, digests: list[DailyRecordingDigest]
    ) -> AnalysisResult:
        self.seen.append((summary_date, tuple(digest.recording_id for digest in digests)))
        if self.status is not PipelineAnalysisStatus.COMPLETED:
            return AnalysisResult(status=self.status, provider=self.name)
        return AnalysisResult(
            status=PipelineAnalysisStatus.COMPLETED,
            provider=self.name,
            model="stub-model",
            data={
                "overview": {"ja": "静かな一日。", "zh_hk": "平靜嘅一日。"},
                "key_points": [
                    {
                        "recording_id": digests[0].recording_id if digests else "",
                        "ja": "進捗を共有した。",
                        "zh_hk": "分享咗進度。",
                    }
                ]
                if digests
                else [],
                "tags": [{"ja": "会議", "zh_hk": "會議"}],
            },
        )


class TestJapanTimeDayBoundary:
    def test_late_evening_in_tokyo_stays_on_its_own_day(self) -> None:
        # 23:30 on 27 August in Tokyo is still 14:30 UTC on the 27th.
        assert day_of(datetime(2026, 8, 27, 14, 30, tzinfo=UTC)) == date(2026, 8, 27)

    def test_after_tokyo_midnight_moves_to_the_next_day(self) -> None:
        # 15:00 UTC is midnight in Tokyo, so this belongs to the 28th.
        assert day_of(datetime(2026, 8, 27, 15, 0, tzinfo=UTC)) == date(2026, 8, 28)

    def test_window_is_a_half_open_utc_range(self) -> None:
        start, end = day_window(date(2026, 8, 27))
        assert start == datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
        assert end == datetime(2026, 8, 27, 15, 0, tzinfo=UTC)


class TestDigestCollection:
    def test_only_the_days_analysed_recordings_are_collected(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service = DailyService(session_factory=session_factory)
        wanted = _seed_recording(
            session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
        )
        # Midnight in Tokyo already belongs to the following day.
        _seed_recording(session_factory, started_at=datetime(2026, 8, 27, 15, 30, tzinfo=UTC))
        # Recorded on the day, but never analysed.
        _seed_recording(
            session_factory,
            started_at=datetime(2026, 8, 27, 2, 0, tzinfo=UTC),
            analysed=False,
        )

        digests, revisions = service.collect_digests(date(2026, 8, 27))

        assert [digest.recording_id for digest in digests] == [str(wanted)]
        assert digests[0].index == 0
        assert digests[0].description_ja == "朝の打ち合わせ。"
        assert revisions == [{"recording_id": str(wanted), "analysis_revision": 1}]

    def test_a_digest_carries_no_transcript_text(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service = DailyService(session_factory=session_factory)
        _seed_recording(session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC))

        digests, _ = service.collect_digests(date(2026, 8, 27))

        # Only fields the analysis already published may travel to the model.
        assert digests[0].highlights_ja == ("よろしくお願いします。",)
        assert not hasattr(digests[0], "transcript")


class TestDaySummaryJob:
    def test_the_job_names_a_day_and_no_recording(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory.begin() as session:
            job = create_daily_summary_job(session, summary_date=date(2026, 8, 27))
            job_id = job.id

        with session_factory() as session:
            stored = session.get(ProcessingJob, job_id)
            assert stored is not None
            assert stored.recording_id is None
            assert stored.summary_date == date(2026, 8, 27)
            assert stored.kind is JobKind.DAILY_SUMMARY

    def test_a_second_summary_for_one_day_is_refused(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory.begin() as session:
            create_daily_summary_job(session, summary_date=date(2026, 8, 27))

        with pytest.raises(InvalidRetryStateError), session_factory.begin() as session:
            create_daily_summary_job(session, summary_date=date(2026, 8, 27))

    def test_claiming_starts_at_the_analysis_stage_and_writes_no_activity(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory.begin() as session:
            create_daily_summary_job(
                session, summary_date=date(2026, 8, 27), available_at=_AVAILABLE_AT
            )

        claim = _queue(session_factory).claim_next(now=datetime(2026, 8, 28, tzinfo=UTC))

        assert claim is not None
        assert claim.recording_id is None
        assert claim.summary_date == date(2026, 8, 27)
        assert claim.stage is JobStage.ANALYZING
        with session_factory() as session:
            # Activity is a per-recording timeline and has no room for a day.
            assert session.scalars(select(ProcessingActivity)).all() == []

    def test_queueing_needs_at_least_one_analysed_recording(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service = DailyService(session_factory=session_factory)
        _seed_recording(
            session_factory,
            started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC),
            analysed=False,
        )

        with pytest.raises(DailyServiceError) as caught:
            service.queue_summary(date(2026, 8, 27))

        assert caught.value.status_code == 409
        assert caught.value.code == "daily_summary_not_available"


class TestWorkerHandling:
    def test_a_claimed_day_job_writes_the_summary(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        recording_id = _seed_recording(
            session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
        )
        service = DailyService(session_factory=session_factory)
        provider = StubDailyProvider()
        processor = PipelineJobProcessor(
            session_factory=session_factory,
            storage=None,  # type: ignore[arg-type]
            daily_summary_provider=provider,
            daily_service=service,
        )
        with session_factory.begin() as session:
            create_daily_summary_job(
                session, summary_date=date(2026, 8, 27), available_at=_AVAILABLE_AT
            )
        queue = _queue(session_factory)
        claim = queue.claim_next(now=datetime(2026, 8, 28, tzinfo=UTC))
        assert claim is not None

        persist = processor(claim, lambda stage: None)
        queue.complete(claim, persist_results=persist)

        assert provider.seen == [("2026-08-27", (str(recording_id),))]
        with session_factory() as session:
            summary = session.scalar(
                select(DailySummary).where(DailySummary.summary_date == date(2026, 8, 27))
            )
            assert summary is not None
            assert summary.status is AnalysisStatus.COMPLETED
            assert summary.model == "stub-model"
            assert summary.result is not None
            assert summary.result["overview"]["ja"] == "静かな一日。"
            assert summary.source_revisions == [
                {"recording_id": str(recording_id), "analysis_revision": 1}
            ]

    def test_a_day_with_nothing_left_keeps_its_previous_summary(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service = DailyService(session_factory=session_factory)
        with session_factory.begin() as session:
            session.add(
                DailySummary(
                    summary_date=date(2026, 8, 27),
                    provider="stub",
                    status=AnalysisStatus.COMPLETED,
                    result={"overview": {"ja": "前の要約。", "zh_hk": "之前嘅摘要。"}},
                    source_revisions=[],
                )
            )
            create_daily_summary_job(
                session, summary_date=date(2026, 8, 27), available_at=_AVAILABLE_AT
            )
        processor = PipelineJobProcessor(
            session_factory=session_factory,
            storage=None,  # type: ignore[arg-type]
            daily_summary_provider=StubDailyProvider(PipelineAnalysisStatus.SKIPPED),
            daily_service=service,
        )
        queue = _queue(session_factory)
        claim = queue.claim_next(now=datetime(2026, 8, 28, tzinfo=UTC))
        assert claim is not None

        queue.complete(claim, persist_results=processor(claim, lambda stage: None))

        with session_factory() as session:
            summary = session.scalar(
                select(DailySummary).where(DailySummary.summary_date == date(2026, 8, 27))
            )
            assert summary is not None
            assert summary.result is not None
            assert summary.result["overview"]["ja"] == "前の要約。"


class TestStaleness:
    def test_a_reanalysed_recording_marks_the_day_stale(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service = DailyService(session_factory=session_factory)
        recording_id = _seed_recording(
            session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
        )
        with session_factory.begin() as session:
            session.add(
                DailySummary(
                    summary_date=date(2026, 8, 27),
                    provider="stub",
                    status=AnalysisStatus.COMPLETED,
                    result={"overview": {"ja": "要約。", "zh_hk": "摘要。"}},
                    source_revisions=[
                        {"recording_id": str(recording_id), "analysis_revision": 1}
                    ],
                )
            )
        assert service.get_day(date(2026, 8, 27)).stale is False

        with session_factory.begin() as session:
            recording = session.get(Recording, recording_id)
            assert recording is not None
            recording.analysis_revision = 2

        assert service.get_day(date(2026, 8, 27)).stale is True


class TestDaysApi:
    def test_a_day_lists_its_recordings_and_summary(self, app_client: TestClient) -> None:
        session_factory = app_client.app.state.test_session_factory
        recording_id = _seed_recording(
            session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
        )
        headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

        listing = app_client.get("/api/v1/days", headers=headers)
        detail = app_client.get("/api/v1/days/2026-08-27", headers=headers)

        assert listing.status_code == 200
        assert listing.json()["items"] == [
            {
                "day": "2026-08-27",
                "recording_count": 1,
                "analysed_count": 1,
                "summary_status": None,
                "summary_stale": False,
            }
        ]
        assert detail.status_code == 200
        body = detail.json()
        assert [item["id"] for item in body["recordings"]] == [str(recording_id)]
        assert body["analysed_recording_ids"] == [str(recording_id)]
        assert body["summary"] is None
        assert body["stale"] is False

    def test_a_summary_is_queued_on_request(self, app_client: TestClient) -> None:
        session_factory = app_client.app.state.test_session_factory
        _seed_recording(session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC))
        headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

        response = app_client.post("/api/v1/days/2026-08-27/summary/reprocess", headers=headers)

        assert response.status_code == 202
        assert response.json()["day"] == "2026-08-27"
        with session_factory() as session:
            job = session.scalar(
                select(ProcessingJob).where(ProcessingJob.summary_date.is_not(None))
            )
            assert job is not None
            assert job.kind is JobKind.DAILY_SUMMARY

    def test_a_day_without_analyses_cannot_be_summarised(self, app_client: TestClient) -> None:
        session_factory = app_client.app.state.test_session_factory
        _seed_recording(
            session_factory,
            started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC),
            analysed=False,
        )
        headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

        response = app_client.post("/api/v1/days/2026-08-27/summary/reprocess", headers=headers)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "daily_summary_not_available"

    def test_a_failed_summary_job_is_reported(self, app_client: TestClient) -> None:
        # Without this the queued notice would simply vanish and the day would
        # look as though nothing had been asked for.
        session_factory = app_client.app.state.test_session_factory
        _seed_recording(session_factory, started_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC))
        with session_factory.begin() as session:
            session.add(
                ProcessingJob(
                    recording_id=None,
                    summary_date=date(2026, 8, 27),
                    kind=JobKind.DAILY_SUMMARY,
                    status=JobStatus.FAILED,
                    stage=JobStage.ANALYZING,
                    attempt_count=3,
                    max_attempts=3,
                    available_at=_AVAILABLE_AT,
                    failed_stage=JobStage.ANALYZING,
                    error_code="lmstudio_unavailable",
                    error_type="RetryableProcessingError",
                    error_message="LM Studio is temporarily unavailable.",
                    error_at=_AVAILABLE_AT,
                )
            )
        headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

        body = app_client.get("/api/v1/days/2026-08-27", headers=headers).json()

        assert body["job"]["status"] == "failed"
        assert body["job"]["error"]["code"] == "lmstudio_unavailable"
        assert body["job"]["error"]["message"] == "LM Studio is temporarily unavailable."

    def test_days_require_authentication(self, app_client: TestClient) -> None:
        assert app_client.get("/api/v1/days").status_code == 401
