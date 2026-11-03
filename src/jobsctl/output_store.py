"""Encoding of a tail for the `outputs` table: zlib above 4 KiB, bounded on the way out."""

from __future__ import annotations

import zlib
from typing import Final

from jobsctl.errors import JobsctlError
from jobsctl.output_capture import TailSnapshot
from jobsctl.store_db import Tx

COMPRESS_ABOVE: Final = 4096
MAX_TAIL_BYTES: Final = 262144  # the largest tail `tail_bytes` allows


def encode_tail(text: str) -> tuple[bytes, bool]:
    """The blob to store and whether it is zlib-compressed (only when above 4 KiB)."""
    data = text.encode("utf-8")
    if len(data) > COMPRESS_ABOVE:
        return zlib.compress(data), True
    return data, False


def decode_tail(blob: bytes, compressed: bool) -> str:
    """The stored text. A row is untrusted input (anything with write access to `state.db` can
    store anything): output beyond the largest tail, a damaged or partial zlib frame and data
    after the frame are refused, and invalid UTF-8 is shown as `\\xNN`."""
    if compressed:
        blob = _inflate(bytes(blob))
    elif len(blob) > MAX_TAIL_BYTES:
        raise _corrupt("a stored tail is larger than the maximum")
    return blob.decode("utf-8", "backslashreplace")


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


def load_tail(tx: Tx, job_id: str, n: int) -> TailSnapshot | None:
    row = tx.one(
        "SELECT tail, compressed, bytes_seen, truncated FROM outputs WHERE job_id = ? AND n = ?",
        (job_id, n),
    )
    if row is None:
        return None
    return TailSnapshot(
        decode_tail(row["tail"], bool(row["compressed"])),
        row["bytes_seen"],
        bool(row["truncated"]),
    )
