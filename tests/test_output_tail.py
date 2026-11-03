"""Rendering a tail from a raw window: masking and sanitizing steps 1-6 and live-flush
withholding (interface.md, Output capture and Masking and sanitizing; spikes B5 and B7)."""

from __future__ import annotations

import random

import pytest

from jobsctl.config import Config
from jobsctl.env_canonical import CanonicalEnv
from jobsctl.env_file import EnvFile
from jobsctl.env_mask import MaskingSet, masking_values
from jobsctl.output_tail import render_tail

SECRET = "SECRET-TOKEN-9"  # 14 characters


def mset(*values: str) -> MaskingSet:
    return masking_values(
        Config.model_validate({}),
        CanonicalEnv({}),
        EnvFile("not_configured"),
        {f"V{i}": v for i, v in enumerate(values)},
    )


NONE = mset()


def render(
    raw: bytes | str,
    masking: MaskingSet = NONE,
    *,
    dropped: bool = False,
    final: bool = True,
    tail_bytes: int = 16384,
    tail_lines: int = 200,
) -> str:
    data = raw.encode() if isinstance(raw, str) else raw
    return render_tail(
        data,
        dropped=dropped,
        masking=masking,
        tail_bytes=tail_bytes,
        tail_lines=tail_lines,
        final=final,
    ).text


def test_plain_text_passes_through() -> None:
    assert render("one\ntwo\n") == "one\ntwo\n"


def test_empty_window() -> None:
    assert render("") == ""


def test_invalid_utf8_becomes_backslash_x() -> None:
    assert render(b"ok \xff\xfe end\n") == "ok \\xff\\xfe end\n"


def test_truncated_multibyte_sequence_at_end_in_final() -> None:
    assert render(b"caf\xc3") == "caf\\xc3"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("\x1b[31mred\x1b[0m\n", "red\n"),
        ("a\x1b]0;title\x07b\n", "ab\n"),
        ("a\x1b]52;c;ZXZpbA==\x07b\n", "ab\n"),
        ("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\\n", "alink\n"),
        ("a\x1bPq#0\x1b\\b\n", "ab\n"),
        ("a\x1bXsos\x1b\\b\n", "ab\n"),
        ("a\x1b^pm\x1b\\b\n", "ab\n"),
        ("a\x1b_apc\x1b\\b\n", "ab\n"),
        ("a\u009b31mb\n", "ab\n"),
        ("a\u009d0;title\u009cb\n", "ab\n"),
        ("a\x00b\n", "a\\u0000b\n"),
        ("a\x7fb\n", "a\\u007fb\n"),
        ("a\u0085b\n", "a\\u0085b\n"),
        ("a\u202eb\u2066c\u200bd\u2028e\n", "a\\u202eb\\u2066c\\u200bd\\u2028e\n"),
        ("prog 10%\rprog 50%\rdone\nnext\n", "done\nnext\n"),
        ("a\tb\n", "a\tb\n"),
    ],
)
def test_escapes_and_controls(raw: str, clean: str) -> None:
    assert render(raw) == clean


def test_raw_c1_byte_is_invalid_utf8_and_shown_as_text() -> None:
    assert render(b"a\x9b31mb\n") == "a\\x9b31mb\n"


@pytest.mark.parametrize("introducer", ["\x1b]", "\x1bP", "\x1bX", "\x1b^", "\x1b_"])
def test_unterminated_string_sequence_is_removed_to_the_end(introducer: str) -> None:
    out = render(f"before\n{introducer}hidden payload\nmore\n")
    assert out == "before\n"


def test_masking_happens_after_escape_removal() -> None:
    m = mset(SECRET)
    assert render(f"tok SECRET\x1b[1m-TOKEN\x1b[0m-9 end\n", m) == "tok *** end\n"


def test_masking_happens_before_control_escaping() -> None:
    m = mset("abc\x01defghij")
    assert render("x abc\x01defghij y\n", m) == "x *** y\n"


def test_longest_value_wins() -> None:
    m = mset("abcdefgh", "abcdefghij")
    assert render("abcdefghij\n", m) == "***\n"


def test_multi_line_value_masked_whole_and_by_line() -> None:
    m = mset("first-line-1\nsecond-line", "first-line-1", "second-line")
    assert render("a first-line-1\nsecond-line b\n", m) == "a *** b\n"
    assert render("a first-line-1\nother\nsecond-line b\n", m) == "a ***\nother\n*** b\n"


def test_dropped_discards_through_first_newline() -> None:
    m = mset(SECRET)
    # The window starts inside a secret: its first line is discarded with the partial value.
    assert render("RET-TOKEN-9 tail\nline two\n", m, dropped=True) == "line two\n"


def test_dropped_without_newline_discards_longest_plus_256_bytes() -> None:
    m = mset(SECRET)
    raw = "RET-TOKEN-9" + "x" * 300 + "end"
    out = render(raw, m, dropped=True)
    assert out == raw[len(SECRET) + 256 :]
    assert "TOKEN" not in out


def test_not_dropped_keeps_everything() -> None:
    assert render("partial line", dropped=False) == "partial line"


def test_dropped_with_no_masked_values_discards_256_bytes_without_newline() -> None:
    assert render("y" * 300, dropped=True) == "y" * 44


def test_tail_lines_limit_keeps_last_lines() -> None:
    raw = "".join(f"l{i}\n" for i in range(10))
    assert render(raw, tail_lines=3) == "l7\nl8\nl9\n"


def test_tail_lines_counts_a_trailing_partial_line() -> None:
    assert render("a\nb\nc", tail_lines=2) == "b\nc"


def test_tail_bytes_cut_on_line_boundary() -> None:
    raw = "aaaa\nbbbb\ncccc\n"
    assert render(raw, tail_bytes=9) == "cccc\n"
    assert render(raw, tail_bytes=15) == raw
    assert render(raw, tail_bytes=14) == "bbbb\ncccc\n"


def test_exact_fit_on_line_start_keeps_the_line() -> None:
    assert render("aaaa\nbbbb\n", tail_bytes=5) == "bbbb\n"


def test_single_huge_line_keeps_its_end() -> None:
    out = render("z" * 1000 + "\n", tail_bytes=100)
    assert out.endswith("z\n") and len(out.encode()) <= 100 and out != ""


def test_tail_bytes_is_measured_after_escaping() -> None:
    out = render("a\n" + "\x00" * 10 + "\n", tail_bytes=20)
    assert len(out.encode()) <= 20
    assert out.endswith("\\u0000\n")
    assert "\x00" not in out


def test_tail_cut_never_splits_a_multibyte_character() -> None:
    out = render("é" * 100, tail_bytes=51)
    assert set(out) == {"é"}
    assert len(out.encode()) <= 51


def test_smaller_of_bytes_and_lines_wins() -> None:
    raw = "".join(f"{i:03d}\n" for i in range(100))
    assert render(raw, tail_bytes=40, tail_lines=200).count("\n") == 10
    assert render(raw, tail_bytes=4000, tail_lines=5).count("\n") == 5


def test_truncated_flag() -> None:
    def trunc(raw: str, **kw: object) -> bool:
        return render_tail(
            raw.encode(),
            dropped=bool(kw.get("dropped", False)),
            masking=NONE,
            tail_bytes=int(kw.get("tail_bytes", 16384)),  # type: ignore[call-overload]
            tail_lines=200,
            final=True,
        ).truncated

    assert trunc("a\nb\n") is False
    assert trunc("a\nb\n", dropped=True) is True
    assert trunc("aaaa\nbbbb\n", tail_bytes=5) is True


# --- live flush ---------------------------------------------------------------------------


def live(raw: str | bytes, masking: MaskingSet = NONE, **kw: object) -> str:
    return render(raw, masking, final=False, **kw)  # type: ignore[arg-type]


def test_live_withholds_the_last_partial_line() -> None:
    assert live("one\ntwo\npart") == "one\ntwo\n"


def test_live_final_includes_the_partial_line() -> None:
    assert render("one\ntwo\npart") == "one\ntwo\npart"


def test_live_withholds_at_least_longest_minus_one_bytes() -> None:
    m = mset(SECRET)
    # A multi-line value whose first line is too short to mask alone.
    m2 = mset("short\nlong-second-line")
    out = live("ok\nshort\n", m2)
    assert "short" not in out
    assert live("ok line\nmore\n" + "y" * 20 + "\n", m).startswith("ok line\nmore\n")


def test_live_complete_secret_straddling_the_withheld_boundary_is_not_shown_in_part() -> None:
    m = mset(SECRET)
    out = live(f"{SECRET}\n", m)
    assert "SE" not in out and "SECRET" not in out


def test_live_with_no_newline_shows_nothing() -> None:
    assert live("x" * 100) == ""


def test_live_cut_does_not_split_multibyte() -> None:
    m = mset("ééééééééé")
    assert "\\x" not in live("ab\ncdé" + "é" * 20 + "\n", m)


def test_live_unterminated_escape_after_last_newline_is_withheld() -> None:
    assert live("a\n\x1b]0;x") == "a\n"


# --- properties (spike B5) ----------------------------------------------------------------

SECRET_ALPHABET = "QWXZ0123456789"  # never appears in filler
FILLER = "abcdefgh ijklmnop\n\n\t"


def stream(rng: random.Random, secrets: list[str], pieces: int) -> bytes:
    out: list[str] = []
    for _ in range(pieces):
        if rng.random() < 0.3:
            out.append(rng.choice(secrets))
        out.append("".join(rng.choice(FILLER) for _ in range(rng.randint(0, 40))))
    return "".join(out).encode()


def make_secrets(rng: random.Random) -> list[str]:
    return [
        "".join(rng.choice(SECRET_ALPHABET) for _ in range(rng.randint(8, 24)))
        for _ in range(rng.randint(1, 3))
    ]


def leaked(text: str) -> bool:
    return any(ch in SECRET_ALPHABET for ch in text)


@pytest.mark.parametrize("seed", range(40))
def test_live_flush_at_every_cut_offset_never_shows_secret_characters(seed: int) -> None:
    rng = random.Random(seed)
    secrets = make_secrets(rng)
    m = mset(*secrets)
    data = stream(rng, secrets, 12)
    for cut in range(len(data) + 1):
        window = data[:cut]
        out = live(window, m, tail_bytes=10_000)
        assert not leaked(out), (seed, cut, out)


@pytest.mark.parametrize("seed", range(40))
def test_final_tail_of_every_window_start_never_shows_secret_characters(seed: int) -> None:
    rng = random.Random(1000 + seed)
    secrets = make_secrets(rng)
    m = mset(*secrets)
    data = stream(rng, secrets, 12)
    for start in range(len(data) + 1):
        out = render(data[start:], m, dropped=start > 0, tail_bytes=10_000)
        assert not leaked(out), (seed, start, out)


def test_newline_free_stream_every_window_start() -> None:
    rng = random.Random(7)
    secrets = make_secrets(rng)
    m = mset(*secrets)
    data = "".join(
        rng.choice(secrets) + "".join(rng.choice("abcdefgh ") for _ in range(rng.randint(0, 60)))
        for _ in range(30)
    ).encode()
    for start in range(len(data) + 1):
        out = render(data[start:], m, dropped=start > 0, tail_bytes=10_000)
        assert not leaked(out), (start, out)
