"""Day-scoped reads and the day summary job, grouped in Japan time.

The recordings are Japanese conversation, so a day is a Japan-time calendar
day: audio captured at 23:30 in Tokyo belongs to that evening rather than to
the previous UTC day.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from audio_server.db.models import (
    Analysis,
    AnalysisStatus,
    DailySummary,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
)
from audio_server.jobs.queue import InvalidRetryStateError, create_daily_summary_job
from audio_server.processing.contracts import AnalysisResult, DailyRecordingDigest

DAY_TIMEZONE = ZoneInfo("Asia/Tokyo")

# How many of the day's own analysis highlights are offered to the day summary.
_MAX_HIGHLIGHTS_PER_RECORDING = 3


class DailyServiceError(Exception):
    def __init__(self, code: str, safe_message: str, *, status_code: int) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code


def day_of(moment: datetime) -> date:
    """Return the Japan-time calendar day a stored timestamp belongs to."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(DAY_TIMEZONE).date()


def day_window(day: date) -> tuple[datetime, datetime]:
    """Return the half-open UTC range covering one Japan-time day.

    Expressing the day as a range keeps the query a plain comparison on
    ``started_at``, so it can use ``ix_recordings_started_at`` and behaves
    identically on PostgreSQL and on the SQLite used by unit tests.
    """

    start = datetime.combine(day, time.min, tzinfo=DAY_TIMEZONE)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DayListEntry:
    day: date
    recording_count: int
    analysed_count: int
    summary_status: AnalysisStatus | None
    summary_stale: bool


@dataclass(slots=True)
class DayDetail:
    day: date
    recordings: list[Recording] = field(default_factory=list)
    analysed_ids: set[uuid.UUID] = field(default_factory=set)
    active_job_ids: set[uuid.UUID] = field(default_factory=set)
    summary: DailySummary | None = None
    stale: bool = False
    job: ProcessingJob | None = None


class DailyService:
    """Reads days, and queues the one job that summarises a day."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        max_attempts: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts

    def list_days(self, *, limit: int, offset: int) -> list[DayListEntry]:
        """List days that hold recordings, newest first.

        Grouping happens in Python because the Japan-time day of a UTC
        timestamp is a database-specific expression, and one code path that
        behaves the same everywhere is worth more here than pushing the
        grouping down: rows are streamed newest first and stop as soon as
        enough days have been seen.
        """

        wanted = limit + offset
        counts: dict[date, int] = {}
        analysed: dict[date, int] = {}
        with self._session_factory() as session:
            analysed_ids = self._analysed_recording_ids(session)
            rows = session.execute(
                select(Recording.id, Recording.started_at).order_by(
                    Recording.started_at.desc(), Recording.id.desc()
                )
            ).yield_per(500)
            for recording_id, started_at in rows:
                day = day_of(started_at)
                if day not in counts and len(counts) >= wanted:
                    break
                counts[day] = counts.get(day, 0) + 1
                if recording_id in analysed_ids:
                    analysed[day] = analysed.get(day, 0) + 1
            days = sorted(counts, reverse=True)[offset : offset + limit]
            summaries = {
                summary.summary_date: summary
                for summary in session.scalars(
                    select(DailySummary).where(DailySummary.summary_date.in_(days))
                )
            }
            entries = []
            for day in days:
                summary = summaries.get(day)
                entries.append(
                    DayListEntry(
                        day=day,
                        recording_count=counts[day],
                        analysed_count=analysed.get(day, 0),
                        summary_status=summary.status if summary else None,
                        summary_stale=(
                            self._is_stale(session, day, summary) if summary else False
                        ),
                    )
                )
            return entries

    def get_day(self, day: date) -> DayDetail:
        start, end = day_window(day)
        with self._session_factory() as session:
            recordings = list(
                session.scalars(
                    select(Recording)
                    .where(Recording.started_at >= start, Recording.started_at < end)
                    .order_by(Recording.started_at, Recording.id)
                )
            )
            summary = session.scalar(
                select(DailySummary).where(DailySummary.summary_date == day)
            )
            job = session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.summary_date == day)
                .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                .limit(1)
            )
            analysed = self._analysed_recording_ids(session)
            ids = [recording.id for recording in recordings]
            return DayDetail(
                day=day,
                recordings=recordings,
                analysed_ids={
                    recording.id for recording in recordings if recording.id in analysed
                },
                active_job_ids=self._active_job_recording_ids(session, ids),
                summary=summary,
                stale=self._is_stale(session, day, summary) if summary else False,
                job=job,
            )

    def queue_summary(self, day: date) -> ProcessingJob:
        """Queue the day summary; the caller decides when a day is worth one."""

        with self._session_factory.begin() as session:
            digests, _ = self._collect(session, day)
            if not digests:
                raise DailyServiceError(
                    "daily_summary_not_available",
                    "A day can be summarised once its recordings have been analysed.",
                    status_code=409,
                )
            try:
                return create_daily_summary_job(
                    session, summary_date=day, max_attempts=self._max_attempts
                )
            except InvalidRetryStateError as exc:
                raise DailyServiceError(
                    "daily_summary_job_active",
                    "This day already has a summary in the queue.",
                    status_code=409,
                ) from exc
            except IntegrityError as exc:
                raise DailyServiceError(
                    "daily_summary_job_active",
                    "This day already has a summary in the queue.",
                    status_code=409,
                ) from exc

    def pending_analysis_recording_ids(self, day: date) -> list[uuid.UUID]:
        """The day's transcribed recordings that still have no analysis to show.

        A failed or missing analysis both count as pending, which is exactly
        what the day page marks as not analysed. Recordings that already hold a
        queued or running job are left out so the batch does not report work it
        was never going to be allowed to queue.
        """

        start, end = day_window(day)
        with self._session_factory() as session:
            analysed = self._analysed_recording_ids(session)
            ids = list(
                session.scalars(
                    select(Recording.id)
                    .where(
                        Recording.started_at >= start,
                        Recording.started_at < end,
                        Recording.processing_status == RecordingStatus.COMPLETED,
                    )
                    .order_by(Recording.started_at, Recording.id)
                )
            )
            active = self._active_job_recording_ids(session, ids)
            return [
                recording_id
                for recording_id in ids
                if recording_id not in analysed and recording_id not in active
            ]

    def collect_digests(self, day: date) -> tuple[list[DailyRecordingDigest], list[dict[str, Any]]]:
        """Build the summary input and the revision snapshot it was built from."""

        with self._session_factory() as session:
            return self._collect(session, day)

    def matches_revisions(
        self,
        session: Session,
        *,
        day: date,
        revisions: Sequence[dict[str, Any]],
    ) -> bool:
        """Whether the day still holds exactly the analyses a summary was built from.

        The check locks the day's recordings. Reading them without a lock only
        narrows the window: a deletion committing between the read and the
        write would put its content back into the summary, and back after the
        deletion had already cleared that day. Every writer here takes the
        recording row first — deletion and re-analysis both do — so taking it
        too makes the answer hold until this transaction commits.
        """

        _, current = self._collect(session, day, lock=True)
        return _revision_key(current) == _revision_key(revisions)

    def persist_summary(
        self,
        session: Session,
        *,
        day: date,
        result: AnalysisResult,
        source_revisions: Sequence[dict[str, Any]],
    ) -> DailySummary:
        """Write a finished summary inside the worker's claim transaction.

        A failure keeps the previous summary: only a successful result reaches
        this method, so the last good day summary survives a bad LM Studio run.
        """

        # The pipeline and the database keep separate status enums with the
        # same vocabulary; convert rather than storing the pipeline's own.
        status = AnalysisStatus(result.status.value)
        summary = session.scalar(select(DailySummary).where(DailySummary.summary_date == day))
        if summary is None:
            summary = DailySummary(summary_date=day, provider=result.provider, status=status)
            session.add(summary)
        summary.provider = result.provider
        summary.model = result.model
        summary.schema_version = str(result.schema_version)
        summary.status = status
        summary.result = dict(result.data) if result.data is not None else None
        summary.source_revisions = list(source_revisions)
        summary.error_code = result.error_code
        summary.error_message = result.error_message
        return summary

    def _collect(
        self, session: Session, day: date, *, lock: bool = False
    ) -> tuple[list[DailyRecordingDigest], list[dict[str, Any]]]:
        start, end = day_window(day)
        statement = (
            select(Recording, Analysis)
            .join(Analysis, Analysis.recording_id == Recording.id)
            .where(
                Recording.started_at >= start,
                Recording.started_at < end,
                Analysis.status == AnalysisStatus.COMPLETED,
            )
            .order_by(Recording.started_at, Recording.id)
        )
        if lock:
            statement = statement.with_for_update(of=Recording)
        rows = session.execute(statement).all()
        digests: list[DailyRecordingDigest] = []
        revisions: list[dict[str, Any]] = []
        for index, (recording, analysis) in enumerate(rows):
            digest = _digest_from_analysis(index, recording, analysis)
            if digest is None:
                continue
            digests.append(digest)
            revisions.append(
                {
                    "recording_id": str(recording.id),
                    "analysis_revision": recording.analysis_revision,
                }
            )
        return digests, revisions

    def _is_stale(self, session: Session, day: date, summary: DailySummary | None) -> bool:
        """A day whose analyses moved on since the summary was written."""

        if summary is None or summary.status is not AnalysisStatus.COMPLETED:
            return False
        _, current = self._collect(session, day)
        return _revision_key(current) != _revision_key(summary.source_revisions or [])

    def _active_job_recording_ids(
        self, session: Session, recording_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these recordings already have a queued or running job."""

        if not recording_ids:
            return set()
        # A day summary job carries no recording, so the column is nullable
        # even though this filter can only match rows that name one.
        return {
            recording_id
            for recording_id in session.scalars(
                select(ProcessingJob.recording_id).where(
                    ProcessingJob.recording_id.in_(recording_ids),
                    ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]),
                )
            )
            if recording_id is not None
        }

    def _analysed_recording_ids(self, session: Session) -> set[uuid.UUID]:
        return set(
            session.scalars(
                select(Analysis.recording_id).where(Analysis.status == AnalysisStatus.COMPLETED)
            )
        )


def _revision_key(revisions: Sequence[dict[str, Any]]) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            (str(item.get("recording_id", "")), int(item.get("analysis_revision", 0)))
            for item in revisions
        )
    )


def _digest_from_analysis(
    index: int, recording: Recording, analysis: Analysis
) -> DailyRecordingDigest | None:
    """Reduce one stored analysis to the fields a day summary reads."""

    result = analysis.result
    if not isinstance(result, dict):
        return None
    description = _bilingual(result.get("description"))
    if description is None:
        return None
    summary = _bilingual(result.get("summary")) or ("", "")
    return DailyRecordingDigest(
        index=index,
        recording_id=str(recording.id),
        time_label=recording.started_at.astimezone(DAY_TIMEZONE).strftime("%H:%M"),
        description_ja=description[0],
        description_zh_hk=description[1],
        summary_ja=summary[0],
        summary_zh_hk=summary[1],
        tags_ja=tuple(_japanese_values(result.get("tags"))),
        highlights_ja=tuple(
            _japanese_values(result.get("highlights"), key="original_ja")
        )[:_MAX_HIGHLIGHTS_PER_RECORDING],
    )


def _bilingual(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    ja = value.get("ja")
    zh_hk = value.get("zh_hk")
    if not isinstance(ja, str) or not isinstance(zh_hk, str) or not ja.strip():
        return None
    return ja, zh_hk


def _japanese_values(value: object, *, key: str = "ja") -> Iterator[str]:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            text = item.get(key)
            if isinstance(text, str) and text.strip():
                yield text
