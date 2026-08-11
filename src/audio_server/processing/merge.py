"""Deterministic reconciliation of Whisper and diarization timelines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

from audio_server.processing.contracts import (
    DiarizationResult,
    MergedTranscriptSegment,
    SpeakerTurn,
    TranscriptionResult,
    WordTiming,
)

UNKNOWN_SPEAKER = "SPEAKER_UNKNOWN"
DEFAULT_NEAREST_TURN_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class _AssignedWord:
    word: WordTiming
    speaker_label: str
    has_overlap: bool


class TimestampTranscriptMerger:
    """Replaceable word-overlap heuristic used by the MVP pipeline."""

    def __init__(self, *, nearest_turn_seconds: float = DEFAULT_NEAREST_TURN_SECONDS):
        if nearest_turn_seconds < 0:
            raise ValueError("nearest turn tolerance cannot be negative")
        self._nearest_turn_seconds = nearest_turn_seconds

    def merge(
        self,
        transcription: TranscriptionResult,
        diarization: DiarizationResult,
    ) -> tuple[MergedTranscriptSegment, ...]:
        primary_turns = tuple(
            sorted(
                diarization.primary_turns,
                key=lambda turn: (turn.start, turn.end, turn.speaker_label),
            )
        )
        regular_turns = tuple(
            sorted(
                diarization.regular_turns,
                key=lambda turn: (turn.start, turn.end, turn.speaker_label),
            )
        )
        transcript_segments = sorted(
            enumerate(transcription.segments),
            key=lambda item: (item[1].start, item[1].end, item[0]),
        )

        merged: list[MergedTranscriptSegment] = []
        for _original_index, segment in transcript_segments:
            segment_language = segment.language or transcription.language
            words = tuple(word for word in segment.words if word.text.strip())
            if words:
                assigned = self._assign_words(words, primary_turns, regular_turns)
                for group in _group_by_speaker(assigned):
                    text = _join_word_text(group).strip()
                    if not text:
                        continue
                    probabilities = [
                        item.word.probability for item in group if item.word.probability is not None
                    ]
                    merged.append(
                        MergedTranscriptSegment(
                            sequence=len(merged),
                            speaker_label=group[0].speaker_label,
                            start=min(item.word.start for item in group),
                            end=max(item.word.end for item in group),
                            text=text,
                            language=segment_language,
                            confidence=fmean(probabilities) if probabilities else None,
                            has_overlap=any(item.has_overlap for item in group),
                        )
                    )
                continue

            text = segment.text.strip()
            if not text:
                continue
            merged.append(
                MergedTranscriptSegment(
                    sequence=len(merged),
                    speaker_label=_choose_speaker(
                        segment.start,
                        segment.end,
                        primary_turns,
                        nearest_turn_seconds=self._nearest_turn_seconds,
                    ),
                    start=segment.start,
                    end=segment.end,
                    text=text,
                    language=segment_language,
                    has_overlap=_has_simultaneous_speech(segment.start, segment.end, regular_turns),
                )
            )
        return tuple(merged)

    def _assign_words(
        self,
        words: Sequence[WordTiming],
        primary_turns: Sequence[SpeakerTurn],
        regular_turns: Sequence[SpeakerTurn],
    ) -> tuple[_AssignedWord, ...]:
        ordered = sorted(
            enumerate(words),
            key=lambda item: (item[1].start, item[1].end, item[0]),
        )
        assigned: list[_AssignedWord] = []
        for _original_index, word in ordered:
            if _is_punctuation_only(word.text) and assigned:
                speaker_label = assigned[-1].speaker_label
                has_overlap = assigned[-1].has_overlap
            else:
                speaker_label = _choose_speaker(
                    word.start,
                    word.end,
                    primary_turns,
                    nearest_turn_seconds=self._nearest_turn_seconds,
                )
                has_overlap = _has_simultaneous_speech(word.start, word.end, regular_turns)
            assigned.append(
                _AssignedWord(
                    word=word,
                    speaker_label=speaker_label,
                    has_overlap=has_overlap,
                )
            )
        return tuple(assigned)


def merge_transcript(
    transcription: TranscriptionResult,
    diarization: DiarizationResult,
    *,
    nearest_turn_seconds: float = DEFAULT_NEAREST_TURN_SECONDS,
) -> tuple[MergedTranscriptSegment, ...]:
    """Functional entry point convenient for tests and simple integrations."""

    return TimestampTranscriptMerger(nearest_turn_seconds=nearest_turn_seconds).merge(
        transcription, diarization
    )


def _choose_speaker(
    start: float,
    end: float,
    turns: Sequence[SpeakerTurn],
    *,
    nearest_turn_seconds: float,
) -> str:
    if not turns:
        return UNKNOWN_SPEAKER

    midpoint = (start + end) / 2
    overlaps = [(turn, _overlap(start, end, turn.start, turn.end)) for turn in turns]
    positive = [(turn, overlap) for turn, overlap in overlaps if overlap > 0]
    if positive:
        positive.sort(
            key=lambda item: (
                -item[1],
                -int(_contains(item[0], midpoint)),
                item[0].start,
                item[0].end,
                item[0].speaker_label,
            )
        )
        return positive[0][0].speaker_label

    containing = [turn for turn in turns if _contains(turn, midpoint)]
    if containing:
        return min(
            containing,
            key=lambda turn: (turn.start, turn.end, turn.speaker_label),
        ).speaker_label

    nearest = sorted(
        ((_distance_to_interval(start, end, turn), turn) for turn in turns),
        key=lambda item: (
            item[0],
            item[1].start,
            item[1].end,
            item[1].speaker_label,
        ),
    )
    distance, turn = nearest[0]
    if distance <= nearest_turn_seconds:
        return turn.speaker_label
    return UNKNOWN_SPEAKER


def _has_simultaneous_speech(
    start: float,
    end: float,
    regular_turns: Sequence[SpeakerTurn],
) -> bool:
    if end == start:
        labels = {turn.speaker_label for turn in regular_turns if _contains(turn, start)}
        return len(labels) > 1

    relevant = [turn for turn in regular_turns if _overlap(start, end, turn.start, turn.end) > 0]
    for index, first in enumerate(relevant):
        for second in relevant[index + 1 :]:
            if first.speaker_label == second.speaker_label:
                continue
            shared_start = max(start, first.start, second.start)
            shared_end = min(end, first.end, second.end)
            if shared_end > shared_start:
                return True
    return False


def _group_by_speaker(
    assigned: Sequence[_AssignedWord],
) -> tuple[tuple[_AssignedWord, ...], ...]:
    groups: list[list[_AssignedWord]] = []
    for item in assigned:
        if not groups or groups[-1][-1].speaker_label != item.speaker_label:
            groups.append([item])
        else:
            groups[-1].append(item)
    return tuple(tuple(group) for group in groups)


def _join_word_text(words: Sequence[_AssignedWord]) -> str:
    text = ""
    for item in words:
        next_text = item.word.text
        if text and _needs_ascii_separator(text, next_text):
            text += " "
        text += next_text
    return text


def _needs_ascii_separator(existing: str, next_text: str) -> bool:
    if not existing or not next_text or existing[-1].isspace() or next_text[0].isspace():
        return False
    left = existing[-1]
    right = next_text[0]
    return left.isascii() and right.isascii() and left.isalnum() and right.isalnum()


def _is_punctuation_only(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not any(character.isalnum() for character in stripped)


def _overlap(
    first_start: float,
    first_end: float,
    second_start: float,
    second_end: float,
) -> float:
    return max(0.0, min(first_end, second_end) - max(first_start, second_start))


def _contains(turn: SpeakerTurn, timestamp: float) -> bool:
    return turn.start <= timestamp <= turn.end


def _distance_to_interval(start: float, end: float, turn: SpeakerTurn) -> float:
    if turn.end < start:
        return start - turn.end
    if turn.start > end:
        return turn.start - end
    return 0.0
