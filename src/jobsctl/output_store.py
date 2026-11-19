"""Encoding of a tail for the `outputs` table: zlib above 4 KiB, bounded on the way out."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Final

from jobsctl.errors import JobsctlError
from jobsctl.output_capture import TailSnapshot
from jobsctl.store_db import Tx

COMPRESS_ABOVE: Final = 4096
MAX_TAIL_BYTES: Final = 262144  # the largest tail `tail_bytes` allows


@dataclass(frozen=True)
class StoredTail:
    """An `outputs` row: the snapshot and when it was written (`output.updated_at`)."""

    snapshot: TailSnapshot
    updated_at: str


def encode_tail(text: str) -> tuple[bytes, bool]:
    """The blob to store and whether it is zlib-compressed (only when above 4 KiB)."""
    data = text.encode("utf-8")
    if len(data) > COMPRESS_ABOVE:
        return zlib.compress(data), True
    return data, False


def tail_bytes(blob: bytes, compressed: bool) -> bytes:
    """The stored tail as bytes. A row is untrusted input (anything with write access to
    `state.db` can store anything): a value that is not a blob, output beyond the largest tail,
    a damaged or partial zlib frame and data after the frame are refused."""
    if not isinstance(blob, bytes | memoryview):
        raise _corrupt("a stored tail is not a blob")
    blob = bytes(blob)
    if compressed:
        return _inflate(blob)
    if len(blob) > MAX_TAIL_BYTES:
        raise _corrupt("a stored tail is larger than the maximum")
    return blob


def decode_tail(blob: bytes, compressed: bool) -> str:
    """The stored text; invalid UTF-8 is refused too (stored tails always are valid)."""
    try:
        return tail_bytes(blob, compressed).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _corrupt("a stored tail is not valid UTF-8") from exc


def _inflate(blob: bytes) -> bytes:
    decoder = zlib.decompressobj()
    try:
        data = decoder.decompress(blob, MAX_TAIL_BYTES + 1)
    except zlib.error as exc:
        raise _corrupt("a stored tail is not valid zlib data") from exc
    if len(data) > MAX_TAIL_BYTES or decoder.unconsumed_tail:
        raise _corrupt("a stored tail expands beyond the maximum")
    if not decoder.eof:
        raise _corrupt("a stored tail is an incomplete zlib stream")
    if decoder.unused_data:
        raise _corrupt("a stored tail has data after its zlib stream")
    return data


def _corrupt(message: str) -> JobsctlError:
    return JobsctlError("internal", message)


def save_tail(tx: Tx, job_id: str, n: int, snapshot: TailSnapshot, now: str) -> None:
    """Write or replace the output row of attempt `n` (the caller holds the write transaction
    and has checked its claim)."""
    blob, compressed = encode_tail(snapshot.text)
    tx.execute(
        "INSERT INTO outputs (job_id, n, tail, compressed, bytes_seen, truncated, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (job_id, n) DO UPDATE SET tail = excluded.tail,"
        " compressed = excluded.compressed, bytes_seen = excluded.bytes_seen,"
        " truncated = excluded.truncated, updated_at = excluded.updated_at",
        (job_id, n, blob, int(compressed), snapshot.bytes_seen, int(snapshot.truncated), now),
    )


def load_tail(tx: Tx, job_id: str, n: int) -> StoredTail | None:
    row = tx.one(
        "SELECT tail, compressed, bytes_seen, truncated, updated_at FROM outputs"
        " WHERE job_id = ? AND n = ?",
        (job_id, n),
    )
    if row is None:
        return None
    snapshot = TailSnapshot(
        decode_tail(row["tail"], bool(row["compressed"])),
        row["bytes_seen"],
        bool(row["truncated"]),
    )
    return StoredTail(snapshot, row["updated_at"])
