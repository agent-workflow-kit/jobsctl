"""Grammars: queues, labels, unique keys, env names, decision ids (interface.md)."""

from __future__ import annotations

import random
import string

import pytest

from jobsctl import validate
from jobsctl.errors import JobsctlError

LABEL_CHARS = string.ascii_letters + string.digits + "._:/#@+=,-"
HOSTILE = [
    "",
    "\x00",
    "a\x00b",
    "a\n",
    "a b",
    " a",
    "é",  # e acute
    "а",  # Cyrillic a
    "ａ",  # fullwidth a
    "٣",  # Arabic-indic digit
    "a​",  # zero-width space
    "a‮",  # bidi override
    "a\udc80",  # lone surrogate
    "a\x7f",
    "a" * 100_000,
]


@pytest.mark.parametrize(
    ("fn", "valid", "invalid"),
    [
        (
            validate.queue_name,
            ["a", "0", "default", "a-b_c", "a" * 64],
            ["", "A", "-a", "_a", "a.b", "a" * 65, "a/b", "Q"],
        ),
        (
            validate.label_key,
            ["a", "a.b", "a-b_c.d", "0", "a" * 64],
            ["", "A", ".a", "-a", "_a", "a" * 65, "a=b", "a:b"],
        ),
        (
            validate.label_value,
            ["", "A", "a=b", "x/y#z@+,:-.", "a" * 256],
            ["a b", "a" * 257, "a;b", "a*", "a\\b"],
        ),
        (
            validate.unique_key,
            ["a", "A.b:c/d#e@f+g=h,i-j", "a" * 256],
            ["", "a" * 257, "a b", "a;b"],
        ),
        (
            validate.env_name,
            ["A", "_", "a1", "NAME_1", "A" * 128, "jobsctl_x", "JOBSCT"],
            ["", "1A", "A-B", "A.B", "A" * 129, "JOBSCTL_X", "JOBSCTL_", "A=B"],
        ),
        (
            validate.decision_id,
            ["a", "A.b:c@d/e-f_g", "a" * 128],
            ["", "a" * 129, "a b", "a=b", "a#b", "a+b", "a,b"],
        ),
    ],
)
def test_boundaries(fn, valid: list[str], invalid: list[str]) -> None:  # type: ignore[no-untyped-def]
    for text in valid:
        assert fn(text) == text
    for text in invalid:
        with pytest.raises(validate.GrammarError):
            fn(text)


@pytest.mark.parametrize(
    "fn",
    [
        validate.queue_name,
        validate.label_key,
        validate.label_value,
        validate.unique_key,
        validate.env_name,
        validate.decision_id,
    ],
)
def test_hostile_inputs_are_rejected(fn) -> None:  # type: ignore[no-untyped-def]
    for text in HOSTILE:
        if text == "" and fn is validate.label_value:
            continue
        with pytest.raises(validate.GrammarError):
            fn(text)


def test_grammar_error_message_never_echoes_input() -> None:
    with pytest.raises(validate.GrammarError) as exc:
        validate.env_name("S3CRET=value")
    assert "S3CRET" not in str(exc.value)


def test_random_strings_agree_with_the_character_class() -> None:
    rng = random.Random(4)
    alphabet = string.ascii_letters + string.digits + "._:/#@+=,- \n\x00;é"
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        expected = 1 <= len(text) and all(c in LABEL_CHARS for c in text)
        try:
            validate.unique_key(text)
            ok = True
        except validate.GrammarError:
            ok = False
        assert ok == expected


@pytest.mark.parametrize(
    ("text", "pair"),
    [
        ("a=b", ("a", "b")),
        ("a=", ("a", "")),
        ("a=b=c", ("a", "b=c")),
        ("a.b-c=x/y", ("a.b-c", "x/y")),
    ],
)
def test_label_splits_at_the_first_equals(text: str, pair: tuple[str, str]) -> None:
    assert validate.parse_label(text) == pair


@pytest.mark.parametrize("text", ["", "ab", "=b", "A=b", "a b=c", "a=b c", "a=\x00", "a=é"])
def test_bad_labels(text: str) -> None:
    with pytest.raises(validate.GrammarError):
        validate.parse_label(text)


def test_labels_are_a_sorted_set() -> None:
    merged = validate.merge_labels([("b", "2"), ("a", "z")], [("a", "y"), ("b", "2"), ("a", "z")])
    assert merged == [("a", "y"), ("a", "z"), ("b", "2")]


def test_a_key_may_carry_several_values() -> None:
    assert validate.merge_labels([], [("k", "1"), ("k", "2")]) == [("k", "1"), ("k", "2")]


def _labels(n: int) -> list[tuple[str, str]]:
    return [(f"k{i:02d}", "v") for i in range(n)]


def test_32_labels_are_allowed_and_a_repeat_at_the_limit_is_a_noop() -> None:
    full = validate.merge_labels([], _labels(32))
    assert len(full) == 32
    assert validate.merge_labels(full, [full[0]]) == full


def test_the_33rd_label_is_usage_with_details_limit() -> None:
    with pytest.raises(JobsctlError) as exc:
        validate.merge_labels(_labels(32), [("extra", "v")])
    assert exc.value.code == "usage"
    assert exc.value.exit_code == 2
    assert exc.value.envelope()["error"]["details"] == {"limit": 32}


def test_duplicates_within_the_added_set_do_not_count_twice() -> None:
    assert len(validate.merge_labels(_labels(31), [("x", "1"), ("x", "1")])) == 32
