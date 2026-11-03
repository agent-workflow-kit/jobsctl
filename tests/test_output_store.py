"""Tail encoding (zlib above 4 KiB) and the `outputs` table."""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl.errors import JobsctlError
from jobsctl.output_capture import TailSnapshot
from jobsctl.output_store import (
    COMPRESS_ABOVE,
    decode_tail,
    encode_tail,
    load_tail,
    save_tail,
)
from jobsctl.store_db import Store
from tests.store_helpers import LATER, NOW, insert_row, job_values


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state" / "jobsctl") as s:
        with s.write() as tx:
            insert_row(tx, "jobs", job_values())
        yield s


def test_small_tail_is_stored_plain() -> None:
    blob, compressed = encode_tail("hello\n")
    assert (blob, compressed) == (b"hello\n", False)


def test_boundary_is_above_4_kib() -> None:
    assert encode_tail("a" * COMPRESS_ABOVE)[1] is False
    assert encode_tail("a" * (COMPRESS_ABOVE + 1))[1] is True


def test_round_trip_compressed_and_plain() -> None:
    for text in ["", "x\n", "é" * 5000, "line\n" * 40000, "\\u0000 text"]:
        blob, compressed = encode_tail(text)
        assert decode_tail(blob, compressed) == text


def test_decompression_bomb_is_refused() -> None:
    bomb = zlib.compress(b"\0" * (50 * 1024 * 1024))
    assert len(bomb) < 100 * 1024
    with pytest.raises(JobsctlError) as err:
        decode_tail(bomb, True)
    assert err.value.code == "internal"


def test_truncated_zlib_frame_is_refused() -> None:
    blob, _ = encode_tail("line\n" * 5000)
    with pytest.raises(JobsctlError):
        decode_tail(blob[:-6], True)


def test_garbage_zlib_is_refused() -> None:
    with pytest.raises(JobsctlError):
        decode_tail(b"not zlib at all", True)


def test_trailing_data_after_frame_is_refused() -> None:
    with pytest.raises(JobsctlError):
        decode_tail(zlib.compress(b"ok") + b"extra", True)


def test_oversized_plain_blob_is_refused() -> None:
    with pytest.raises(JobsctlError):
        decode_tail(b"a" * 262145, False)


def test_invalid_utf8_in_a_stored_row_is_escaped_not_raised() -> None:
    assert decode_tail(b"a\xffb", False) == "a\\xffb"


def test_save_and_load(store: Store) -> None:
    snap = TailSnapshot("out\n" * 3000, 123456, True)
    with store.write() as tx:
        save_tail(tx, "jb-aaaaaaaa", 1, snap, NOW)
    with store.read() as tx:
        row = tx.one("SELECT compressed, bytes_seen, truncated, updated_at FROM outputs")
        assert row is not None
        assert (row["compressed"], row["bytes_seen"], row["truncated"], row["updated_at"]) == (
            1,
            123456,
            1,
            NOW,
        )
        assert load_tail(tx, "jb-aaaaaaaa", 1) == snap


def test_save_replaces_the_row(store: Store) -> None:
    with store.write() as tx:
        save_tail(tx, "jb-aaaaaaaa", 1, TailSnapshot("a\n", 2, False), NOW)
        save_tail(tx, "jb-aaaaaaaa", 1, TailSnapshot("a\nb\n", 4, True), LATER)
    with store.read() as tx:
        assert tx.one("SELECT count(*) AS c FROM outputs")["c"] == 1  # type: ignore[index]
        assert load_tail(tx, "jb-aaaaaaaa", 1) == TailSnapshot("a\nb\n", 4, True)


def test_load_missing_is_none(store: Store) -> None:
    with store.read() as tx:
        assert load_tail(tx, "jb-aaaaaaaa", 9) is None
