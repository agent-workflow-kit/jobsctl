"""``export``: read ``audit.jsonl`` or ``log.jsonl`` back as JSON lines (interface.md, export).

Only the current file is read (rotated files are the operator's). Torn or foreign lines (a
crashed writer's fragment, a line that is not a JSON object with an integer ``seq`` and a
``ts``, a line with a number JSON cannot print (``NaN``, ``Infinity``, an overflowing float such
as ``1e999``), an over-long line) are skipped, as is a last line without its newline, which may
be an append in progress. Records are printed re-serialized as ASCII-only JSON, so nothing in the
file can reach the terminal as a raw control character.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jobsctl.errors import JobsctlError
from jobsctl.log_records import MAX_RECORD_BYTES, dumps, parse_ts


def _open_regular(path: Path) -> int | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JobsctlError("internal", f"cannot read {path}: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise JobsctlError("internal", f"{path} is not a regular file")
        if st.st_uid != os.geteuid():
            raise JobsctlError("internal", f"{path} is owned by another user")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """The complete, well-formed records of ``path`` in file order."""
    fd = _open_regular(path)
    if fd is None:
        return
    with os.fdopen(fd, "rb") as handle:
        while True:
            line = handle.readline(MAX_RECORD_BYTES + 1)
            if not line:
                return
            if not line.endswith(b"\n"):
                if len(line) <= MAX_RECORD_BYTES:
                    return  # the last line has no newline yet: an append in progress
                while line and not line.endswith(b"\n"):  # skip the rest of an over-long line
                    line = handle.readline(MAX_RECORD_BYTES + 1)
                continue
            record = _parse(line)
            if record is not None:
                yield record


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e999 parses as infinity, which dumps refuses
        raise ValueError("number out of range")
    return value


def _parse(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):  # ValueError also covers an over-long integer
        return None
    seq = record.get("seq") if isinstance(record, dict) else None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None
    if parse_ts(record.get("ts")) is None:
        return None
    return record  # type: ignore[no-any-return]


def export_lines(
    path: Path, *, since: dt.datetime | None = None, after_seq: int | None = None
) -> Iterator[str]:
    """JSON lines of the records with ``seq`` above ``after_seq`` and ``ts`` at or after
    ``since`` (both filters apply when both are given)."""
    for record in read_records(path):
        if after_seq is not None and record["seq"] <= after_seq:
            continue
        if since is not None:
            when = parse_ts(record["ts"])
            assert when is not None
            if when < since:
                continue
        try:
            line = dumps(record)
        except (
            ValueError,
            RecursionError,
        ):  # cannot be printed as JSON: skip it like a foreign line
            continue
        yield line
