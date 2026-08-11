from __future__ import annotations

import pytest

from audio_server.processing.contracts import (
    DiarizationResult,
    SpeakerTurn,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
)
from audio_server.processing.merge import UNKNOWN_SPEAKER, merge_transcript


def _transcription(
    *segments: TranscriptionSegment,
    language: str | None = "ja",
) -> TranscriptionResult:
    return TranscriptionResult(segments=segments, language=language)


def _diarization(
    *exclusive: SpeakerTurn,
    regular: tuple[SpeakerTurn, ...] | None = None,
) -> DiarizationResult:
    return DiarizationResult(
        exclusive_turns=exclusive,
        regular_turns=regular if regular is not None else exclusive,
    )


def test_assigns_words_by_overlap_and_inherits_speaker_for_punctuation() -> None:
    transcription = _transcription(
        TranscriptionSegment(
            start=0,
            end=2,
            text="Hello, world",
            words=(
                WordTiming(0, 0.9, " Hello", probability=0.8),
                WordTiming(1.0, 1.05, ",", probability=0.9),
                WordTiming(1.1, 2.0, " world", probability=1.0),
            ),
        ),
        language="en",
    )
    diarization = _diarization(
        SpeakerTurn(0, 1.0, "SPEAKER_00"),
        SpeakerTurn(1.0, 2.0, "SPEAKER_01"),
    )

    merged = merge_transcript(transcription, diarization)

    assert [(item.sequence, item.speaker_label, item.text) for item in merged] == [
        (0, "SPEAKER_00", "Hello,"),
        (1, "SPEAKER_01", "world"),
    ]
    assert merged[0].confidence == pytest.approx(0.85)
    assert merged[1].language == "en"


def test_equal_overlap_tie_prefers_midpoint_then_earlier_turn() -> None:
    transcription = _transcription(
        TranscriptionSegment(
            start=0,
            end=2,
            text="word",
            words=(WordTiming(0, 2, "word"),),
        )
    )
    diarization = _diarization(
        SpeakerTurn(0, 1, "SPEAKER_00"),
        SpeakerTurn(1, 2, "SPEAKER_01"),
    )

    assert merge_transcript(transcription, diarization)[0].speaker_label == "SPEAKER_00"


def test_largest_overlap_wins_even_when_another_turn_contains_midpoint() -> None:
    transcription = _transcription(
        TranscriptionSegment(
            start=0,
            end=3,
            text="word",
            words=(WordTiming(0, 3, "word"),),
        )
    )
    diarization = _diarization(
        SpeakerTurn(0, 1, "SPEAKER_00"),
        SpeakerTurn(0.75, 3, "SPEAKER_01"),
    )

    assert merge_transcript(transcription, diarization)[0].speaker_label == "SPEAKER_01"


def test_nearest_turn_is_used_only_within_tolerance() -> None:
    near = _transcription(TranscriptionSegment(start=1.1, end=1.2, text="near"))
    far = _transcription(TranscriptionSegment(start=2, end=2.1, text="far"))
    diarization = _diarization(SpeakerTurn(0, 1, "SPEAKER_00"))

    assert merge_transcript(near, diarization)[0].speaker_label == "SPEAKER_00"
    assert merge_transcript(far, diarization)[0].speaker_label == UNKNOWN_SPEAKER


def test_regular_turns_mark_overlap_while_exclusive_turn_selects_primary_speaker() -> None:
    transcription = _transcription(
        TranscriptionSegment(
            start=1,
            end=2,
            text="同時発話",
            words=(WordTiming(1, 2, "同時発話"),),
        )
    )
    diarization = _diarization(
        SpeakerTurn(1, 2, "SPEAKER_00"),
        regular=(
            SpeakerTurn(1, 2, "SPEAKER_00"),
            SpeakerTurn(1.2, 1.8, "SPEAKER_01"),
        ),
    )

    merged = merge_transcript(transcription, diarization)

    assert merged[0].speaker_label == "SPEAKER_00"
    assert merged[0].has_overlap is True


def test_segment_level_fallback_uses_maximum_overlap() -> None:
    transcription = _transcription(
        TranscriptionSegment(start=0, end=4, text="word timestamps unavailable")
    )
    diarization = _diarization(
        SpeakerTurn(0, 1, "SPEAKER_00"),
        SpeakerTurn(1, 4, "SPEAKER_01"),
    )

    merged = merge_transcript(transcription, diarization)

    assert len(merged) == 1
    assert merged[0].speaker_label == "SPEAKER_01"
    assert merged[0].text == "word timestamps unavailable"


def test_no_diarization_uses_unknown_speaker() -> None:
    transcription = _transcription(TranscriptionSegment(start=0, end=1, text="こんにちは"))

    merged = merge_transcript(transcription, _diarization())

    assert merged[0].speaker_label == UNKNOWN_SPEAKER


def test_output_is_chronological_and_sequence_is_stable() -> None:
    transcription = _transcription(
        TranscriptionSegment(start=2, end=3, text="second"),
        TranscriptionSegment(start=0, end=1, text="first"),
        language="en",
    )

    merged = merge_transcript(transcription, _diarization())

    assert [(item.sequence, item.text) for item in merged] == [
        (0, "first"),
        (1, "second"),
    ]
