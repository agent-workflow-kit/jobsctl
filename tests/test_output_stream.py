"""The streaming masker: a secret never reaches the output, at any split of the stream."""

from __future__ import annotations

import random

import pytest

from jobsctl.env_mask import masking_set_of
from jobsctl.output_stream import StreamMasker, StreamOverflow
from jobsctl.output_tail import render_final

SECRET = "s3cr3t-token-VALUE"
MASKING = masking_set_of([SECRET])
STRAYS = ["\x1b", "\x1b[", "\x1b]", "\x90", "\x9b", "\x1bP", "\x1b\x1b"]


def stream(chunks: list[bytes], *, max_pending: int = 1 << 20) -> str:
    masker = StreamMasker(MASKING, max_pending=max_pending)
    out = "".join(masker.feed(c) for c in chunks)
    return out + masker.finish()


def whole(data: bytes) -> str:
    return render_final(data.decode("utf-8", "backslashreplace"), MASKING)


def split_at(data: bytes, offsets: list[int]) -> list[bytes]:
    cuts = [0, *sorted(set(offsets)), len(data)]
    return [data[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]


@pytest.mark.parametrize("stray", ["", *STRAYS])
def test_every_single_split_offset_of_a_secret_leaves_no_trace(stray: str) -> None:
    data = f"before {stray}{SECRET} after\nnext line\n".encode()
    for i in range(len(data) + 1):
        out = stream(split_at(data, [i]))
        assert SECRET not in out
        assert "3cr3t" not in out and "s3cr3t" not in out
        assert out == whole(data)


@pytest.mark.parametrize("stray", ["", "\x1b", "\x1b]", "\x90"])
def test_every_pair_of_split_offsets_matches_the_whole_render(stray: str) -> None:
    data = f"a\n{stray}{SECRET}\nb{stray}".encode()
    for i in range(len(data) + 1):
        for j in range(i, len(data) + 1):
            out = stream(split_at(data, [i, j]))
            assert SECRET not in out
            assert out == whole(data)


def test_byte_at_a_time() -> None:
    data = f"x\x1b{SECRET}y\n\x1b[31mred\x1b[0m {SECRET}".encode()
    out = stream([data[i : i + 1] for i in range(len(data))])
    assert out == whole(data) and SECRET not in out


def test_a_multibyte_character_split_over_chunks_is_intact() -> None:
    data = "café 世界 \U0001f600\n".encode()
    for i in range(len(data) + 1):
        assert stream(split_at(data, [i])) == data.decode()


def test_invalid_utf8_becomes_backslash_x_whatever_the_split() -> None:
    data = b"a\xff\xfeb\xc3\n\xe2\x82"
    for i in range(len(data) + 1):
        assert stream(split_at(data, [i])) == whole(data)


def test_output_is_released_at_newlines_not_held_to_the_end() -> None:
    masker = StreamMasker(MASKING)
    pad = "p" * 40
    assert masker.feed(f"line one\n{pad}\npart".encode()).startswith("line one\n")
    assert masker.feed(b"ial") == ""


def test_nothing_is_released_for_a_partial_line_without_newline() -> None:
    masker = StreamMasker(MASKING)
    assert masker.feed(b"no newline yet") == ""
    assert masker.finish() == "no newline yet"


@pytest.mark.parametrize("seed", range(40))
def test_random_streams_never_leak_and_equal_the_whole_render(seed: int) -> None:
    rng = random.Random(seed)
    pieces = [SECRET, SECRET[:5], SECRET[5:], "\n", " ", "text", "\x1b", "\x1b[31m", "\x1b[0m",
              "é", "\x1b]0;t\x07", SECRET + SECRET[:9], "x" * 40]  # fmt: skip
    data = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 40))).encode()
    offsets = [rng.randrange(len(data) + 1) for _ in range(rng.randrange(0, 8))]
    out = stream(split_at(data, offsets))
    assert SECRET not in out
    assert out == whole(data)


@pytest.mark.parametrize("seed", range(40))
def test_hostile_streams_never_leak_even_with_carriage_returns_and_open_escapes(
    seed: int,
) -> None:
    rng = random.Random(1000 + seed)
    pieces = [SECRET, "\r", "\n", "\x1b]0;open", "\x1b[", "\x1bP", "\x1b", SECRET[:7], SECRET[7:],
              "\x00", "\xff".encode("latin-1").decode("latin-1"), "ok"]  # fmt: skip
    data = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 40))).encode("utf-8")
    offsets = [rng.randrange(len(data) + 1) for _ in range(rng.randrange(0, 8))]
    assert SECRET not in stream(split_at(data, offsets))


def test_a_multi_line_secret_is_masked_across_every_split() -> None:
    key = "-----BEGIN KEY-----\nabcdefghijkl\n-----END KEY-----"
    masking = masking_set_of([key])
    data = f"head\n{key}\ntail\n".encode()
    for i in range(len(data) + 1):
        masker = StreamMasker(masking)
        out = masker.feed(data[:i]) + masker.feed(data[i:]) + masker.finish()
        assert "abcdefghijkl" not in out and "BEGIN KEY" not in out
        assert out == render_final(data.decode(), masking)


def test_a_flood_without_newlines_is_flushed_in_bounded_memory() -> None:
    masker = StreamMasker(MASKING, max_pending=1000)
    out = ""
    for _ in range(200):
        out += masker.feed(b"x" * 500)
    assert len(masker._pending) <= 1000 + 2 * len(SECRET) + 500
    out += masker.finish()
    assert out == "x" * 100000


def test_a_secret_in_a_newline_free_flood_is_masked_at_every_forced_cut() -> None:
    for offset in range(0, 60):
        data = (b"y" * offset) + SECRET.encode() + (b"z" * 300)
        masker = StreamMasker(MASKING, max_pending=64)
        out = "".join(masker.feed(data[i : i + 7]) for i in range(0, len(data), 7))
        out += masker.finish()
        assert SECRET not in out and "3cr3t" not in out


def test_overflow_when_no_safe_cut_exists() -> None:
    # An endless chain of overlapping secrets has no cut that does not split one.
    masking = masking_set_of(["abcdefgh", "efghijkl", "ijklmnop", "mnopabcd"])
    masker = StreamMasker(masking, max_pending=40)
    with pytest.raises(StreamOverflow):
        for _ in range(100):
            masker.feed(b"abcdefghijklmnop" * 4)
