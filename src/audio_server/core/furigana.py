"""Hiragana reading aids for Japanese text.

Readings come from a dictionary-based morphological analyser rather than the
language model. An LLM invents plausible-looking readings, and a wrong reading
actively teaches the wrong word, so determinism matters more than coverage here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cost is paid lazily at runtime
    from janome.tokenizer import Tokenizer

_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KATAKANA_START = "ァ"
_KATAKANA_END = "ヶ"
_KANA_OFFSET = 0x60
# Bounds the memo so a long session cannot grow it without limit; conversational
# transcripts repeat short utterances heavily, so the hit rate is high.
_MEMO_SIZE = 4096


@dataclass(frozen=True, slots=True)
class FuriganaToken:
    """One run of text, with a reading only when it needs one."""

    text: str
    reading: str | None


def _katakana_to_hiragana(value: str) -> str:
    return "".join(
        chr(ord(char) - _KANA_OFFSET) if _KATAKANA_START <= char <= _KATAKANA_END else char
        for char in value
    )


def _has_kanji(value: str) -> bool:
    return _KANJI.search(value) is not None


@lru_cache(maxsize=1)
def _tokenizer() -> Tokenizer:
    # Importing janome builds its dictionary, so defer it until first use and
    # keep one instance for the process.
    from janome.tokenizer import Tokenizer

    return Tokenizer()


def _trim_okurigana(surface: str, reading: str) -> tuple[str, str, str, str]:
    """Split a token so the reading sits over the kanji only.

    ``持ち帰り``/``もちかえり`` becomes ``持ち帰`` read ``もちかえ`` with ``り``
    left as plain text, which is how furigana is normally set.
    """

    prefix = 0
    while (
        prefix < len(surface)
        and prefix < len(reading)
        and surface[prefix] == reading[prefix]
        and not _has_kanji(surface[prefix])
    ):
        prefix += 1

    suffix = 0
    while (
        suffix < len(surface) - prefix
        and suffix < len(reading) - prefix
        and surface[len(surface) - 1 - suffix] == reading[len(reading) - 1 - suffix]
        and not _has_kanji(surface[len(surface) - 1 - suffix])
    ):
        suffix += 1

    core = surface[prefix : len(surface) - suffix]
    core_reading = reading[prefix : len(reading) - suffix]
    trailing = surface[len(surface) - suffix :] if suffix else ""
    return surface[:prefix], core, core_reading, trailing


@lru_cache(maxsize=_MEMO_SIZE)
def _annotate_cached(text: str) -> tuple[FuriganaToken, ...]:
    tokens: list[FuriganaToken] = []

    def append(value: str, reading: str | None) -> None:
        if not value:
            return
        # Merge neighbouring plain runs so the payload stays compact.
        if reading is None and tokens and tokens[-1].reading is None:
            tokens[-1] = FuriganaToken(text=tokens[-1].text + value, reading=None)
            return
        tokens.append(FuriganaToken(text=value, reading=reading))

    for token in _tokenizer().tokenize(text):
        surface = token.surface
        if not _has_kanji(surface):
            append(surface, None)
            continue

        raw_reading = getattr(token, "reading", "*")
        reading = _katakana_to_hiragana(raw_reading) if raw_reading != "*" else ""
        if not reading or reading == surface:
            append(surface, None)
            continue

        leading, core, core_reading, trailing = _trim_okurigana(surface, reading)
        append(leading, None)
        if core and core_reading and _has_kanji(core):
            append(core, core_reading)
        else:
            append(core, None)
        append(trailing, None)

    return tuple(tokens)


def annotate(text: str) -> list[FuriganaToken]:
    """Split text into runs, attaching a hiragana reading to each kanji run."""

    if not text or not _has_kanji(text):
        return [FuriganaToken(text=text, reading=None)] if text else []
    return list(_annotate_cached(text))


def annotate_all(texts: object) -> dict[str, list[FuriganaToken]]:
    """Build a reading map for every distinct Japanese string in ``texts``.

    Responses carry one map instead of a parallel field beside each string, so
    repeated quotes cost nothing and existing response shapes stay unchanged.
    """

    readings: dict[str, list[FuriganaToken]] = {}
    if not isinstance(texts, (list, tuple, set, frozenset)):
        return readings
    for value in texts:
        if not isinstance(value, str) or not value or value in readings:
            continue
        if _has_kanji(value):
            readings[value] = annotate(value)
    return readings
