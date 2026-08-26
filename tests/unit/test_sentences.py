"""Sentence grouping decides what a translation is attached to."""

from __future__ import annotations

import pytest

from audio_server.processing.contracts import MergedTranscriptSegment
from audio_server.processing.sentences import group_sentences


def _segment(sequence: int, text: str, speaker: str = "SPEAKER_00") -> MergedTranscriptSegment:
    return MergedTranscriptSegment(
        sequence=sequence,
        speaker_label=speaker,
        start=float(sequence),
        end=float(sequence) + 0.9,
        text=text,
    )


def test_phrases_are_joined_until_a_terminator() -> None:
    segments = [
        _segment(0, "昨日は"),
        _segment(1, "会議に"),
        _segment(2, "行きました。"),
        _segment(3, "とても長かったです。"),
    ]

    sentences = group_sentences(segments)

    assert [(item.start_sequence, item.end_sequence) for item in sentences] == [(0, 2), (3, 3)]
    assert sentences[0].text == "昨日は会議に行きました。"


def test_a_change_of_speaker_always_starts_a_new_sentence() -> None:
    segments = [
        _segment(0, "これは"),
        _segment(1, "そうですね", speaker="SPEAKER_01"),
    ]

    sentences = group_sentences(segments)

    # Without this a reply would be glued onto the line it answers.
    assert [item.speaker_label for item in sentences] == ["SPEAKER_00", "SPEAKER_01"]
    assert [(item.start_sequence, item.end_sequence) for item in sentences] == [(0, 0), (1, 1)]


def test_unpunctuated_speech_is_capped_instead_of_growing_without_end() -> None:
    segments = [_segment(index, "あ" * 30) for index in range(10)]

    sentences = group_sentences(segments, max_chars=100)

    assert len(sentences) > 1
    assert all(len(item.text) <= 120 for item in sentences)
    # Every segment is still covered exactly once, in order.
    covered = [
        sequence
        for item in sentences
        for sequence in range(item.start_sequence, item.end_sequence + 1)
    ]
    assert covered == list(range(10))


@pytest.mark.parametrize("closer", ["」", "』", "）", '"'])
def test_a_terminator_still_counts_behind_a_closing_quote(closer: str) -> None:
    segments = [_segment(0, f"「はい。{closer}"), _segment(1, "次に進みます。")]

    sentences = group_sentences(segments)

    assert len(sentences) == 2


def test_trailing_speech_without_a_terminator_is_still_returned() -> None:
    sentences = group_sentences([_segment(0, "終わりの言葉")])

    assert [item.text for item in sentences] == ["終わりの言葉"]


def test_an_empty_transcript_has_no_sentences() -> None:
    assert group_sentences([]) == ()


def test_max_chars_must_be_positive() -> None:
    with pytest.raises(ValueError):
        group_sentences([_segment(0, "テスト。")], max_chars=0)
