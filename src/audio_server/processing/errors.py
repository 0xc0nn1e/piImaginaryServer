"""Safe processing errors that a durable worker can classify."""

from __future__ import annotations

from audio_server.processing.contracts import ProcessingStage


class ProcessingError(Exception):
    """An expected failure safe to persist and expose through the status API."""

    def __init__(
        self,
        *,
        code: str,
        safe_message: str,
        retryable: bool,
        stage: ProcessingStage | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.stage = stage

    def at_stage(self, stage: ProcessingStage) -> ProcessingError:
        """Attach the active stage when a provider raised a generic error."""

        if self.stage is None:
            self.stage = stage
        return self


class PermanentProcessingError(ProcessingError):
    def __init__(
        self,
        *,
        code: str,
        safe_message: str,
        stage: ProcessingStage | None = None,
    ) -> None:
        super().__init__(
            code=code,
            safe_message=safe_message,
            retryable=False,
            stage=stage,
        )


class RetryableProcessingError(ProcessingError):
    def __init__(
        self,
        *,
        code: str,
        safe_message: str,
        stage: ProcessingStage | None = None,
    ) -> None:
        super().__init__(
            code=code,
            safe_message=safe_message,
            retryable=True,
            stage=stage,
        )


class ProviderConfigurationError(PermanentProcessingError):
    """A worker-level dependency/model/device configuration failure.

    Workers should call provider ``load`` methods before claiming a job so these
    failures do not consume the retry budget of every queued recording.
    """
