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


def test_mask_runs_after_escape_removal_and_before_control_escaping() -> None:
    secret = "s3cr3t-value"
    # an escape sequence inside the secret is removed first, so the masked text is still found
    text = f"a {secret[:4]}\x1b[31m{secret[4:]} b\x07\x1b[0m"
    out = sanitize(text, mask=lambda t: t.replace(secret, "***"))
    assert out == "a *** b\\u0007"
