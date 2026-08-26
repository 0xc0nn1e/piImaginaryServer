"""Group Whisper segments into sentences before anything translates them.

Whisper splits on speech pauses, not grammar, so a segment is usually a phrase.
Translating phrase by phrase produces disjointed Cantonese, and asking the model
to decide the boundaries lets it return ranges that do not exist. The server
therefore groups deterministically and the model only ever sees one sentence at
a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from audio_server.processing.contracts import MergedTranscriptSegment

# Japanese and Latin sentence terminators, plus the closing quotes that may
# trail them.
_TERMINATORS = frozenset("。．.!！?？…")
_TRAILING = frozenset("」』）)”’\"'　 ")

MAX_SENTENCE_CHARS = 400


@dataclass(frozen=True, slots=True)
class TranscriptSentence:
    """One sentence and the span of segments it was assembled from."""

    start_sequence: int
    end_sequence: int
    start_time: float
    end_time: float
    speaker_label: str
    text: str

    def __post_init__(self) -> None:
        if self.end_sequence < self.start_sequence:
            raise ValueError("sentence end cannot precede its start")
        if self.end_time < self.start_time:
            raise ValueError("sentence end time cannot precede its start")
        if not self.text.strip():
            raise ValueError("sentence text cannot be empty")


def group_sentences(
    segments: Sequence[MergedTranscriptSegment], *, max_chars: int = MAX_SENTENCE_CHARS
) -> tuple[TranscriptSentence, ...]:
    """Join consecutive segments from one speaker into sentences."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    sentences: list[TranscriptSentence] = []
    pending: list[MergedTranscriptSegment] = []

    def flush() -> None:
        if not pending:
            return
        sentences.append(
            TranscriptSentence(
                start_sequence=pending[0].sequence,
                end_sequence=pending[-1].sequence,
                start_time=pending[0].start,
                end_time=pending[-1].end,
                speaker_label=pending[0].speaker_label,
                text="".join(segment.text for segment in pending).strip(),
            )
        )
        pending.clear()

    for segment in segments:
        if pending and segment.speaker_label != pending[0].speaker_label:
            # A new voice always starts a new sentence, otherwise a reply would
            # be glued onto the line it answers.
            flush()
        length = sum(len(item.text) for item in pending) + len(segment.text)
        if pending and length > max_chars:
            flush()
        pending.append(segment)
        if _ends_sentence(segment.text):
            flush()

    flush()
    return tuple(sentences)


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip()
    while stripped and stripped[-1] in _TRAILING:
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in _TERMINATORS
