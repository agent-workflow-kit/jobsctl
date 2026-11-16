"""The job as `show`, `wait` and `add --wait` print it: the Job object plus `attempts` and
`output` (`schemas/defs.json` `job_detail`).

TODO(#11): `show` reuses `job_detail` (adding `--attempt N` and `--no-output`) and decides the
human rendering; `wait` prints a short summary until then.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.job_view import current_attempt, job_object
from jobsctl.output_store import load_tail
from jobsctl.store_db import Tx
from jobsctl.textsafe import sanitize


def attempt_object(row: sqlite3.Row) -> dict[str, Any]:
    """One entry of `attempts` (schemas/defs.json `attempt`)."""
    effective = row["effective"]
    exit_record = row["exit"]
    return {
        "attempt": row["n"],
        "status": row["status"],
        "reason": row["reason"],
        "hold_reason": row["hold_reason"],
        "claimed_at": row["claimed_at"],
        "running_at": row["running_at"],
        "ended_at": row["ended_at"],
        "exit": None if exit_record is None else json.loads(exit_record),
        "leftovers": row["leftovers"],
        "resolved_path": row["resolved_path"],
        "effective": None if effective is None else json.loads(effective),
        "escalated_hooks": json.loads(row["escalated_hooks"]),
        "vetoed_by": row["vetoed_by"],
        "hook_messages": json.loads(row["hook_messages"]),
        "decision_ids": json.loads(row["decision_ids"]),
        "force_requested_by": row["force_requested_by"],
        "resolved_by": row["resolved_by"],
        "note": row["note"],
        "jobsctl_version": row["jobsctl_version"],
    }


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
    return {
        "tail": stored.snapshot.text,
        "bytes_seen": stored.snapshot.bytes_seen,
        "truncated": stored.snapshot.truncated,
        "updated_at": stored.updated_at,
    }


def job_detail(
    tx: Tx,
    row: sqlite3.Row,
    config: Config,
    *,
    env_file_unavailable: bool,
    now: str,
    quiet: bool = False,
) -> dict[str, Any]:
    job = job_object(tx, row, config, env_file_unavailable=env_file_unavailable, now=now)
    attempts = tx.all("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", [row["id"]])
    current = current_attempt(tx, row)
    job["attempts"] = [attempt_object(a) for a in attempts]
    job["output"] = (
        None if current is None else output_object(tx, row["id"], current["n"], quiet=quiet)
    )
    return job
