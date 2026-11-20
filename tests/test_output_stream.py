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


MULTI_SECRETS = [
    "ab\ncd-multi\nline-xyz",  # first line shorter than the longest value minus one
    "abcd\nefgh-multi\nline-xyz",
    "-----BEGIN KEY-----\nabcdefghijkl\n-----END KEY-----",
]


@pytest.mark.parametrize("stray", ["", *STRAYS])
@pytest.mark.parametrize("secret", MULTI_SECRETS)
def test_a_multi_line_secret_after_a_stray_escape_matches_the_whole_render(
    secret: str, stray: str
) -> None:
    masking = masking_set_of([secret])
    for prefix in ("", "hi\n"):
        data = f"{prefix}{stray}{secret}\nnext\n".encode()
        want = render_final(data.decode("utf-8", "backslashreplace"), masking)
        for i in range(len(data) + 1):
            masker = StreamMasker(masking)
            out = masker.feed(data[:i]) + masker.feed(data[i:]) + masker.finish()
            assert out == want, (secret, stray, prefix, i)
        for i in range(len(data) + 1):
            for j in range(i, len(data) + 1):
                masker = StreamMasker(masking)
                out = "".join(masker.feed(c) for c in split_at(data, [i, j])) + masker.finish()
                assert out == want, (secret, stray, prefix, i, j)


def test_r1_repro_x9b_before_a_multi_line_secret() -> None:
    secret = "ab\ncd-multi\nline-xyz"
    masking = masking_set_of([secret])
    data = ("\x9b" + secret + "\nnext\n").encode()
    masker = StreamMasker(masking)
    out = masker.feed(data[:5]) + masker.feed(data[5:]) + masker.finish()
    assert out == render_final(data.decode(), masking) == "***\nnext\n"


def _open_osc_stream(max_pending: int, k: int, cut: int, payload: int, csi: int, step: int) -> str:
    head = b"x" * k + SECRET.encode()[:cut] + b"\x1b]0;" + b"Y" * payload + b"\x1b[0m" * csi
    tail = b"\x1b\\" + SECRET.encode()[cut:] + b"\nend\n"
    masker = StreamMasker(MASKING, max_pending=max_pending)
    out = []
    for i in range(0, len(head), step):
        out.append(masker.feed(head[i : i + step]))
    out.append(masker.feed(tail))
    out.append(masker.finish())
    return "".join(out)


def test_a_secret_split_around_an_open_osc_is_not_released_in_pieces() -> None:
    mp = 1000
    leaks = []
    for k in range(940, 1060):
        for cut in (9, 5, 1):
            for payload in (0, 5, 40):
                for csi in (0, 10):
                    for step in (7, 64):
                        try:
                            out = _open_osc_stream(mp, k, cut, payload, csi, step)
                        except StreamOverflow:
                            continue  # the log would stop: nothing unsafe is returned
                        if SECRET in out:
                            leaks.append((k, cut, payload, csi, step))
    assert leaks == []


@pytest.mark.parametrize("seed", range(30))
def test_open_osc_around_secrets_never_leaks_with_a_small_max_pending(seed: int) -> None:
    rng = random.Random(7000 + seed)
    pieces = ["\x1b]0;", "Y" * 20, "\x1b[0m", "\x1b\\", SECRET, SECRET[:6], SECRET[6:], "x" * 30,
              "\n", "\x07"]  # fmt: skip
    data = "".join(rng.choice(pieces) for _ in range(rng.randrange(5, 60))).encode()
    offsets = [rng.randrange(len(data) + 1) for _ in range(rng.randrange(0, 20))]
    masker = StreamMasker(MASKING, max_pending=64)
    out = ""
    try:
        for chunk in split_at(data, offsets):
            out += masker.feed(chunk)
        out += masker.finish()
    except StreamOverflow:
        pass
    assert SECRET not in out


def test_feed_does_not_render_again_until_held_text_has_grown() -> None:
    from unittest import mock

    import jobsctl.output_stream as mod

    masker = StreamMasker(MASKING)
    data = b"\x1b]0;" + b"payload line\n" * 20000
    with mock.patch.object(mod, "render_stable", wraps=mod.render_stable) as spy:
        out = ""
        for i in range(0, len(data), 64):
            out += masker.feed(data[i : i + 64])
        calls = spy.call_count
        out += masker.finish()
    assert out == render_final(data.decode(), MASKING)
    assert calls < 200


@pytest.mark.parametrize("seed", range(30))
def test_backoff_keeps_equality_with_the_whole_render(seed: int) -> None:
    rng = random.Random(9000 + seed)
    pieces = ["\x1b]0;open", "payload\n", "\x07", SECRET, "x" * 50, "\n", "\x1b\\", "\x1b[0m"]
    data = "".join(rng.choice(pieces) for _ in range(rng.randrange(5, 120))).encode()
    step = rng.choice([1, 3, 17, 100])
    out = stream([data[i : i + step] for i in range(0, len(data), step)])
    assert SECRET not in out
    # equality holds except where a string escape spans a newline (see R6); the no-leak holds
    if b"\x1b]" not in data and b"\x1bP" not in data:
        assert out == whole(data)


def test_a_secret_prefix_followed_by_many_complete_escapes_is_held() -> None:
    for k in range(940, 1060):
        masker = StreamMasker(MASKING, max_pending=1000)
        head = b"x" * k + SECRET.encode()[:9] + b"\x1b[0m" * 10
        tail = SECRET.encode()[9:] + b"\nend\n"
        try:
            out = "".join(masker.feed(head[i : i + 7]) for i in range(0, len(head), 7))
            out += masker.feed(tail) + masker.finish()
        except StreamOverflow:
            continue
        assert SECRET not in out
        assert out == whole(head + tail)
