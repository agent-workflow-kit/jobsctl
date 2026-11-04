"""The sanitizer shared by human output (interface.md, Masking and sanitizing steps 3 and 5)."""

from __future__ import annotations

import random

import pytest

from jobsctl.textsafe import (
    echo,
    escape_unsafe,
    incomplete_escape_start,
    sanitize,
    strip_escapes,
    strip_escapes_mapped,
)

SANITIZE_CASES = [
    ("plain text\tand\nnewline", "plain text\tand\nnewline"),
    ("\x1b[31mred\x1b[0m", "red"),
    ("a\x1b]0;title\x07b", "ab"),
    ("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b", "alinkb"),
    ("a\x1bPdcs data\x1b\\b", "ab"),
    ("a\x1b_apc\x1b\\b", "ab"),
    ("a\x9b31mred", "ared"),
    ("a\x9d0;t\x9cb", "ab"),
    ("old\rnew", "new"),
    ("l1\r\nl2\rx\n", "l1\nx\n"),
    ("a\x00b", "a\\u0000b"),
    ("a\x7fb", "a\\u007fb"),
    ("a\x85b", "a\\u0085b"),
    ("a‮b", "a\\u202eb"),
    ("a​b", "a\\u200bb"),
    ("a﻿b", "a\\ufeffb"),
    ("ünï", "ünï"),
    ("bad\udcffbyte", "bad\\xffbyte"),
    ("a\U000e0001b", "a\\U000e0001b"),
    ("lone\ud800", "lone\\ud800"),
]


@pytest.mark.parametrize(("raw", "clean"), SANITIZE_CASES)
def test_sanitize(raw: str, clean: str) -> None:
    assert sanitize(raw) == clean


@pytest.mark.parametrize(
    "introducer", ["\x1b]", "\x1bP", "\x1b_", "\x1b^", "\x1bX", "\x9d", "\x90"]
)
def test_sanitize_removes_an_unterminated_string_sequence_to_the_end(introducer: str) -> None:
    assert sanitize("a" + introducer + "0;never ends\nand more") == "a"


def test_sanitize_line_and_paragraph_separators_are_escaped() -> None:
    assert sanitize("a\u2028b\u2029c") == "a\\u2028b\\u2029c"


def test_sanitize_inline_escapes_cr_and_lf_instead_of_rewriting_lines() -> None:
    assert sanitize("old\rnew\nnext\r\nx", inline=True) == "old\\rnew\\nnext\\r\\nx"
    assert sanitize("a\tb", inline=True) == "a\tb"


def test_echo_escapes_line_breaks() -> None:
    assert echo("a\r\nb") == "a\\r\\nb"


def test_sanitize_output_is_encodable() -> None:
    sanitize("x\udcff\ud800\U0010ffff\x1b[").encode("utf-8")


def test_echo_truncates_and_sanitizes() -> None:
    assert echo("a" * 10, limit=5) == "aaaaa..."
    assert echo("\x1b[31mred") == "red"
    assert echo("short") == "short"


@pytest.mark.parametrize(("raw", "_"), SANITIZE_CASES)
def test_sanitize_is_strip_escapes_then_escape_unsafe(raw: str, _: str) -> None:
    assert sanitize(raw) == escape_unsafe(strip_escapes(raw))


def test_sanitize_is_strip_escapes_then_escape_unsafe_on_hostile_strings() -> None:
    rng = random.Random(21)
    pieces = ["\x1b[", "\x1b]", "\x1b", "\x9b", "\x9d", "\x07", "\x1b\\", "\r", "\n", "\r\n"]
    pieces += ["31m", "0;t", "a", "\u202e", "\ud800", "\u2028", "\x00", "é"]
    for _ in range(500):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        assert sanitize(raw) == escape_unsafe(strip_escapes(raw)), raw


@pytest.mark.parametrize(
    ("text", "start"),
    [
        ("abc", None),
        ("abc\x1b", 3),
        ("abc\x1b[", 3),
        ("abc\x1b[3;", 3),
        ("abc\x9b3", 3),
        ("abc\x1b[31m", None),
        ("abc\x1b[3\n", None),
        ("abc\x1b]0;t", 3),
        ("abc\x1b]0;t\x07", None),
        ("abc\x1b]0;t\x1b\\", None),
        ("abc\x9d", 3),
        ("abc\x1b]0;a\x07def\x1b[0m\x1bP", 16),
        ("x\x1bq tail text", None),
    ],
)
def test_incomplete_escape_start(text: str, start: int | None) -> None:
    assert incomplete_escape_start(text) == (len(text) if start is None else start)


def test_strip_escapes_mapped_matches_strip_escapes_and_locates_characters() -> None:
    rng = random.Random(22)
    pieces = ["\x1b[31m", "\x1b]0;t\x07", "\x1b", "\x9b", "\r", "\n", "\r\n", "ab", "é", "\x1bP"]
    for _ in range(500):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        stripped, origin = strip_escapes_mapped(raw)
        assert stripped == strip_escapes(raw), raw
        assert len(origin) == len(stripped) and origin == sorted(set(origin))
        assert all(raw[i] == ch for i, ch in zip(origin, stripped, strict=True)), raw
