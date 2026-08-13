"""ffprobe validation and ffmpeg normalization."""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audio_server.processing.contracts import AudioProbe, ProcessingStage
from audio_server.processing.errors import (
    PermanentProcessingError,
    ProviderConfigurationError,
    RetryableProcessingError,
)

_INPUT_FORMAT_WHITELIST = "aac,flac,matroska,mov,mp3,ogg,wav"
_INPUT_PROTOCOL_WHITELIST = "file"

_WAV_FORMAT = frozenset({"wav"})
_FLAC_FORMAT = frozenset({"flac"})
_RAW_AAC_FORMAT = frozenset({"aac"})
_MOV_FORMAT = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})
_OGG_FORMAT = frozenset({"ogg"})
_MATROSKA_WEBM_FORMAT = frozenset({"matroska", "webm"})
_MP3_FORMAT = frozenset({"mp3"})


@dataclass(frozen=True, slots=True)
class FFmpegSettings:
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    probe_timeout_seconds: float = 30.0
    conversion_timeout_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.probe_timeout_seconds <= 0 or self.conversion_timeout_seconds <= 0:
            raise ValueError("ffmpeg timeouts must be positive")


class AudioProcessor:
    """Uses subprocess argument lists only; client filenames never enter a shell."""

    def __init__(self, settings: FFmpegSettings | None = None) -> None:
        self._settings = settings or FFmpegSettings()

    def probe(self, source: Path) -> AudioProbe:
        if not source.is_file():
            raise PermanentProcessingError(
                code="audio_missing",
                safe_message="The original audio file is unavailable.",
                stage=ProcessingStage.PREPROCESSING,
            )

        command = [
            self._settings.ffprobe_binary,
            "-v",
            "error",
            "-protocol_whitelist",
            _INPUT_PROTOCOL_WHITELIST,
            "-format_whitelist",
            _INPUT_FORMAT_WHITELIST,
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration:format=format_name,duration",
            "-of",
            "json",
            str(source),
        ]
        result = self._run(
            command,
            timeout=self._settings.probe_timeout_seconds,
            operation="probe",
        )
        if result.returncode != 0:
            raise PermanentProcessingError(
                code="audio_validation_failed",
                safe_message="The uploaded file is not a supported audio recording.",
                stage=ProcessingStage.PREPROCESSING,
            )

        try:
            payload = json.loads(result.stdout)
            return _parse_probe_payload(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PermanentProcessingError(
                code="audio_validation_failed",
                safe_message="The uploaded file has invalid audio metadata.",
                stage=ProcessingStage.PREPROCESSING,
            ) from exc

    def normalize(self, source: Path, destination: Path) -> None:
        source_resolved = source.resolve(strict=True)
        destination_resolved = destination.resolve(strict=False)
        if source_resolved == destination_resolved:
            raise PermanentProcessingError(
                code="unsafe_audio_destination",
                safe_message="The processing destination cannot replace the original audio.",
                stage=ProcessingStage.PREPROCESSING,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        output_descriptor, output_identity = _create_private_output(destination)
        command = [
            self._settings.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            _INPUT_PROTOCOL_WHITELIST,
            "-format_whitelist",
            _INPUT_FORMAT_WHITELIST,
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ]
        try:
            result = self._run(
                command,
                timeout=self._settings.conversion_timeout_seconds,
                operation="convert",
                stdout_descriptor=output_descriptor,
            )
            if result.returncode != 0:
                raise PermanentProcessingError(
                    code="audio_conversion_failed",
                    safe_message="The audio could not be converted for processing.",
                    stage=ProcessingStage.PREPROCESSING,
                )
            os.fsync(output_descriptor)
            output_stat = os.fstat(output_descriptor)
            if output_stat.st_size == 0 or not _path_matches_identity(destination, output_identity):
                raise PermanentProcessingError(
                    code="unsafe_audio_destination",
                    safe_message="The processing destination changed during conversion.",
                    stage=ProcessingStage.PREPROCESSING,
                )
        finally:
            os.close(output_descriptor)

    def _run(
        self,
        command: list[str],
        *,
        timeout: float,
        operation: str,
        stdout_descriptor: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            if stdout_descriptor is not None:
                return subprocess.run(
                    command,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_descriptor,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                )
            return subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ProviderConfigurationError(
                code=f"ff{operation}_unavailable",
                safe_message="The required audio processing tool is not installed.",
                stage=ProcessingStage.PREPROCESSING,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RetryableProcessingError(
                code=f"audio_{operation}_timeout",
                safe_message="Audio processing timed out and may be retried.",
                stage=ProcessingStage.PREPROCESSING,
            ) from exc
        except OSError as exc:
            raise RetryableProcessingError(
                code=f"audio_{operation}_unavailable",
                safe_message="Audio processing is temporarily unavailable.",
                stage=ProcessingStage.PREPROCESSING,
            ) from exc


def _parse_probe_payload(payload: Any) -> AudioProbe:
    if not isinstance(payload, dict):
        raise ValueError("ffprobe output must be an object")
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise ValueError("ffprobe output has no audio stream")

    stream = streams[0]
    format_data = payload.get("format")
    if not isinstance(format_data, dict):
        raise ValueError("ffprobe output has no format")

    codec = str(stream["codec_name"]).lower()
    format_name = str(format_data["format_name"]).lower()
    mime_type, extension = _validated_media_identity(codec, format_name)

    duration_raw = format_data.get("duration", stream.get("duration"))
    if duration_raw is None:
        raise ValueError("audio duration is unavailable")
    duration = float(duration_raw)
    sample_rate = int(stream["sample_rate"])
    channels = int(stream["channels"])
    if not math.isfinite(duration):
        raise ValueError("non-finite duration")
    return AudioProbe(
        duration_seconds=duration,
        codec_name=codec,
        format_name=format_name,
        sample_rate=sample_rate,
        channels=channels,
        mime_type=mime_type,
        preferred_extension=extension,
    )


def _validated_media_identity(codec: str, format_name: str) -> tuple[str, str]:
    formats = frozenset(format_name.split(","))
    if codec.startswith("pcm_") and formats == _WAV_FORMAT:
        return "audio/wav", ".wav"
    if codec == "flac" and formats == _FLAC_FORMAT:
        return "audio/flac", ".flac"
    if codec == "aac":
        if formats == _MOV_FORMAT:
            return "audio/mp4", ".m4a"
        if formats == _RAW_AAC_FORMAT:
            return "audio/aac", ".aac"
    if codec == "opus":
        if formats == _OGG_FORMAT:
            return "audio/ogg", ".opus"
        if formats == _MATROSKA_WEBM_FORMAT:
            return "audio/webm", ".webm"
    if codec == "mp3" and formats == _MP3_FORMAT:
        return "audio/mpeg", ".mp3"
    raise ValueError("unsupported codec and container combination")


def _create_private_output(destination: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise PermanentProcessingError(
            code="audio_destination_exists",
            safe_message="The processing destination already exists.",
            stage=ProcessingStage.PREPROCESSING,
        ) from exc
    except OSError as exc:
        raise RetryableProcessingError(
            code="audio_destination_unavailable",
            safe_message="The processing destination is temporarily unavailable.",
            stage=ProcessingStage.PREPROCESSING,
        ) from exc

    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        created = os.fstat(descriptor)
        return descriptor, (created.st_dev, created.st_ino)
    except OSError as exc:
        os.close(descriptor)
        raise RetryableProcessingError(
            code="audio_destination_unavailable",
            safe_message="The processing destination is temporarily unavailable.",
            stage=ProcessingStage.PREPROCESSING,
        ) from exc


def _path_matches_identity(destination: Path, identity: tuple[int, int]) -> bool:
    try:
        current = os.lstat(destination)
    except OSError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == identity[0]
        and current.st_ino == identity[1]
        and stat.S_IMODE(current.st_mode) == 0o600
    )
