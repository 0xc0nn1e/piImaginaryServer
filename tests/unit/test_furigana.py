from __future__ import annotations

import pytest

from audio_server.core.furigana import annotate, annotate_all


def _rendered(text: str) -> str:
    return "".join(
        f"{token.text}({token.reading})" if token.reading else token.text
        for token in annotate(text)
    )


@pytest.mark.parametrize(
    "text",
    [
        "一旦こちらで持ち帰ります。",
        "来週までに結論を出しましょう。",
        "お願いします。",
        "はい、そうですね。",
        "AIで日本語の会話は上手になりますか",
        "",
    ],
)
def test_runs_always_reconstruct_the_original_text(text: str) -> None:
    """A reading must never alter the sentence it annotates."""

    assert "".join(token.text for token in annotate(text)) == text


def test_reading_covers_only_the_kanji_not_its_okurigana() -> None:
    # 持ち帰り reads もちかえり, but the trailing り is already visible, so the
    # ruby belongs over 持ち帰 alone.
    assert _rendered("一旦こちらで持ち帰ります。") == (
        "一旦(いったん)こちらで持ち帰(もちかえ)ります。"
    )


def test_leading_kana_stays_outside_the_reading() -> None:
    assert _rendered("お願いします。") == "お願(ねが)いします。"


def test_text_without_kanji_gets_no_readings() -> None:
    tokens = annotate("はい、そうですね。")

    assert [token.reading for token in tokens] == [None]
    assert tokens[0].text == "はい、そうですね。"


def test_empty_text_produces_no_tokens() -> None:
    assert annotate("") == []


def test_annotate_all_skips_strings_that_need_no_reading() -> None:
    readings = annotate_all(
        ["結論を出す", "はい", "", "結論を出す", "そうですね"]
    )

    # Kana-only strings are absent, and the repeated string is stored once.
    assert list(readings) == ["結論を出す"]


def test_annotate_all_ignores_non_string_input() -> None:
    assert annotate_all(None) == {}
    assert annotate_all(["text", 5, None]) == {}
