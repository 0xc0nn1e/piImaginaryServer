from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from audio_server.processing.audio import AudioProcessor
from audio_server.processing.contracts import ProcessingStage
from audio_server.processing.diarization import (
    PyannoteDiarizationProvider,
    PyannoteSettings,
)
from audio_server.processing.errors import ProcessingError
from audio_server.processing.transcription import (
    FasterWhisperProvider,
    FasterWhisperSettings,
)

_FORMAT_WHITELIST = "aac,flac,matroska,mov,ogg,wav"


def _probe_payload(codec: str, format_name: str) -> dict[str, object]:
    return {
        "streams": [
            {
                "codec_name": codec,
                "sample_rate": "48000",
                "channels": 2,
            }
        ],
        "format": {"format_name": format_name, "duration": "12.5"},
    }


def test_audio_processor_uses_argument_list_and_parses_supported_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "client name.flac"
    source.write_bytes(b"original")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert "shell" not in kwargs
        payload = _probe_payload("flac", "flac")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    probe = AudioProcessor().probe(source)

    assert probe.duration_seconds == 12.5
    assert probe.mime_type == "audio/flac"
    assert probe.preferred_extension == ".flac"
    assert commands[0][-1] == str(source)
    assert commands[0][commands[0].index("-protocol_whitelist") + 1] == "file"
    assert commands[0][commands[0].index("-format_whitelist") + 1] == _FORMAT_WHITELIST
    assert "hls" not in _FORMAT_WHITELIST
    assert "concat" not in _FORMAT_WHITELIST


def test_audio_normalization_uses_allowlists_and_private_new_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "original.flac"
    source.write_bytes(b"original")
    destination = tmp_path / "work" / "processing.wav"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output_descriptor = kwargs["stdout"]
        assert isinstance(output_descriptor, int)
        os.write(output_descriptor, b"RIFF-secure-derived-audio")
        return subprocess.CompletedProcess(command, 0, None, "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    AudioProcessor().normalize(source, destination)

    command = commands[0]
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert command[command.index("-format_whitelist") + 1] == _FORMAT_WHITELIST
    assert command[-3:] == ["-f", "wav", "pipe:1"]
    assert "-y" not in command
    assert str(destination) not in command
    assert destination.read_bytes() == b"RIFF-secure-derived-audio"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("existing_kind", ["file", "symlink"])
def test_audio_normalization_never_follows_or_overwrites_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_kind: str,
) -> None:
    source = tmp_path / "original.flac"
    source.write_bytes(b"original")
    destination = tmp_path / "processing.wav"
    protected = tmp_path / "protected.wav"
    protected.write_bytes(b"protected")
    if existing_kind == "file":
        destination.write_bytes(b"existing")
    else:
        destination.symlink_to(protected)

    def must_not_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise AssertionError("ffmpeg must not run for an existing destination")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    with pytest.raises(ProcessingError) as caught:
        AudioProcessor().normalize(source, destination)

    assert caught.value.code == "audio_destination_exists"
    assert protected.read_bytes() == b"protected"
    if existing_kind == "file":
        assert destination.read_bytes() == b"existing"


def test_audio_probe_failure_never_exposes_ffprobe_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "original.m4a"
    source.write_bytes(b"invalid")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 1, "", "private/path sensitive-detail")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProcessingError) as caught:
        AudioProcessor().probe(source)

    assert caught.value.code == "audio_validation_failed"
    assert caught.value.stage is ProcessingStage.PREPROCESSING
    assert "private" not in caught.value.safe_message
    assert "sensitive-detail" not in caught.value.safe_message


@pytest.mark.parametrize(
    ("codec", "format_name", "expected_mime", "expected_extension"),
    [
        ("pcm_s16le", "wav", "audio/wav", ".wav"),
        ("flac", "flac", "audio/flac", ".flac"),
        ("aac", "aac", "audio/aac", ".aac"),
        (
            "aac",
            "mov,mp4,m4a,3gp,3g2,mj2",
            "audio/mp4",
            ".m4a",
        ),
        ("opus", "ogg", "audio/ogg", ".opus"),
        ("opus", "matroska,webm", "audio/webm", ".webm"),
    ],
)
def test_audio_probe_accepts_only_documented_codec_container_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codec: str,
    format_name: str,
    expected_mime: str,
    expected_extension: str,
) -> None:
    source = tmp_path / "upload.part"
    source.write_bytes(b"media")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(_probe_payload(codec, format_name)),
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    probe = AudioProcessor().probe(source)

    assert probe.mime_type == expected_mime
    assert probe.preferred_extension == expected_extension


@pytest.mark.parametrize(
    ("codec", "format_name"),
    [
        ("pcm_s16le", "mov,mp4,m4a,3gp,3g2,mj2"),
        ("flac", "ogg"),
        ("aac", "ogg"),
        ("opus", "mov,mp4,m4a,3gp,3g2,mj2"),
        ("mp3", "wav"),
        ("opus", "matroska"),
    ],
)
def test_audio_probe_rejects_codec_container_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codec: str,
    format_name: str,
) -> None:
    source = tmp_path / "upload.part"
    source.write_bytes(b"media")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(_probe_payload(codec, format_name)),
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProcessingError) as caught:
        AudioProcessor().probe(source)

    assert caught.value.code == "audio_validation_failed"


def test_faster_whisper_generator_is_fully_materialized_and_mapped() -> None:
    class FakeModel:
        def transcribe(self, audio_path: str, **kwargs: Any) -> tuple[Any, Any]:
            assert audio_path == "processing.wav"
            assert kwargs["word_timestamps"] is True

            def segments() -> Any:
                yield SimpleNamespace(
                    start=0,
                    end=1.5,
                    text=" hello",
                    avg_logprob=-0.25,
                    no_speech_prob=0.1,
                    words=[
                        SimpleNamespace(
                            start=0,
                            end=1.5,
                            word=" hello",
                            probability=0.92,
                        )
                    ],
                )

            info = SimpleNamespace(language="en", language_probability=0.97)
            return segments(), info

    provider = FasterWhisperProvider(FasterWhisperSettings())
    provider._model = FakeModel()

    result = provider.transcribe(Path("processing.wav"))

    assert result.language == "en"
    assert result.language_probability == 0.97
    assert result.segments[0].words[0].probability == 0.92
    assert result.segments[0].average_log_probability == -0.25


@dataclass(frozen=True)
class _FakeTimelineSegment:
    start: float
    end: float


class _FakeAnnotation:
    def __init__(self, turns: list[tuple[float, float, str]]) -> None:
        self._turns = turns

    def itertracks(self, *, yield_label: bool) -> Any:
        assert yield_label is True
        for start, end, label in self._turns:
            yield _FakeTimelineSegment(start, end), "track", label


def test_pyannote_maps_exclusive_and_regular_timelines_to_same_anonymous_labels() -> None:
    output = SimpleNamespace(
        speaker_diarization=_FakeAnnotation([(0, 2, "raw-z"), (1, 1.5, "raw-a")]),
        exclusive_speaker_diarization=_FakeAnnotation([(0, 1, "raw-z"), (1, 2, "raw-a")]),
    )

    class FakePipeline:
        def __call__(self, audio_path: str) -> Any:
            assert audio_path == "processing.wav"
            return output

    provider = PyannoteDiarizationProvider(PyannoteSettings())
    provider._pipeline = FakePipeline()

    result = provider.diarize(Path("processing.wav"))

    assert [turn.speaker_label for turn in result.exclusive_turns] == [
        "SPEAKER_01",
        "SPEAKER_00",
    ]
    assert [turn.speaker_label for turn in result.regular_turns] == [
        "SPEAKER_01",
        "SPEAKER_00",
    ]
