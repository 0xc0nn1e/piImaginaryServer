from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import audio_server.jobs.worker as worker_module
from audio_server.db.models import Base, JobStatus, ProcessingJob, Recording, RecordingStatus
from audio_server.jobs.queue import (
    ClaimLostError,
    JobFailure,
    JobQueue,
    RecoverySummary,
    RetryPolicy,
    create_processing_job,
)
from audio_server.jobs.worker import Worker, WorkerIntervals
from audio_server.processing.errors import ProviderConfigurationError


def test_worker_module_invocation_calls_main() -> None:
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "PROCESSING_WORKERS": "0",
    }

    result = subprocess.run(
        [sys.executable, "-m", "audio_server.jobs.worker"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("attempt", "expected_seconds"),
    [(1, 30), (2, 60), (3, 120), (6, 900), (20, 900)],
)
def test_retry_policy_uses_bounded_exponential_backoff(
    attempt: int,
    expected_seconds: int,
) -> None:
    policy = RetryPolicy(
        base_delay=timedelta(seconds=30),
        max_delay=timedelta(seconds=900),
    )

    assert policy.delay_after(attempt) == timedelta(seconds=expected_seconds)


def test_retry_policy_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=timedelta(0))
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=timedelta(seconds=10), max_delay=timedelta(seconds=5))
    with pytest.raises(ValueError):
        RetryPolicy().delay_after(0)


@pytest.mark.parametrize(
    ("retryable", "attempt", "maximum", "expected"),
    [
        (True, 1, 3, True),
        (True, 2, 3, True),
        (True, 3, 3, False),
        (False, 1, 3, False),
    ],
)
def test_retry_policy_respects_error_classification_and_attempt_limit(
    retryable: bool,
    attempt: int,
    maximum: int,
    expected: bool,
) -> None:
    assert (
        RetryPolicy.can_retry(
            retryable=retryable,
            attempt_count=attempt,
            max_attempts=maximum,
        )
        is expected
    )


class _QueueThatMustNotClaim:
    worker_id = "test-worker"
    lease_duration = timedelta(seconds=10)

    def __init__(self) -> None:
        self.claim_calls = 0

    def claim_next(self):  # type: ignore[no-untyped-def]
        self.claim_calls += 1
        raise AssertionError("provider initialization must happen before claim")


def test_worker_initializes_providers_before_claiming() -> None:
    queue = _QueueThatMustNotClaim()

    def broken_factory():  # type: ignore[no-untyped-def]
        raise RuntimeError("model configuration is invalid")

    worker = Worker(
        cast(JobQueue, queue),
        broken_factory,
        intervals=WorkerIntervals(
            poll_seconds=0.01,
            heartbeat_seconds=1,
            recovery_seconds=1,
        ),
    )

    with pytest.raises(RuntimeError, match="model configuration is invalid"):
        worker.run_once()
    assert queue.claim_calls == 0


class _RecoveryOrderQueue:
    worker_id = "test-worker"
    lease_duration = timedelta(seconds=10)

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.claim_calls = 0

    def recover_expired(self) -> RecoverySummary:
        self.events.append("recover")
        return RecoverySummary()

    def claim_next(self):  # type: ignore[no-untyped-def]
        self.claim_calls += 1
        raise AssertionError("a failed initializer must not claim a job")


def test_worker_recovers_expired_leases_even_when_provider_initialization_fails() -> None:
    events: list[str] = []
    queue = _RecoveryOrderQueue(events)

    def broken_factory():  # type: ignore[no-untyped-def]
        events.append("initialize")
        raise RuntimeError("model configuration is invalid")

    worker = Worker(
        cast(JobQueue, queue),
        broken_factory,
        intervals=WorkerIntervals(
            poll_seconds=0.01,
            heartbeat_seconds=1,
            recovery_seconds=1,
        ),
    )

    with pytest.raises(RuntimeError, match="model configuration is invalid"):
        worker.run(threading.Event())
    assert events == ["recover", "initialize"]
    assert queue.claim_calls == 0


class _BoundaryFailureWorker:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def run(self, stop_event: object) -> None:
        del stop_event
        raise self._error


def test_worker_child_configures_logging_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(log_level="WARNING", log_format="plain"),
    )
    monkeypatch.setattr(
        worker_module,
        "configure_logging",
        lambda level, log_format: calls.append((level, log_format)),
    )

    worker_module._configure_child_logging()

    assert calls == [("WARNING", "plain")]


def test_worker_process_configures_logging_before_loading_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class SuccessfulWorker:
        def run(self, stop_event: object) -> None:
            del stop_event
            events.append("run")

    def load_factory(path: str):  # type: ignore[no-untyped-def]
        del path
        events.append("factory")
        return lambda index: SuccessfulWorker()

    monkeypatch.setattr(
        worker_module,
        "_configure_child_logging",
        lambda: events.append("logging"),
    )
    monkeypatch.setattr(worker_module, "_load_worker_factory", load_factory)

    worker_module._worker_process_main(2, threading.Event(), "unused:factory")

    assert events == ["logging", "factory", "run"]


def _error_with_secret_cause(error: Exception, secret: str) -> Exception:
    try:
        raise RuntimeError(secret)
    except RuntimeError as cause:
        error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_type"),
    [
        (
            ValueError("outer exception text must not be logged"),
            "unexpected_worker_failure",
            "ValueError",
        ),
        (
            ProviderConfigurationError(
                code="provider_configuration_failed",
                safe_message="Provider configuration is invalid.",
            ),
            "provider_configuration_failed",
            "ProviderConfigurationError",
        ),
    ],
)
def test_worker_process_boundary_redacts_exception_chain_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected_code: str,
    expected_type: str,
) -> None:
    secret = "SENTINEL_SECRET_MUST_NOT_APPEAR"
    failing_worker = _BoundaryFailureWorker(_error_with_secret_cause(error, secret))
    monkeypatch.setattr(worker_module, "_configure_child_logging", lambda: None)
    monkeypatch.setattr(
        worker_module,
        "_load_worker_factory",
        lambda path: lambda index: failing_worker,
    )

    with (
        caplog.at_level(logging.CRITICAL, logger=worker_module.__name__),
        pytest.raises(SystemExit) as exit_info,
    ):
        worker_module._worker_process_main(2, threading.Event(), "unused:factory")

    assert exit_info.value.code == 1
    assert exit_info.value.__cause__ is None
    assert exit_info.value.__context__ is None
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == "worker process failed"
    assert record.error_code == expected_code
    assert record.error_type == expected_type
    assert record.exc_info is None
    assert record.exc_text is None
    assert secret not in caplog.text
    assert secret not in repr(record.__dict__)


def test_queue_retry_and_claim_token_fencing_without_external_services() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(UTC)
    recording_id = uuid.uuid4()

    with factory() as session, session.begin():
        session.add(
            Recording(
                id=recording_id,
                device_id="unit-test-device",
                original_filename="sample.flac",
                storage_key=f"recordings/{recording_id}/original.flac",
                mime_type="audio/flac",
                audio_format="flac",
                file_size=1024,
                sha256="a" * 64,
                started_at=now,
                ended_at=now + timedelta(seconds=10),
                duration_seconds=10,
                client_metadata={},
            )
        )
        original_job = create_processing_job(
            session,
            recording_id=recording_id,
            available_at=now,
        )
        job_id = original_job.id

    queue = JobQueue(
        factory,
        worker_id="unit-worker",
        lease_duration=timedelta(seconds=10),
        retry_policy=RetryPolicy(
            base_delay=timedelta(seconds=1),
            max_delay=timedelta(seconds=1),
        ),
    )
    stale_claim = queue.claim_next(now=now)
    assert stale_claim is not None
    assert queue.fail(
        stale_claim,
        JobFailure(
            code="temporary_failure",
            error_type="RetryableProcessingError",
            message="A temporary dependency failure occurred.",
            retryable=True,
        ),
        now=now + timedelta(seconds=1),
    )

    current_claim = queue.claim_next(now=now + timedelta(seconds=2))
    assert current_claim is not None
    assert current_claim.claim_token != stale_claim.claim_token
    with pytest.raises(ClaimLostError):
        queue.complete(stale_claim)
    queue.complete(current_claim)

    with factory() as session:
        job = session.get(ProcessingJob, job_id)
        recording = session.get(Recording, recording_id)
        assert job is not None and recording is not None
        assert job.status is JobStatus.COMPLETED
        assert job.attempt_count == 2
        assert recording.processing_status is RecordingStatus.COMPLETED
