"""The ring buffer and capture: bounded memory, byte counts, wrap, chunk boundaries."""

from __future__ import annotations

import random
import resource
import tracemalloc

import pytest

from jobsctl.config import Config
from jobsctl.env_canonical import CanonicalEnv
from jobsctl.env_file import EnvFile
from jobsctl.env_mask import MaskingSet, masking_values
from jobsctl.output_capture import OutputCapture

SECRET = "QWXZ-9876-0123"


def mset(*values: str) -> MaskingSet:
    return masking_values(
        Config.model_validate({}),
        CanonicalEnv({}),
        EnvFile("not_configured"),
        {f"V{i}": v for i, v in enumerate(values)},
    )


def test_window_size_is_tail_bytes_plus_longest_plus_4096() -> None:
    cap = OutputCapture(mset(SECRET), tail_bytes=1000, tail_lines=200)
    assert cap.window_size == 1000 + len(SECRET) + 4096


def test_counts_every_byte_and_flags_truncation() -> None:
    cap = OutputCapture(mset(), tail_bytes=100, tail_lines=200)
    cap.feed(b"a" * 4000)
    assert (cap.bytes_seen, cap.dropped) == (4000, False)
    cap.feed(b"b" * 1000)
    assert (cap.bytes_seen, cap.dropped) == (5000, True)
    assert len(cap.window) == cap.window_size


def test_empty_feed_changes_nothing() -> None:
    cap = OutputCapture(mset(), tail_bytes=100, tail_lines=200)
    cap.feed(b"")
    assert (cap.bytes_seen, cap.dropped, cap.window) == (0, False, b"")


def test_chunk_larger_than_window_keeps_its_end() -> None:
    cap = OutputCapture(mset(), tail_bytes=10, tail_lines=200)
    data = bytes(range(256)) * 100
    cap.feed(data)
    assert cap.window == data[-cap.window_size :]
    assert cap.bytes_seen == len(data) and cap.dropped


def test_window_equals_the_stream_end_for_any_chunking() -> None:
    rng = random.Random(3)
    data = bytes(rng.randrange(256) for _ in range(30_000))
    cap = OutputCapture(mset(), tail_bytes=100, tail_lines=200)
    pos = 0
    while pos < len(data):
        n = rng.randint(1, 9000)
        cap.feed(data[pos : pos + n])
        pos += n
        assert cap.window == data[:pos][-cap.window_size :]


def test_exact_fill_is_not_truncation() -> None:
    cap = OutputCapture(mset(), tail_bytes=10, tail_lines=200)
    cap.feed(b"x" * cap.window_size)
    assert cap.dropped is False
    cap.feed(b"y")
    assert cap.dropped is True


def test_secret_split_across_chunks_is_masked() -> None:
    cap = OutputCapture(mset(SECRET), tail_bytes=1000, tail_lines=200)
    for i in range(0, len(SECRET), 3):
        cap.feed(SECRET[i : i + 3].encode())
    cap.feed(b"\n")
    assert cap.snapshot(final=True).text == "***\n"


def test_secret_split_across_a_ring_wrap_is_masked() -> None:
    m = mset(SECRET)
    for filler in range(0, 200):
        cap = OutputCapture(m, tail_bytes=64, tail_lines=200)
        size = cap.window_size
        cap.feed(b"." * (size - 5 + filler % 7))
        cap.feed(SECRET.encode())
        cap.feed(b"." * (filler % 11) + b"\n")
        text = cap.snapshot(final=True).text
        assert "QWXZ" not in text and "9876" not in text and "0123" not in text


def test_snapshot_reports_counts() -> None:
    cap = OutputCapture(mset(), tail_bytes=100, tail_lines=200)
    cap.feed(b"hello\nwor")
    live = cap.snapshot(final=False)
    assert (live.text, live.bytes_seen, live.truncated) == ("hello\n", 9, False)
    done = cap.snapshot(final=True)
    assert done.text == "hello\nwor"


def test_two_gib_at_bounded_memory() -> None:
    cap = OutputCapture(mset(SECRET), tail_bytes=16384, tail_lines=200)
    chunk = (b"x" * 1023 + b"\n") * 64  # 64 KiB
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    try:
        for _ in range(2 * 1024 * 1024 * 1024 // len(chunk)):
            cap.feed(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert cap.bytes_seen == 2 * 1024**3
    assert cap.dropped
    assert peak < 2 * 1024 * 1024
    assert after - before < 50 * 1024  # KiB on Linux
    assert cap.snapshot(final=True).text.endswith("x\n")


def test_rejects_non_bytes() -> None:
    cap = OutputCapture(mset(), tail_bytes=10, tail_lines=10)
    with pytest.raises(TypeError):
        cap.feed("text")  # type: ignore[arg-type]
