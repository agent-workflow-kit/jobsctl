"""The sanitizer shared by human output (interface.md, Masking and sanitizing steps 3 and 5)."""

from __future__ import annotations

import pytest

from jobsctl.textsafe import echo, sanitize


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
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
    ],
)
def test_sanitize(raw: str, clean: str) -> None:
    assert sanitize(raw) == clean


def test_sanitize_unterminated_sequences_do_not_swallow_text_forever() -> None:
    out = sanitize("a\x1b]0;never ends" + "x" * 10)
    assert "\x1b" not in out


def test_sanitize_output_is_encodable() -> None:
    sanitize("x\udcff\ud800\U0010ffff\x1b[").encode("utf-8")


def test_echo_truncates_and_sanitizes() -> None:
    assert echo("a" * 10, limit=5) == "aaaaa..."
    assert echo("\x1b[31mred") == "red"
    assert echo("short") == "short"
