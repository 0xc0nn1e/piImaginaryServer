"""Background worker loop and bounded process supervisor."""

from __future__ import annotations

import enum
import importlib
import logging
import multiprocessing
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from types import FrameType, TracebackType
from typing import Any, Protocol, TypeVar, cast

from sqlalchemy.orm import Session

from audio_server.core.config import get_settings
from audio_server.core.logging import configure_logging
from audio_server.db.models import JobStage
from audio_server.jobs.queue import (
    ClaimedJob,
    ClaimLostError,
    JobFailure,
    JobQueue,
    ResultPersister,
    RetryPolicy,
)
from audio_server.processing.errors import ProcessingError

logger = logging.getLogger(__name__)

DEFAULT_WORKER_FACTORY = "audio_server.worker_runtime:build_worker"


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


ProgressStage = JobStage | str | enum.Enum
ProgressCallback = Callable[[ProgressStage], None]


class JobProcessor(Protocol):
    """Application adapter around storage, processing, and result mapping."""

    def __call__(
        self,
        job: ClaimedJob,
        progress: ProgressCallback,
        /,
    ) -> ResultPersister | None:
        """Process one original file and return an atomic DB result writer."""


ProcessorFactory = Callable[[], JobProcessor]
ProcessorT = TypeVar("ProcessorT", bound=JobProcessor)
WorkerFactory = Callable[[int], "Worker"]


@dataclass(frozen=True, slots=True)
class WorkerIntervals:
    poll_seconds: float = 1.0
    heartbeat_seconds: float = 30.0
    recovery_seconds: float = 30.0

    def __post_init__(self) -> None:
        if min(self.poll_seconds, self.heartbeat_seconds, self.recovery_seconds) <= 0:
            raise ValueError("worker intervals must be positive")


class _LeaseHeartbeat:
    """Renew a lease during a single long AI stage."""

    def __init__(self, queue: JobQueue, claim: ClaimedJob, interval_seconds: float) -> None:
        self._queue = queue
        self._claim = claim
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._claim_lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-heartbeat-{claim.id}",
            daemon=True,
        )

    @property
    def claim_lost(self) -> bool:
        return self._claim_lost.is_set()

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._queue.heartbeat(self._claim)
            except ClaimLostError:
                self._claim_lost.set()
                logger.warning(
                    "processing lease lost",
                    extra={
                        "recording_id": str(self._claim.recording_id),
                        "job_id": str(self._claim.id),
                    },
                )
                return
            except Exception as error:
                # A transient database outage may recover before the lease
                # expires. Completion remains fenced even if renewals fail.
                logger.warning(
                    "job heartbeat failed",
                    extra={
                        "recording_id": str(self._claim.recording_id),
                        "job_id": str(self._claim.id),
                        "error_type": type(error).__name__,
                    },
                )


class Worker:
    """One serial AI processing worker.

    ``processor_factory`` runs before the first claim. Expensive model loading
    and configuration validation therefore cannot consume a recording's retry
    budget.
    """

    def __init__(
        self,
        queue: JobQueue,
        processor_factory: Callable[[], ProcessorT],
        *,
        intervals: WorkerIntervals | None = None,
    ) -> None:
        self.queue = queue
        self._processor_factory: ProcessorFactory = processor_factory
        self.intervals = intervals or WorkerIntervals()
        if self.intervals.heartbeat_seconds * 2 >= queue.lease_duration.total_seconds():
            raise ValueError("lease duration must exceed two heartbeat intervals")
        self._processor: JobProcessor | None = None

    def initialize(self) -> None:
        if self._processor is None:
            self._processor = self._processor_factory()
            logger.info(
                "processing providers initialized",
                extra={"worker_id": self.queue.worker_id},
            )

    def run(self, stop_event: StopEvent) -> None:
        # Recovery must not depend on AI model availability. A bad model or
        # device configuration can prevent initialization, but expired leases
        # still need to become claimable by another healthy worker.
        self._recover_expired()
        self.initialize()
        next_recovery = time.monotonic() + self.intervals.recovery_seconds

        while not stop_event.is_set():
            now = time.monotonic()
            if now >= next_recovery:
                self._recover_expired()
                next_recovery = now + self.intervals.recovery_seconds

            processed = self.run_once()
            if not processed:
                stop_event.wait(self.intervals.poll_seconds)

    def run_once(self) -> bool:
        """Claim and process at most one job; return whether one was claimed."""

        self.initialize()
        processor = self._processor
        if processor is None:  # pragma: no cover - defensive type narrowing
            raise RuntimeError("worker processor did not initialize")

        claim = self.queue.claim_next()
        if claim is None:
            return False

        logger.info(
            "processing started",
            extra={
                "recording_id": str(claim.recording_id),
                "job_id": str(claim.id),
                "attempt": claim.attempt_count,
            },
        )
        heartbeat = _LeaseHeartbeat(
            self.queue,
            claim,
            self.intervals.heartbeat_seconds,
        )

        def progress(stage: ProgressStage) -> None:
            normalized = _normalize_stage(stage)
            if heartbeat.claim_lost:
                raise ClaimLostError("processing job lease expired during execution")
            # Completion is only durable after result rows and job status commit
            # together below.
            if normalized is JobStage.COMPLETED:
                self.queue.heartbeat(claim)
            else:
                self.queue.advance_stage(claim, normalized)
            logger.info(
                "processing stage updated",
                extra={
                    "recording_id": str(claim.recording_id),
                    "job_id": str(claim.id),
                    "stage": normalized.value,
                },
            )

        try:
            with heartbeat:
                persist_results = processor(claim, progress)
                if heartbeat.claim_lost:
                    raise ClaimLostError("processing job lease expired during execution")
            self.queue.complete(claim, persist_results=persist_results)
        except ClaimLostError:
            logger.warning(
                "discarding result after processing lease loss",
                extra={"recording_id": str(claim.recording_id), "job_id": str(claim.id)},
            )
            return True
        except ProcessingError as error:
            self._record_failure(
                claim,
                JobFailure(
                    code=error.code,
                    error_type=type(error).__name__,
                    message=error.safe_message,
                    retryable=error.retryable,
                    stage=_normalize_stage(error.stage) if error.stage is not None else None,
                ),
            )
            return True
        except Exception as error:
            # Arbitrary exception text can contain transcripts, command output,
            # or credentials. Persist only a generic safe message.
            self._record_failure(
                claim,
                JobFailure(
                    code="unexpected_processing_failure",
                    error_type=type(error).__name__,
                    message="Processing failed unexpectedly.",
                    retryable=True,
                ),
            )
            return True

        logger.info(
            "processing completed",
            extra={"recording_id": str(claim.recording_id), "job_id": str(claim.id)},
        )
        return True

    def _record_failure(self, claim: ClaimedJob, failure: JobFailure) -> None:
        try:
            retry_scheduled = self.queue.fail(claim, failure)
        except ClaimLostError:
            logger.warning(
                "failure ignored after processing lease loss",
                extra={"recording_id": str(claim.recording_id), "job_id": str(claim.id)},
            )
            return
        logger.error(
            "processing failed",
            extra={
                "recording_id": str(claim.recording_id),
                "job_id": str(claim.id),
                "error_code": failure.code,
                "error_type": failure.error_type,
                "retry_scheduled": retry_scheduled,
            },
        )

    def _recover_expired(self) -> None:
        summary = self.queue.recover_expired()
        if summary.requeued or summary.failed:
            logger.warning(
                "expired processing leases recovered",
                extra={
                    "worker_id": self.queue.worker_id,
                    "jobs_requeued": summary.requeued,
                    "jobs_failed": summary.failed,
                },
            )


def _normalize_stage(stage: ProgressStage) -> JobStage:
    if isinstance(stage, JobStage):
        return stage
    value = stage.value if isinstance(stage, enum.Enum) else stage
    return JobStage(value)


def _load_worker_factory(path: str) -> WorkerFactory:
    module_name, separator, attribute_name = path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("worker factory must use 'module:callable' syntax")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not callable(factory):
        raise TypeError("worker factory target is not callable")
    return cast(WorkerFactory, factory)


def _worker_process_main(index: int, stop_event: StopEvent, factory_path: str) -> None:
    failed = False
    try:
        factory = _load_worker_factory(factory_path)
        worker = factory(index)
        worker.run(stop_event)
    except ProcessingError as error:
        # Never attach exc_info or interpolate exception text here. Provider
        # causes can contain credentials, local paths, or model-service output.
        logger.critical(
            "worker process failed",
            extra={
                "worker_index": index,
                "error_code": error.code,
                "error_type": type(error).__name__,
            },
        )
        failed = True
    except Exception as error:
        logger.critical(
            "worker process failed",
            extra={
                "worker_index": index,
                "error_code": "unexpected_worker_failure",
                "error_type": type(error).__name__,
            },
        )
        failed = True

    # Raising outside the except block avoids retaining or rendering the
    # original exception context in multiprocessing's child bootstrap.
    if failed:
        raise SystemExit(1)


def run_supervisor(
    *,
    worker_count: int,
    factory_path: str = DEFAULT_WORKER_FACTORY,
    shutdown_timeout: float = 30.0,
) -> int:
    """Run a bounded number of spawned worker processes until signalled."""

    if not 1 <= worker_count <= 32:
        raise ValueError("worker_count must be between 1 and 32")
    if shutdown_timeout <= 0:
        raise ValueError("shutdown_timeout must be positive")

    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    previous_handlers: dict[signal.Signals, Any] = {}

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop_event.set()

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[handled_signal] = signal.signal(handled_signal, request_stop)

    processes = [
        context.Process(
            target=_worker_process_main,
            args=(index, stop_event, factory_path),
            name=f"audio-worker-{index}",
        )
        for index in range(worker_count)
    ]
    try:
        for process in processes:
            process.start()
        logger.info("worker supervisor started", extra={"worker_count": worker_count})

        exit_code = 0
        while not stop_event.wait(0.5):
            exited = next(
                (process for process in processes if process.exitcode is not None),
                None,
            )
            if exited is not None:
                logger.error(
                    "worker process exited unexpectedly",
                    extra={"worker_name": exited.name, "exit_code": exited.exitcode},
                )
                exit_code = 1
                stop_event.set()
        deadline = time.monotonic() + shutdown_timeout
        for process in processes:
            remaining = max(0.0, deadline - time.monotonic())
            process.join(timeout=remaining)
        for process in processes:
            if process.is_alive():
                logger.warning(
                    "terminating unresponsive worker",
                    extra={"worker_name": process.name},
                )
                process.terminate()
                process.join(timeout=5)
        return exit_code
    finally:
        stop_event.set()
        for handled_signal, handler in previous_handlers.items():
            signal.signal(handled_signal, handler)


def make_worker_id(index: int) -> str:
    """Build a non-secret identifier useful in operational logs and leases."""

    return f"{socket.gethostname()}-{os.getpid()}-{index}"


def queue_from_settings(session_factory: Callable[[], Session], *, worker_id: str) -> JobQueue:
    """Create queue policy from validated application settings."""

    settings = get_settings()
    return JobQueue(
        session_factory,
        worker_id=worker_id,
        lease_duration=timedelta(seconds=settings.job_lease_seconds),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=settings.retry_base_seconds),
            max_delay=timedelta(seconds=settings.retry_max_seconds),
        ),
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    raise SystemExit(run_supervisor(worker_count=settings.processing_workers))
