"""PostgreSQL-backed queue primitives with lease fencing.

The queue intentionally uses the application database. Recording creation and
the initial job can therefore share one transaction, while workers claim jobs
with ``FOR UPDATE SKIP LOCKED`` and release the transaction before doing any AI
work.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, TypeAlias

from sqlalchemy import Select, or_, select, update
from sqlalchemy.orm import Session

from audio_server.activity.repository import append_activity
from audio_server.db.activity_models import ProcessingActivityType
from audio_server.db.models import (
    JobKind,
    JobStage,
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
)


class SessionFactory(Protocol):
    """The subset of ``sessionmaker`` used by the queue."""

    def __call__(self) -> Session: ...


class JobQueueError(RuntimeError):
    """Base class for queue coordination failures."""


class ClaimLostError(JobQueueError):
    """The worker no longer owns the lease identified by its claim token."""


class RecordingNotFoundError(JobQueueError):
    """A recording could not be found when enqueueing or retrying."""


class InvalidRetryStateError(JobQueueError):
    """A recording is not in a state that allows a manual retry."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Detached job identity passed to long-running processing code."""

    id: uuid.UUID
    recording_id: uuid.UUID | None
    kind: JobKind
    claim_token: uuid.UUID
    worker_id: str
    attempt_count: int
    max_attempts: int
    stage: JobStage
    # Set instead of ``recording_id`` for a day summary; the two are exclusive.
    summary_date: date | None = None


@dataclass(frozen=True, slots=True)
class JobFailure:
    """A privacy-safe failure suitable for durable status storage."""

    code: str
    error_type: str
    message: str
    retryable: bool
    stage: JobStage | None = None

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("failure code cannot be empty")
        if not self.error_type.strip():
            raise ValueError("failure error_type cannot be empty")
        if not self.message.strip():
            raise ValueError("failure message cannot be empty")


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    requeued: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential retry delays.

    ``attempt_count`` is incremented when a job is claimed. Its first failure
    therefore waits ``base_delay``; the second waits twice as long, capped by
    ``max_delay``.
    """

    base_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(seconds=900)

    def __post_init__(self) -> None:
        if self.base_delay <= timedelta(0):
            raise ValueError("base retry delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("maximum retry delay cannot be less than the base delay")

    def delay_after(self, attempt_count: int) -> timedelta:
        if attempt_count < 1:
            raise ValueError("attempt_count must be at least one")
        multiplier = 1 << min(attempt_count - 1, 30)
        return min(self.base_delay * multiplier, self.max_delay)

    @staticmethod
    def can_retry(*, retryable: bool, attempt_count: int, max_attempts: int) -> bool:
        if attempt_count < 0 or max_attempts < 1:
            raise ValueError("invalid attempt counts")
        return retryable and attempt_count < max_attempts


ResultPersister: TypeAlias = Callable[[Session], None]

_SAFE_CODE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _bounded_identifier(value: str, *, fallback: str) -> str:
    normalized = _SAFE_CODE.sub("_", value.strip())[:128]
    return normalized or fallback


def _bounded_message(value: str) -> str:
    # Messages are supplied by domain errors and must already be safe. Removing
    # control characters and bounding length prevents log/status injection and
    # accidental unbounded provider output.
    normalized = " ".join(value.split())
    return normalized[:1000] or "Processing failed."


def _coerce_stage(stage: JobStage | str) -> JobStage:
    if isinstance(stage, JobStage):
        return stage
    value = stage.value if isinstance(stage, enum.Enum) else stage
    return JobStage(value)


def _append_job_activity(
    session: Session,
    *,
    recording_id: uuid.UUID | None,
    job_id: uuid.UUID,
    job_kind: JobKind,
    event_type: ProcessingActivityType,
    job_status: JobStatus | None,
    stage: JobStage | None,
    attempt_count: int,
    max_attempts: int,
    occurred_at: datetime,
    error_code: str | None = None,
    error_type: str | None = None,
    safe_message: str | None = None,
    retry_scheduled: bool = False,
    next_attempt_at: datetime | None = None,
) -> None:
    """Append an event to a recording's timeline when the job has a recording.

    Processing activity is the per-recording history shown on a recording page.
    A day summary is scoped to a date and belongs on no single recording's
    timeline, so it records nothing here rather than borrowing one.
    """

    if recording_id is None:
        return
    append_activity(
        session,
        recording_id=recording_id,
        job_id=job_id,
        job_kind=job_kind,
        event_type=event_type,
        job_status=job_status,
        stage=stage,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        occurred_at=occurred_at,
        error_code=error_code,
        error_type=error_type,
        safe_message=safe_message,
        retry_scheduled=retry_scheduled,
        next_attempt_at=next_attempt_at,
    )


# A job that reads the committed transcript starts at the stage it will spend
# its whole life in; only transcription walks through stages of its own.
_CLAIMED_STAGE = {
    JobKind.ANALYSIS: JobStage.ANALYZING,
    JobKind.DAILY_SUMMARY: JobStage.ANALYZING,
    JobKind.TRANSLATION: JobStage.TRANSLATING,
}


def chained_follow_up(kind: JobKind) -> JobKind | None:
    """The job a recording's pipeline runs after ``kind`` when it is chained.

    Transcription commits the transcript and then hands the LLM work to the
    analysis worker. Analysis leads because it is what a reviewer waits for;
    the per-sentence translation is far longer and can trail behind it. Each
    step therefore owns its own retry budget, so a transient LM Studio failure
    costs a retry instead of the whole result.

    Only the chain uses this. A job queued by hand from the recording or day
    page carries no follow-up, so pressing one button never sets off the
    other's work.
    """

    if kind is JobKind.FULL:
        return JobKind.ANALYSIS
    if kind is JobKind.ANALYSIS:
        return JobKind.TRANSLATION
    return None


def _queue_follow_up(
    session: Session,
    job: ProcessingJob,
    *,
    max_attempts: int,
    occurred_at: datetime,
) -> ProcessingJob | None:
    """Queue the job that continues a recording's pipeline, in this transaction.

    The caller must already have flushed ``job`` into a terminal status:
    ``uq_processing_jobs_one_active_recording`` allows one queued or running
    job per recording, so the successor can only be inserted once this one no
    longer counts as active.
    """

    follow_up_kind = job.follow_up_kind
    recording_id = job.recording_id
    if follow_up_kind is None or recording_id is None:
        return None

    # A recording's timeline is ordered by time and then by a random id, so an
    # identical timestamp would show this queueing before the finish that
    # caused it. The successor is queued a moment later, which is also true.
    queued_at = occurred_at + timedelta(microseconds=1)
    follow_up = ProcessingJob(
        recording_id=recording_id,
        kind=follow_up_kind,
        follow_up_kind=chained_follow_up(follow_up_kind),
        status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=queued_at,
    )
    session.add(follow_up)
    session.flush()
    _append_job_activity(
        session,
        recording_id=recording_id,
        job_id=follow_up.id,
        job_kind=follow_up_kind,
        event_type=ProcessingActivityType.JOB_QUEUED,
        job_status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        occurred_at=queued_at,
    )
    return follow_up


def _active_job_statement(recording_id: uuid.UUID) -> Select[tuple[ProcessingJob]]:
    return select(ProcessingJob).where(
        ProcessingJob.recording_id == recording_id,
        ProcessingJob.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING)),
    )


def create_processing_job(
    session: Session,
    *,
    recording_id: uuid.UUID,
    max_attempts: int = 3,
    available_at: datetime | None = None,
    kind: JobKind = JobKind.FULL,
) -> ProcessingJob:
    """Create a queued job inside the caller's existing transaction.

    Upload ingestion should use this helper after adding its ``Recording`` so
    the recording and initial job commit atomically. The database partial unique
    index remains the final concurrency guard against duplicate active jobs.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    queued_at = available_at or _utcnow()

    # Upload ingestion may just have added this recording; the shared session
    # factory disables autoflush, so make it visible before querying.
    session.flush()
    recording = session.get(Recording, recording_id)
    if recording is None:
        raise RecordingNotFoundError(f"recording {recording_id} does not exist")

    active = session.scalar(_active_job_statement(recording_id).limit(1))
    if active is not None:
        raise InvalidRetryStateError("recording already has an active processing job")

    job = ProcessingJob(
        recording_id=recording_id,
        kind=kind,
        # Transcription always chains; a job asked for by hand never does.
        follow_up_kind=chained_follow_up(kind) if kind is JobKind.FULL else None,
        status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=queued_at,
    )
    if kind is JobKind.FULL:
        recording.processing_status = RecordingStatus.QUEUED
    session.add(job)
    session.flush()
    _append_job_activity(
        session,
        recording_id=recording_id,
        job_id=job.id,
        job_kind=kind,
        event_type=ProcessingActivityType.JOB_QUEUED,
        job_status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        occurred_at=queued_at,
    )
    return job


def _active_day_job_statement(summary_date: date) -> Select[tuple[ProcessingJob]]:
    return select(ProcessingJob).where(
        ProcessingJob.summary_date == summary_date,
        ProcessingJob.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING)),
    )


def create_daily_summary_job(
    session: Session,
    *,
    summary_date: date,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> ProcessingJob:
    """Queue a day summary inside the caller's existing transaction.

    The job names a date rather than a recording, so nothing here changes any
    recording status. The partial unique index on ``summary_date`` stays the
    final guard against two summaries being queued for the same day.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    queued_at = available_at or _utcnow()

    session.flush()
    active = session.scalar(_active_day_job_statement(summary_date).limit(1))
    if active is not None:
        raise InvalidRetryStateError("day already has an active summary job")

    job = ProcessingJob(
        recording_id=None,
        summary_date=summary_date,
        kind=JobKind.DAILY_SUMMARY,
        status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=queued_at,
    )
    session.add(job)
    session.flush()
    return job


def retry_failed_recording(
    session: Session,
    *,
    recording_id: uuid.UUID,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> ProcessingJob:
    """Create a new job for a failed recording in the caller's transaction.

    Locking the recording serializes concurrent retry requests. The active-job
    partial unique index additionally protects the invariant at commit time.
    """

    recording = session.scalar(
        select(Recording).where(Recording.id == recording_id).with_for_update()
    )
    if recording is None:
        raise RecordingNotFoundError(f"recording {recording_id} does not exist")
    if recording.processing_status is not RecordingStatus.FAILED:
        raise InvalidRetryStateError("only a failed recording can be retried")

    active = session.scalar(_active_job_statement(recording_id).limit(1))
    if active is not None:
        raise InvalidRetryStateError("recording already has an active processing job")

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    queued_at = available_at or _utcnow()
    job = ProcessingJob(
        recording_id=recording_id,
        kind=JobKind.FULL,
        follow_up_kind=chained_follow_up(JobKind.FULL),
        status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=queued_at,
    )
    recording.processing_status = RecordingStatus.QUEUED
    session.add(job)
    session.flush()
    _append_job_activity(
        session,
        recording_id=recording_id,
        job_id=job.id,
        job_kind=JobKind.FULL,
        event_type=ProcessingActivityType.MANUAL_RETRY_QUEUED,
        job_status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        occurred_at=queued_at,
    )
    return job


class JobQueue:
    """Lease-based database queue; every public operation uses a short session."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=300),
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        self._session_factory = session_factory
        self.worker_id = _bounded_identifier(worker_id, fallback="worker")
        self.lease_duration = lease_duration
        self.retry_policy = retry_policy or RetryPolicy()

    def claim_next(
        self,
        *,
        now: datetime | None = None,
        kinds: Collection[JobKind] | None = None,
    ) -> ClaimedJob | None:
        """Atomically lease the oldest available job without blocking peers.

        ``kinds`` restricts the claim to those job kinds. A worker that only
        handles analysis therefore never leases a transcription job it has no
        providers for, and cheap analysis work stops queueing behind hours of
        CPU-bound transcription.
        """

        claimed_at = now or _utcnow()
        token = uuid.uuid4()
        conditions = [
            ProcessingJob.status == JobStatus.QUEUED,
            ProcessingJob.available_at <= claimed_at,
        ]
        if kinds is not None:
            conditions.append(ProcessingJob.kind.in_(tuple(kinds)))
        with self._session_factory() as session, session.begin():
            job = session.scalar(
                select(ProcessingJob)
                .where(*conditions)
                .order_by(ProcessingJob.available_at, ProcessingJob.created_at, ProcessingJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None

            job.status = JobStatus.PROCESSING
            job.stage = _CLAIMED_STAGE.get(job.kind, JobStage.PREPROCESSING)
            job.attempt_count += 1
            job.worker_id = self.worker_id
            job.claim_token = token
            job.claimed_at = claimed_at
            job.heartbeat_at = claimed_at
            job.lease_expires_at = claimed_at + self.lease_duration
            job.started_at = job.started_at or claimed_at
            job.finished_at = None
            job.failed_stage = None
            job.error_code = None
            job.error_type = None
            job.error_message = None
            job.error_at = None
            if job.kind is JobKind.FULL:
                session.execute(
                    update(Recording)
                    .where(Recording.id == job.recording_id)
                    .values(processing_status=RecordingStatus.PROCESSING)
                )
            _append_job_activity(
                session,
                recording_id=job.recording_id,
                job_id=job.id,
                job_kind=job.kind,
                event_type=ProcessingActivityType.PROCESSING_STARTED,
                job_status=JobStatus.PROCESSING,
                stage=job.stage,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                occurred_at=claimed_at,
            )
            claimed = ClaimedJob(
                id=job.id,
                recording_id=job.recording_id,
                summary_date=job.summary_date,
                kind=job.kind,
                claim_token=token,
                worker_id=self.worker_id,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                stage=job.stage,
            )
        return claimed

    def heartbeat(self, claim: ClaimedJob, *, now: datetime | None = None) -> None:
        """Extend a lease only if the caller still owns its claim token."""

        heartbeat_at = now or _utcnow()
        with self._session_factory() as session, session.begin():
            updated_id = session.scalar(
                update(ProcessingJob)
                .where(
                    ProcessingJob.id == claim.id,
                    ProcessingJob.status == JobStatus.PROCESSING,
                    ProcessingJob.claim_token == claim.claim_token,
                    ProcessingJob.worker_id == claim.worker_id,
                )
                .values(
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + self.lease_duration,
                )
                .returning(ProcessingJob.id)
            )
            if updated_id is None:
                raise ClaimLostError("processing job lease is no longer owned by this worker")

    def advance_stage(
        self,
        claim: ClaimedJob,
        stage: JobStage | str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist progress and extend the lease under the same fence."""

        next_stage = _coerce_stage(stage)
        if next_stage in {JobStage.QUEUED, JobStage.COMPLETED}:
            raise ValueError("queued/completed stages are controlled by queue lifecycle methods")
        heartbeat_at = now or _utcnow()
        with self._session_factory() as session, session.begin():
            updated_id = session.scalar(
                update(ProcessingJob)
                .where(
                    ProcessingJob.id == claim.id,
                    ProcessingJob.status == JobStatus.PROCESSING,
                    ProcessingJob.claim_token == claim.claim_token,
                    ProcessingJob.worker_id == claim.worker_id,
                )
                .values(
                    stage=next_stage,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + self.lease_duration,
                )
                .returning(ProcessingJob.id)
            )
            if updated_id is None:
                raise ClaimLostError("cannot update stage after losing the job lease")
            _append_job_activity(
                session,
                recording_id=claim.recording_id,
                job_id=claim.id,
                job_kind=claim.kind,
                event_type=ProcessingActivityType.STAGE_STARTED,
                job_status=JobStatus.PROCESSING,
                stage=next_stage,
                attempt_count=claim.attempt_count,
                max_attempts=claim.max_attempts,
                occurred_at=heartbeat_at,
            )

    def complete(
        self,
        claim: ClaimedJob,
        *,
        persist_results: ResultPersister | None = None,
        now: datetime | None = None,
    ) -> None:
        """Persist results and completion atomically after checking the fence."""

        completed_at = now or _utcnow()
        with self._session_factory() as session, session.begin():
            job = self._lock_owned_job(session, claim)
            if persist_results is not None:
                persist_results(session)
            job.status = JobStatus.COMPLETED
            job.stage = JobStage.COMPLETED
            job.finished_at = completed_at
            job.heartbeat_at = completed_at
            job.lease_expires_at = None
            job.claim_token = None
            if job.kind is JobKind.FULL:
                session.execute(
                    update(Recording)
                    .where(Recording.id == job.recording_id)
                    .values(processing_status=RecordingStatus.COMPLETED)
                )
            _append_job_activity(
                session,
                recording_id=job.recording_id,
                job_id=job.id,
                job_kind=job.kind,
                event_type=ProcessingActivityType.PROCESSING_COMPLETED,
                job_status=JobStatus.COMPLETED,
                stage=JobStage.COMPLETED,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                occurred_at=completed_at,
            )
            # The successor is queued in this transaction, so a worker that dies
            # between committing a transcript and asking for its analysis cannot
            # leave the recording half finished.
            session.flush()
            _queue_follow_up(
                session,
                job,
                max_attempts=job.max_attempts,
                occurred_at=completed_at,
            )

    def fail(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Record a failure and return whether the job was scheduled to retry."""

        failed_at = now or _utcnow()
        with self._session_factory() as session, session.begin():
            job = self._lock_owned_job(session, claim)
            failed_stage = failure.stage or job.stage
            will_retry = self.retry_policy.can_retry(
                retryable=failure.retryable,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
            )

            job.failed_stage = failed_stage
            job.error_code = _bounded_identifier(failure.code, fallback="processing_failed")
            job.error_type = _bounded_identifier(failure.error_type, fallback="ProcessingError")
            job.error_message = _bounded_message(failure.message)
            job.error_at = failed_at
            job.claim_token = None
            job.worker_id = None
            job.claimed_at = None
            job.heartbeat_at = None
            job.lease_expires_at = None

            if will_retry:
                job.status = JobStatus.QUEUED
                job.stage = JobStage.QUEUED
                job.available_at = failed_at + self.retry_policy.delay_after(job.attempt_count)
                if job.kind is JobKind.FULL:
                    session.execute(
                        update(Recording)
                        .where(Recording.id == job.recording_id)
                        .values(processing_status=RecordingStatus.QUEUED)
                    )
                _append_job_activity(
                    session,
                    recording_id=job.recording_id,
                    job_id=job.id,
                    job_kind=job.kind,
                    event_type=ProcessingActivityType.RETRY_SCHEDULED,
                    job_status=JobStatus.QUEUED,
                    stage=failed_stage,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    occurred_at=failed_at,
                    error_code=job.error_code,
                    error_type=job.error_type,
                    safe_message=job.error_message,
                    retry_scheduled=True,
                    next_attempt_at=job.available_at,
                )
            else:
                job.status = JobStatus.FAILED
                job.finished_at = failed_at
                if job.kind is JobKind.FULL:
                    session.execute(
                        update(Recording)
                        .where(Recording.id == job.recording_id)
                        .values(processing_status=RecordingStatus.FAILED)
                    )
                _append_job_activity(
                    session,
                    recording_id=job.recording_id,
                    job_id=job.id,
                    job_kind=job.kind,
                    event_type=ProcessingActivityType.PROCESSING_FAILED,
                    job_status=JobStatus.FAILED,
                    stage=failed_stage,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    occurred_at=failed_at,
                    error_code=job.error_code,
                    error_type=job.error_type,
                    safe_message=job.error_message,
                )
                if job.kind is not JobKind.FULL:
                    # Analysis and translation are independent readings of the
                    # same transcript, so one giving up does not cancel the
                    # other. Transcription is different: a run that failed
                    # committed no transcript for the chain to work from.
                    session.flush()
                    _queue_follow_up(
                        session,
                        job,
                        max_attempts=job.max_attempts,
                        occurred_at=failed_at,
                    )
        return will_retry

    def recover_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> RecoverySummary:
        """Requeue or fail abandoned leases while fencing their former owners."""

        if limit < 1:
            raise ValueError("recovery limit must be positive")
        recovered_at = now or _utcnow()
        requeued = 0
        failed = 0
        with self._session_factory() as session, session.begin():
            jobs = session.scalars(
                select(ProcessingJob)
                .where(
                    ProcessingJob.status == JobStatus.PROCESSING,
                    or_(
                        ProcessingJob.lease_expires_at.is_(None),
                        ProcessingJob.lease_expires_at <= recovered_at,
                    ),
                )
                .order_by(ProcessingJob.lease_expires_at, ProcessingJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            for job in jobs:
                failed_stage = job.stage
                job.failed_stage = failed_stage
                job.error_code = "worker_lease_expired"
                job.error_type = "WorkerLeaseExpired"
                job.error_message = "The processing worker stopped renewing its lease."
                job.error_at = recovered_at
                job.claim_token = None
                job.worker_id = None
                job.claimed_at = None
                job.heartbeat_at = None
                job.lease_expires_at = None

                can_retry = self.retry_policy.can_retry(
                    retryable=True,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                )
                if can_retry:
                    job.status = JobStatus.QUEUED
                    job.stage = JobStage.QUEUED
                    job.available_at = recovered_at + self.retry_policy.delay_after(
                        job.attempt_count
                    )
                    recording_status = RecordingStatus.QUEUED
                    requeued += 1
                else:
                    job.status = JobStatus.FAILED
                    job.finished_at = recovered_at
                    recording_status = RecordingStatus.FAILED
                    failed += 1
                _append_job_activity(
                    session,
                    recording_id=job.recording_id,
                    job_id=job.id,
                    job_kind=job.kind,
                    event_type=ProcessingActivityType.LEASE_RECOVERED,
                    job_status=job.status,
                    stage=failed_stage,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    occurred_at=recovered_at,
                    error_code=job.error_code,
                    error_type=job.error_type,
                    safe_message=job.error_message,
                    retry_scheduled=can_retry,
                    next_attempt_at=job.available_at if can_retry else None,
                )
                if job.kind is JobKind.FULL:
                    session.execute(
                        update(Recording)
                        .where(Recording.id == job.recording_id)
                        .values(processing_status=recording_status)
                    )
                elif not can_retry:
                    # A worker that died holding an analysis lease must not take
                    # the translation down with it.
                    session.flush()
                    _queue_follow_up(
                        session,
                        job,
                        max_attempts=job.max_attempts,
                        occurred_at=recovered_at,
                    )
        return RecoverySummary(requeued=requeued, failed=failed)

    @staticmethod
    def _lock_owned_job(session: Session, claim: ClaimedJob) -> ProcessingJob:
        job = session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.id == claim.id,
                ProcessingJob.status == JobStatus.PROCESSING,
                ProcessingJob.claim_token == claim.claim_token,
                ProcessingJob.worker_id == claim.worker_id,
            )
            .with_for_update()
        )
        if job is None:
            raise ClaimLostError("processing job lease is no longer owned by this worker")
        return job
