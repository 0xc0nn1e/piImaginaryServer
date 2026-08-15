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


@pytest.mark.parametrize(
    "counted",
    [
        "一人",
        "二人",
        "四人",
        "一日",
        "二日",
        "三日",
        "十日",
        "二十日",
        "四時",
        "七時",
        "二十歳",
        "一回",
    ],
)
def test_numeral_and_counter_pairs_are_left_unannotated(counted: str) -> None:
    """The dictionary scores a numeral and its counter separately.

    That loses every irregular and euphonic reading (一人 ひとり, 二十日 はつか,
    四時 よじ), and some pairs are ambiguous without context (一日 is いちにち or
    ついたち). Concatenating the pieces would teach the wrong word, so these
    stay unannotated rather than guessing.
    """

    tokens = annotate(counted)

    assert [token.reading for token in tokens] == [None]
    assert tokens[0].text == counted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今日", "きょう"),
        ("昨日", "きのう"),
        ("大人", "おとな"),
        ("八百屋", "やおや"),
        ("一昨日", "おととい"),
    ],
)
def test_single_dictionary_entries_keep_their_reading(text: str, expected: str) -> None:
    """Suppressing counters must not cost the compounds that are already right."""

    assert [token.reading for token in annotate(text)] == [expected]


def test_counter_suppression_does_not_swallow_surrounding_words() -> None:
    rendered = "".join(
        f"[{token.text}|{token.reading}]" if token.reading else token.text
        for token in annotate("二人で東京駅に行く")
    )

    assert rendered == "二人で[東京|とうきょう][駅|えき]に[行|い]く"


def test_annotate_all_skips_strings_that_need_no_reading() -> None:
    readings = annotate_all(
        ["結論を出す", "はい", "", "結論を出す", "そうですね"]
    )

    # Kana-only strings are absent, and the repeated string is stored once.
    assert list(readings) == ["結論を出す"]


def test_annotate_all_ignores_non_string_input() -> None:
    assert annotate_all(None) == {}
    assert annotate_all(["text", 5, None]) == {}
