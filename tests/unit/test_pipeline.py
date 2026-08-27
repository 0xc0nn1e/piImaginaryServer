from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from audio_server.processing.analysis import (
    DisabledAnalysisProvider,
    DisabledTranslationProvider,
)
from audio_server.processing.contracts import (
    AnalysisResult,
    AnalysisStatus,
    AudioProbe,
    DiarizationResult,
    MergedTranscriptSegment,
    ProcessingStage,
    SpeakerTurn,
    TranscriptionResult,
    TranscriptionSegment,
    TranslationResult,
    WordTiming,
)
from audio_server.processing.errors import ProcessingError, RetryableProcessingError
from audio_server.processing.pipeline import ProcessingPipeline


class FakeAudioProcessor:
    def __init__(self) -> None:
        self.probed: Path | None = None
        self.normalized: tuple[Path, Path] | None = None

    def probe(self, source: Path) -> AudioProbe:
        self.probed = source
        return AudioProbe(
            duration_seconds=2,
            codec_name="flac",
            format_name="flac",
            sample_rate=48_000,
            channels=2,
            mime_type="audio/flac",
            preferred_extension=".flac",
        )

    def normalize(self, source: Path, destination: Path) -> None:
        self.normalized = (source, destination)


class FakeTranscriptionProvider:
    def __init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        assert audio_path.name == "processing.wav"
        return TranscriptionResult(
            segments=(
                TranscriptionSegment(
                    start=0,
                    end=2,
                    text="hello",
                    words=(WordTiming(0, 2, "hello", probability=0.9),),
                ),
            ),
            language="en",
            language_probability=0.95,
        )


class FakeDiarizationProvider:
    def __init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def diarize(self, audio_path: Path) -> DiarizationResult:
        assert audio_path.name == "processing.wav"
        turn = SpeakerTurn(0, 2, "SPEAKER_00")
        return DiarizationResult(exclusive_turns=(turn,), regular_turns=(turn,))


class FailingAnalysisProvider:
    @property
    def name(self) -> str:
        return "fake-llm"

    def analyze(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> AnalysisResult:
        del recording_id, segments
        raise RuntimeError("provider output must not be exposed")


class FailingTranslationProvider:
    @property
    def name(self) -> str:
        return "fake-llm"

    def translate(
        self,
        recording_id: str,
        segments: Sequence[MergedTranscriptSegment],
    ) -> TranslationResult:
        del recording_id, segments
        raise RuntimeError("provider output must not be exposed")


def _pipeline(
    *,
    audio: FakeAudioProcessor | None = None,
    transcription: object | None = None,
    diarization: object | None = None,
    analysis: object | None = None,
    translation: object | None = None,
) -> ProcessingPipeline:
    return ProcessingPipeline(
        audio_processor=audio or FakeAudioProcessor(),
        transcription_provider=transcription or FakeTranscriptionProvider(),  # type: ignore[arg-type]
        diarization_provider=diarization or FakeDiarizationProvider(),  # type: ignore[arg-type]
        analysis_provider=analysis or DisabledAnalysisProvider(),  # type: ignore[arg-type]
        translation_provider=translation or DisabledTranslationProvider(),  # type: ignore[arg-type]
    )


def test_pipeline_runs_all_stages_with_injected_providers(tmp_path: Path) -> None:
    stages: list[ProcessingStage] = []
    audio = FakeAudioProcessor()
    pipeline = _pipeline(audio=audio)
    source = tmp_path / "original.flac"

    result = pipeline.run(
        recording_id="recording-id",
        source_path=source,
        work_dir=tmp_path / "work",
        stage_callback=stages.append,
    )

    assert stages == list(ProcessingStage)
    assert audio.probed == source
    assert audio.normalized == (source, tmp_path / "work" / "processing.wav")
    assert result.recording_id == "recording-id"
    assert result.transcription_language == "en"
    assert result.transcription_language_probability == 0.95
    assert [(segment.speaker_label, segment.text) for segment in result.transcript] == [
        ("SPEAKER_00", "hello")
    ]
    assert result.analysis.status is AnalysisStatus.SKIPPED


def test_analysis_failure_does_not_discard_completed_transcript(tmp_path: Path) -> None:
    stages: list[ProcessingStage] = []
    pipeline = _pipeline(analysis=FailingAnalysisProvider())

    result = pipeline.run(
        recording_id="recording-id",
        source_path=tmp_path / "original.flac",
        work_dir=tmp_path / "work",
        stage_callback=stages.append,
    )

    assert result.transcript[0].text == "hello"
    assert result.analysis.status is AnalysisStatus.FAILED
    assert result.analysis.error_code == "analysis_failed"
    assert result.analysis.error_message == "Transcript analysis failed."
    assert stages[-1] is ProcessingStage.COMPLETED


def test_unknown_provider_failure_is_safe_and_retryable(tmp_path: Path) -> None:
    class BrokenTranscriptionProvider:
        def transcribe(self, audio_path: Path) -> TranscriptionResult:
            del audio_path
            raise RuntimeError("private provider details")

    pipeline = _pipeline(transcription=BrokenTranscriptionProvider())

    with pytest.raises(ProcessingError) as caught:
        pipeline.run(
            recording_id="recording-id",
            source_path=tmp_path / "original.flac",
            work_dir=tmp_path / "work",
        )

    assert caught.value.code == "transcription_failed"
    assert caught.value.stage is ProcessingStage.TRANSCRIBING
    assert caught.value.retryable is True
    assert "private" not in caught.value.safe_message


def test_declared_provider_failure_keeps_retry_classification(tmp_path: Path) -> None:
    class UnavailableDiarizationProvider:
        def diarize(self, audio_path: Path) -> DiarizationResult:
            del audio_path
            raise RetryableProcessingError(
                code="gpu_unavailable",
                safe_message="The processing device is temporarily unavailable.",
            )

    pipeline = _pipeline(diarization=UnavailableDiarizationProvider())

    with pytest.raises(ProcessingError) as caught:
        pipeline.run(
            recording_id="recording-id",
            source_path=tmp_path / "original.flac",
            work_dir=tmp_path / "work",
        )

    assert caught.value.code == "gpu_unavailable"
    assert caught.value.stage is ProcessingStage.DIARIZING
    assert caught.value.retryable is True


def test_stage_callback_failure_is_not_wrapped(tmp_path: Path) -> None:
    class LeaseLost(RuntimeError):
        pass

    def callback(stage: ProcessingStage) -> None:
        if stage is ProcessingStage.DIARIZING:
            raise LeaseLost("claim token is stale")

    with pytest.raises(LeaseLost):
        _pipeline().run(
            recording_id="recording-id",
            source_path=tmp_path / "original.flac",
            work_dir=tmp_path / "work",
            stage_callback=callback,
        )


def test_load_providers_warms_optional_heavy_dependencies() -> None:
    transcription = FakeTranscriptionProvider()
    diarization = FakeDiarizationProvider()
    pipeline = _pipeline(transcription=transcription, diarization=diarization)

    pipeline.load_providers()

    assert transcription.loaded is True
    assert diarization.loaded is True


def test_translation_failure_does_not_discard_completed_transcript(tmp_path: Path) -> None:
    stages: list[ProcessingStage] = []
    pipeline = _pipeline(translation=FailingTranslationProvider())

    result = pipeline.run(
        recording_id="recording-id",
        source_path=tmp_path / "original.flac",
        work_dir=tmp_path / "work",
        stage_callback=stages.append,
    )

    # Translation is optional. Letting it take the transcript down would lose
    # hours of transcription over an LLM that returned the wrong shape.
    assert result.transcript[0].text == "hello"
    assert result.translation is not None
    assert result.translation.status is AnalysisStatus.FAILED
    assert result.translation.error_code == "translation_failed"
    assert "provider output" not in (result.translation.error_message or "")
    assert stages[-1] is ProcessingStage.COMPLETED
