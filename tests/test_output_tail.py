"""Rendering a tail from a raw window: masking and sanitizing steps 1-6 and live-flush
withholding (interface.md, Output capture and Masking and sanitizing; spikes B5 and B7)."""

from __future__ import annotations

import random
import time

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
    assert render("tok SECRET\x1b[1m-TOKEN\x1b[0m-9 end\n", m) == "tok *** end\n"


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


# --- review round 1 -----------------------------------------------------------------------

AWS = "AKIAIOSFODNN7EXAMPLE"
LEAD = "filler line one\nfiller line two\n"
TRAIL = "\nmore filler text goes here\nand here\n"


@pytest.mark.parametrize("stray", ["\x1b", "\x1b[", "\u009b", "\x1b[1;", "\x1b[;"])
def test_stray_escape_before_a_secret_does_not_eat_its_first_character(stray: str) -> None:
    m = mset(AWS)
    for raw in (f"{stray}{AWS}\n", f"{LEAD}{stray}{AWS}{TRAIL}"):
        out = render(raw, m)
        assert "KIAIOSF" not in out and "AKIA" not in out and "***" in out, out
    out = live(f"{LEAD}{stray}{AWS}{TRAIL}", m)
    assert "KIAIOSF" not in out and "AKIA" not in out
    assert out.startswith(LEAD)


def test_stray_escape_repro_from_review() -> None:
    assert render(b"\x1bAKIAIOSFODNN7EXAMPLE\n", mset(AWS)) == "***\n"


def test_a_secret_containing_escape_like_bytes_is_masked() -> None:
    m = mset("pa\x1b[31mssword123", "pw\x07\x9cvalue12345")
    for raw in ("x pa\x1b[31mssword123 y\n", "x pw\x07\x9cvalue12345 y\n"):
        assert render(raw, m) == "x *** y\n"
        assert live(LEAD + raw + TRAIL, m).startswith(LEAD + "x *** y\n")


def test_a_secret_split_by_an_escape_is_still_masked_after_stripping() -> None:
    assert render("a SECRET-\x1b[1mTOKEN-9 b\n", mset(SECRET)) == "a *** b\n"


def test_overlapping_secrets_leave_no_part_in_a_tail() -> None:
    m = mset("abcdefgh", "fghijklm")
    assert render("x abcdefghijklm y\n", m) == "x *** y\n"


def test_dropped_multi_line_secret_cut_at_the_window_start() -> None:
    m = mset("pk_live_51\nxyz789")
    assert render("e_51\nxyz789 end\n", m, dropped=True) == ""
    assert render("e_51\nxyz789 end\nnext\n", m, dropped=True) == "next\n"
    m3 = mset("aaaaaaaa\nbbbb\ncccc")
    assert render("aaa\nbbbb\ncccc end\nnext\n", m3, dropped=True) == "next\n"
    assert render("bbb\ncccc end\nnext\nlast\n", m3, dropped=True) == "last\n"


def test_dropped_multi_line_window_with_too_few_newlines_shows_no_value_rest() -> None:
    m = mset("pk_live_51\nxyz789")
    assert render("e_51\nxyz789 end", m, dropped=True) == ""


def test_dropped_newline_discard_moves_past_a_complete_multi_line_value() -> None:
    m = mset("pk_live_51\nxyz789")
    out = render("ab\nzz pk_live_51\nxyz789 end\nnext\n", m, dropped=True)
    assert "xyz789" not in out and "pk_live" not in out


def test_dropped_partial_first_line_with_an_unterminated_osc_does_not_hide_the_tail() -> None:
    assert render(b"ab\x1b]0;t\nSHOWN\nmore\n", dropped=True) == "SHOWN\nmore\n"


def test_live_withholds_before_an_incomplete_escape_or_character() -> None:
    m = mset("QQQ\nQQQQQQQQQ")
    assert "Q" not in live(b"QQQ\nQQQQQQQQ\x1b[3", m)
    assert live(b"QQQ\nQQQQQQQQ\xe2\x82", m) == ""
    m2 = mset("short\nlonglonglonglong")
    out = live(b"short\nlonglonglong\x1b[" + b"0" * 5000, m2)
    assert "short" not in out and "long" not in out
    # once the value is complete, nothing of it shows
    assert "short" not in live(b"short\nlonglonglonglong\x1b[0m\n", m2)


def test_live_boundary_is_exactly_longest_minus_one() -> None:
    m = mset("short\nlong-second-line")  # 22 characters
    assert live("short\nlong-second-lin", m) == ""
    assert "s" not in live("short\nlong-second-line\n", m)
    assert live("ok\n" + "x" * 30, m) == "ok\n"


def test_live_incomplete_utf8_is_not_shown_as_backslash_x() -> None:
    assert live(b"one\ncaf\xc3") == "one\n"
    assert render(b"one\ncaf\xc3") == "one\ncaf\\xc3"


def test_masking_hostile_near_miss_input_stays_fast() -> None:
    m = mset(*("a" * k + "b" for k in range(8, 199)))
    raw = "a" * 20_000
    start = time.process_time()
    assert render(raw, m, tail_bytes=262144) == raw
    assert time.process_time() - start < 20


def test_truncated_covers_the_size_cut() -> None:
    cut = render_tail(
        b"aaaa\nbbbb\n",
        dropped=False,
        masking=NONE,
        tail_bytes=5,
        tail_lines=200,
        final=True,
    )
    assert cut.truncated is True


def test_trailing_carriage_return_leaves_an_empty_last_segment() -> None:
    assert render("one\ntwo\r") == "one\n"


# --- properties with escapes, carriage returns, multibyte and overlapping secrets ---------

# No digits (an escaped ESC is `\\u001b`) and no P, X, ^, _ or ] (after a stray ESC they would start
# a string sequence that swallows text up to the next terminator, wherever that is).
ALPHABET2 = "QWZMNKé中"
ESCAPES2 = ["\x1b", "\x1b[", "\u009b", "\x1b[31m", "\x1b]0;t\x07", "\x1b[0;"]
WHOLE_ESCAPES = ["\x1b[0m", "\x1b]0;t\x07", "\x1b[31m"]
FILLER2 = "abcdefgh ijklmnop\n\n\t\r"


def make_secrets2(rng: random.Random) -> list[str]:
    out: list[str] = []
    for _ in range(rng.randint(1, 3)):
        v = "".join(rng.choice(ALPHABET2) for _ in range(rng.randint(8, 20)))
        kind = rng.random()
        if kind < 0.25:  # short first line
            v = "".join(rng.choice(ALPHABET2) for _ in range(rng.randint(1, 3))) + "\n" + v
        elif kind < 0.4:  # three lines
            v = v[:5] + "\n" + v[5:12] + "\n" + v[12:] + "QWZMQWZM"
        out.append(v)
    if rng.random() < 0.5:  # a value that overlaps the previous one
        base = out[0].replace("\n", "Q")
        out.append(base[len(base) // 2 :] + "".join(rng.choice(ALPHABET2) for _ in range(9)))
    return out


def stream2(rng: random.Random, secrets: list[str], pieces: int) -> bytes:
    # A carriage return right after a multi-line value rewrites the whole logical line,
    # including text a live flush legitimately showed before the value was complete.
    filler_chars = FILLER2 if not any("\n" in s for s in secrets) else FILLER2.replace("\r", "")
    out: list[str] = []
    for _ in range(pieces):
        if rng.random() < 0.35:
            s = rng.choice(secrets)
            if rng.random() < 0.5:  # a stray introducer before the secret
                out.append(rng.choice(ESCAPES2))
            elif rng.random() < 0.4:  # a whole escape sequence inside it
                i = rng.randint(0, len(s))
                s = s[:i] + rng.choice(WHOLE_ESCAPES) + s[i:]
            out.append(s)
        out.append("".join(rng.choice(filler_chars) for _ in range(rng.randint(0, 40))))
    return "".join(out).encode()


def leaked2(text: str) -> bool:
    return any(ch in ALPHABET2 for ch in text)


BIG = 1_000_000


@pytest.mark.parametrize("seed", range(30))
def test_live_flush_property_with_escapes_returns_and_overlaps(seed: int) -> None:
    rng = random.Random(5000 + seed)
    secrets = make_secrets2(rng)
    m = mset(*secrets)
    data = stream2(rng, secrets, 10)
    whole = render(data, m, tail_bytes=BIG, tail_lines=BIG)
    for cut in range(len(data) + 1):
        out = live(data[:cut], m, tail_bytes=BIG, tail_lines=BIG)
        assert not leaked2(out), (seed, cut, out)
        assert whole.startswith(out), (seed, cut, out)


@pytest.mark.parametrize("seed", range(30))
def test_final_tail_property_with_escapes_returns_and_overlaps(seed: int) -> None:
    rng = random.Random(6000 + seed)
    secrets = make_secrets2(rng)
    m = mset(*secrets)
    data = stream2(rng, secrets, 10)
    assert not leaked2(render(data, m, tail_bytes=BIG, tail_lines=BIG)), seed
    for start in range(len(data) + 1):
        out = render(data[start:], m, dropped=start > 0, tail_bytes=BIG, tail_lines=BIG)
        assert not leaked2(out), (seed, start, out)


def test_property_generators_do_produce_the_hard_cases() -> None:
    secrets = [s for seed in range(30) for s in make_secrets2(random.Random(5000 + seed))]
    assert any(s.count("\n") == 2 for s in secrets)  # three lines
    assert any(not s.isascii() for s in secrets)  # multibyte
    assert any(s.index("\n") < 4 for s in secrets if "\n" in s)  # short first line


# --- review round 2 -----------------------------------------------------------------------

V1_SECRET = "SECRETVALUE12345"
V1_RAW = (
    "\x1bS" + V1_SECRET[1:6] + "\x1b[0m" + V1_SECRET[6:] + "\n",
    "\x1b" + V1_SECRET[:5] + "\x1b[0m" + V1_SECRET[5:] + "\n",
)


@pytest.mark.parametrize("raw", V1_RAW)
def test_stray_escape_before_a_secret_with_an_inner_escape_is_masked(raw: str) -> None:
    m = mset(V1_SECRET)
    out = render(raw, m)
    assert "ECRET" not in out and "VALUE" not in out and "12345" not in out, out
    out = live(LEAD + raw + TRAIL, m)
    assert "ECRET" not in out and "VALUE" not in out and "12345" not in out, out
    assert out.startswith(LEAD)


def mangle(rng: random.Random, secret: str) -> str:
    """The secret with an optional stray ESC before it and an optional escape inside it."""
    if rng.random() < 0.7:
        i = rng.randrange(1, len(secret))
        inner = rng.choice(["\x1b[0m", "\x1b]0;t\x07", "\x1bM"])
        secret = secret[:i] + inner + secret[i:]
    # An ESC before X (SOS) would start a string sequence, which swallows the text up to a
    # terminator in the secret: that splits the secret by another byte, see interface.md.
    return rng.choice(["", "" if secret[0] in "XP" else "\x1b"]) + secret


def leaked_secret(text: str) -> bool:
    """`leaked`, ignoring the digits of the `\\u001b` that shows a stray ESC."""
    return leaked(text.replace("\\u001b", ""))


@pytest.mark.parametrize("seed", range(40))
def test_mangled_secrets_are_never_shown_live_or_final(seed: int) -> None:
    rng = random.Random(5000 + seed)
    secrets = make_secrets(rng)
    m = mset(*secrets)
    pieces = []
    for _ in range(10):
        pieces.append("".join(rng.choice(FILLER) for _ in range(rng.randint(0, 20))))
        pieces.append(mangle(rng, rng.choice(secrets)))
    data = "".join(pieces).encode()
    for cut in range(len(data) + 1):
        assert not leaked_secret(live(data[:cut], m, tail_bytes=10_000)), (seed, cut)
        out = render(data[cut:], m, dropped=cut > 0, tail_bytes=10_000)
        assert not leaked_secret(out), (seed, cut, out)
