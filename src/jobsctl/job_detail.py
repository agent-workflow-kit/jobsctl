"""The job as `show`, `wait`, `add --wait` and the browse TUI print it: the Job object plus
`attempts` and `output` (`schemas/defs.json` `job_detail`).

The output of an attempt is read in one of two ways, because the commands differ on purpose:
`show` ("raise") fails when the tail cannot be read and sanitizes it again; `wait` ("warn")
turns an unreadable tail into `output: null` plus a warning, so its exit code still reports the
job's status. `wait`'s human summary stays its own (`print_summary`): it is a one-line status
with the tail, not `show`'s full report.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any, Literal

from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.job_view import attempt_object, current_attempt, job_object
from jobsctl.output_log import find_log
from jobsctl.output_store import load_tail, tail_bytes
from jobsctl.store_db import Tx
from jobsctl.textsafe import sanitize


def output_object(tx: Tx, job_id: str, n: int, *, quiet: bool = False) -> dict[str, Any] | None:
    """The stored tail of attempt `n`, or `None` when there is none or it cannot be read.

    A tail that cannot be read is `output: null` with a warning on stderr (not under `--quiet`),
    so the exit code of `wait` still reports the job's status (interface.md, `wait`).
    """
    try:
        stored = load_tail(tx, job_id, n)
    except JobsctlError as err:
        if not quiet:
            message = sanitize(err.message, inline=True)
            sys.stderr.write(
                f"jobsctl: warning: the output of {job_id} cannot be read ({message})\n"
            )
            sys.stderr.flush()
        return None
    if stored is None:
        return None
    return _with_log(
        tx,
        job_id,
        n,
        {
            "tail": stored.snapshot.text,
            "bytes_seen": stored.snapshot.bytes_seen,
            "truncated": stored.snapshot.truncated,
            "updated_at": stored.updated_at,
        },
    )


def _with_log(tx: Tx, job_id: str, n: int, output: dict[str, Any]) -> dict[str, Any]:
    """`output` plus `log` (the full output log's path, size and whether the cap stopped it)
    when the attempt has a log file; otherwise `output` unchanged, so nothing differs while the
    feature is off."""
    found = find_log(tx.store.paths, job_id, n)
    if found is not None:
        output["log"] = {"path": found.path, "bytes": found.bytes, "truncated": found.truncated}
    return output


def shown_output_object(tx: Tx, job_id: str, n: int) -> dict[str, Any] | None:
    """The stored tail of attempt `n` as `show` prints it, or `None` when there is none.

    The tail is untrusted: an undecodable byte becomes `\\xNN` and the text is sanitized again,
    in `--json` as well as in human output.
    """
    row = tx.one("SELECT * FROM outputs WHERE job_id = ? AND n = ?", [job_id, n])
    if row is None:
        return None
    return _with_log(
        tx,
        job_id,
        n,
        {
            "tail": sanitize(
                tail_bytes(row["tail"], bool(row["compressed"])).decode("utf-8", "backslashreplace")
            ),
            "bytes_seen": row["bytes_seen"],
            "truncated": bool(row["truncated"]),
            "updated_at": row["updated_at"],
        },
    )


def require_attempt(tx: Tx, job_id: str, attempt: int) -> None:
    """Raise `not_found` unless the job has attempt `attempt`."""
    if tx.one("SELECT 1 FROM attempts WHERE job_id = ? AND n = ?", [job_id, attempt]) is None:
        raise JobsctlError("not_found", f"job {job_id} has no attempt {attempt}")


OutputMode = Literal["warn", "raise", "off"]


def job_detail(
    tx: Tx,
    row: sqlite3.Row,
    config: Config,
    *,
    env_file_unavailable: bool,
    now: str,
    quiet: bool = False,
    attempt: int | None = None,
    output: OutputMode = "warn",
) -> dict[str, Any]:
    """The job object with `attempts` and `output`.

    `output` is the attempt `attempt` (default: the current attempt, `null` when there is none)
    read as `output` says: "warn" (`output_object`), "raise" (`shown_output_object`) or "off"
    (`output: null`). An `attempt` the job does not have is `not_found`.
    """
    attempts = tx.all("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", [row["id"]])
    current = current_attempt(tx, row)
    wanted: int | None = None if current is None else int(current["n"])
    if attempt is not None:
        require_attempt(tx, row["id"], attempt)
        wanted = attempt
    job = job_object(tx, row, config, env_file_unavailable=env_file_unavailable, now=now)
    job["attempts"] = [attempt_object(a) for a in attempts]
    job["output"] = None
    if wanted is not None and output == "warn":
        job["output"] = output_object(tx, row["id"], wanted, quiet=quiet)
    elif wanted is not None and output == "raise":
        job["output"] = shown_output_object(tx, row["id"], wanted)
    return job
