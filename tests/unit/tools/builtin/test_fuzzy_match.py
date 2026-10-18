"""The fuzzy matcher behind ``patch`` (P05-19).

Fixtures: ``tests/fixtures/fuzzy/<strategy>.txt``.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from indus.tools.builtin import fuzzy_match as fm

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "fuzzy"
NAMES = [name for name, _ in fm.STRATEGIES]


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


# (strategy, search text, replacement, expected file afterwards)
CHAIN_CASES = [
    ("exact", "beta = 2", "beta = 3", "alpha = 1\nbeta = 3\n"),
    (
        "line_trimmed",
        "if ready:  \n  go()",
        "if ready:\n    stop()",
        "def run():\n    if ready:\n        stop()\n",
    ),
    (
        "whitespace_normalized",
        "total = compute(a, b)",
        "total = compute(b)",
        "total = compute(b)\n",
    ),
    ("escape_normalized", "first line\\nsecond line", "first line\nSECOND", "first line\nSECOND\n"),
    ("unicode_normalized", 'print("hello -- world")', 'print("bye")', 'print("bye")\n'),
    (
        "block_anchor",
        "def f():\n    a = 1\n    b = 3\n    return a + b",
        "def f():\n    return 3",
        "def f():\n    return 3\n",
    ),
    (
        "context_similarity",
        "def compute_totals(items):\n    return sum(items)",
        "def compute_total(items):\n    return sum(items) or 0",
        "def compute_total(items):\n    return sum(items) or 0\n",
    ),
]


@pytest.mark.parametrize(("name", "old", "new", "expected"), CHAIN_CASES)
def test_each_strategy_is_the_first_that_matches_its_fixture(
    name: str, old: str, new: str, expected: str
) -> None:
    content = fixture(name)
    position = NAMES.index(name)
    for earlier in NAMES[:position]:
        assert fm.spans_for(earlier, content, old) == [], earlier
    result = fm.find_and_replace(content, old, new)
    assert result.error is None, result.error
    assert result.strategy == name
    assert result.content == expected


@pytest.mark.parametrize(
    ("name", "old"),
    [
        ("indentation_flexible", "with open(path) as fh:\n    data = fh.read()"),
        ("trimmed_boundary", "header():\nbody stays\nfooter()"),
    ],
)
def test_strategies_that_an_earlier_one_always_covers_still_match_alone(
    name: str, old: str
) -> None:
    # Anything these two accept, line_trimmed accepts first; the chain keeps them for order parity.
    content = fixture(name)
    assert fm.spans_for(name, content, old)
    assert fm.find_spans(content, old) is not None


def test_similarity_matches_carry_a_note() -> None:
    result = fm.find_and_replace(fixture("block_anchor"), CHAIN_CASES[5][1], "x")
    assert any("Approximate match" in note for note in result.notes)


def test_unicode_spans_map_back_to_the_original_characters() -> None:
    content = "say \u201chi\u201d now"
    assert fm.spans_for("unicode_normalized", content, 'say "hi"') == [(0, 8)]


def test_whitespace_spans_cover_whole_runs() -> None:
    content = "a  =\t\tb + c\n"
    [(start, end)] = fm.spans_for("whitespace_normalized", content, "a = b")
    assert content[start:end] == "a  =\t\tb"


# -- ambiguity, replace_all, rejections ------------------------------------------------------------


def test_several_matches_list_locations() -> None:
    content = "".join(f"x = 1  # {i}\n" if i % 2 else f"other {i}\n" for i in range(16))
    result = fm.find_and_replace(content, "x = 1", "x = 2")
    assert result.error is not None and "matches 8 places" in result.error
    assert "L2: x = 1  # 1" in result.error
    assert result.error.count("\n  L") == fm.MAX_LOCATIONS
    assert "and 3 more" in result.error
    assert result.content == content


def test_replace_all() -> None:
    result = fm.find_and_replace("a1 b a1 c a1", "a1", "z", replace_all=True)
    assert result.content == "z b z c z" and result.count == 3


def test_replace_all_refuses_approximate_matches() -> None:
    content = "def f():\n    x = 1\n    return x\ndef f():\n    x = 2\n    return x\n"
    result = fm.find_and_replace(content, "def f():\n    x = 9\n    return x", "y", True)
    assert result.error is not None and "replace_all needs the exact text" in result.error


@pytest.mark.parametrize(("old", "words"), [("", "empty"), ("  \n ", "only whitespace")])
def test_empty_old_string(old: str, words: str) -> None:
    error = fm.find_and_replace("text", old, "x").error
    assert error is not None and words in error


def test_identical_strings_are_rejected() -> None:
    assert fm.find_and_replace("abc", "abc", "abc").error == fm.IDENTICAL


def test_already_applied_is_detected() -> None:
    content = "value = compute(42)\n"
    result = fm.find_and_replace(content, "value = compute(41)", "value = compute(42)")
    assert result.already_applied and result.error is None
    assert not fm.already_applied(content, "x", "short")


def test_no_match_shows_the_closest_lines() -> None:
    content = "import os\n\ndef load_config(path):\n    pass\n"
    result = fm.find_and_replace(content, "def load_configuration(file):", "x")
    assert result.error is not None and result.error.startswith(fm.NO_MATCH)
    assert "3|def load_config(path):" in result.error


def test_whitespace_only_difference_is_shown() -> None:
    content = "\tfoo()\n\tbar()\n"
    hint = fm.closest_lines("    foo()\n    entirely_unrelated_call_here()", content)
    assert "Only the indentation differs" in hint and "file has: →foo()" in hint


# -- escape drift ----------------------------------------------------------------------------------


def test_quote_drift_is_corrected_when_the_fixed_text_exists() -> None:
    result = fm.find_and_replace("print('done')\n", "print(\\'done\\')", "print(\\'finished\\')")
    assert result.content == "print('finished')\n"
    assert any("Escape drift corrected" in note for note in result.notes)


def test_quote_drift_is_not_corrected_without_verbatim_evidence() -> None:
    result = fm.find_and_replace("print('other')\n", "print(\\'done\\')", "print(\\'x\\')")
    assert result.error is not None and result.content == "print('other')\n"


def test_quote_drift_with_unproven_new_escapes_is_refused() -> None:
    result = fm.find_and_replace("say('hi')\n", "say(\\'hi\\')", "say(\\'hi\\', \\\"x\\\")")
    assert result.error is not None and "Escape drift" in result.error


def test_doubled_backslashes_are_halved_when_the_halved_text_exists() -> None:
    content = 'p = "a\\\\b"\n'  # the file holds two backslashes
    result = fm.find_and_replace(content, 'p = "a\\\\\\\\b"', 'p = "a/b"')
    assert result.content == 'p = "a/b"\n'
    ambiguous = fm.find_and_replace(content, 'p = "a\\\\\\\\b"', 'p = "a\\\\c"')
    assert ambiguous.error is not None and "may be meant" in ambiguous.error


def test_newline_literal_drift_is_refused() -> None:
    result = fm.find_and_replace("a\nb\n", "a\\nb", "c\\nd")
    assert result.error is not None and "real line breaks" in result.error


def test_literal_tab_becomes_a_tab_where_the_file_has_one() -> None:
    result = fm.find_and_replace("a\tb\n", "a\\tb", "c\\td")
    assert result.content == "c\td\n"


# -- re-indenting --------------------------------------------------------------------------------


def test_non_exact_match_is_reindented_onto_the_file() -> None:
    content = "class A:\n    def m(self):\n        return 1\n"
    result = fm.find_and_replace(
        content, "def m(self):\n    return 1", "def m(self):\n    x = 2\n    return x"
    )
    assert result.strategy == "line_trimmed"
    assert result.content == "class A:\n    def m(self):\n        x = 2\n        return x\n"


def test_trailing_newline_in_a_line_match_is_not_doubled() -> None:
    content = "  one\n  two\nthree\n"
    result = fm.find_and_replace(content, "one\ntwo\n", "uno\ndos\n")
    assert result.content == "  uno\n  dos\nthree\n"


# -- a property: an exact patch and its inverse give the file back ---------------------------------


def test_exact_patch_then_inverse_restores_the_file() -> None:
    rng = random.Random(20261018)
    alphabet = "abcdefghij \t'\"\\{}()=+-*/\u2014"
    for _ in range(200):
        lines = [
            f"{i:04d}:" + "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 30)))
            for i in range(rng.randrange(1, 40))
        ]
        content = "\n".join(lines) + ("\n" if rng.random() < 0.5 else "")
        first = rng.randrange(len(lines))
        last = rng.randrange(first, min(len(lines), first + 4))
        old = "\n".join(lines[first : last + 1])
        new = f"{9999 - first:04d}#" + "".join(
            rng.choice(alphabet) for _ in range(rng.randrange(1, 20))
        )
        if new in content:
            continue
        forward = fm.find_and_replace(content, old, new)
        assert forward.error is None and forward.strategy == "exact", forward.error
        back = fm.find_and_replace(forward.content, new, old)
        assert back.error is None, back.error
        assert back.content == content
