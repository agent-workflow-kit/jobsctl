"""The Job object (interface.md, Job object) built from a `jobs` row.

Shared by the commands that print a job (`add`, `list`, `show`, `wait`, the mutating commands).
`rank` and `blocked_by` are computed, never stored, by `order.py`. `current_attempt` is the one
implementation of the current-attempt rule (interface.md, Job object); `attempt_object` is the
one Attempt object builder, for `show`, `wait` and the hook payloads. Both are public: other
modules (`job_detail`, `show`, `hook_events`, `pre_start`) import them.

Strings in these objects are printed as stored. A job's argv and `--env` values are stored
losslessly (a non-UTF-8 byte is a lone surrogate); `print_json` prints such a byte as the text
`\\xNN`, which cannot be told from real `\\xNN` text. That form is the documented one
(interface.md, Job object, `argv`): argv is never untrusted text, and a submitter that needs to
tell them apart passes the data in a file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Final

from jobsctl.config import Config
from jobsctl.order import blocked_by, rank
from jobsctl.store_db import Tx

JobRow = sqlite3.Row | Mapping[str, Any]  # a stored row, or a projection of one (`--dry-run`)
_RUNNING: Final = ("starting", "running")


def job_row(tx: Tx, job_id: str) -> sqlite3.Row | None:
    return tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])


def current_attempt(tx: Tx, row: JobRow) -> sqlite3.Row | None:
    """The attempt in progress, or the one whose end or hold produced the job's status.

    None while `queued`, after a manual hold, and after a cancel of a queued job (the job's
    last attempt then ended earlier, with another status or reason). `row` may be a projection.
    """
    status = row["status"]
    if status == "queued" or row["attempt"] < 1:
        return None
    if status == "held" and row["hold_reason"] == "manual":
        return None
    attempt = tx.one(
        "SELECT * FROM attempts WHERE job_id = ? AND n = ?", [row["id"], row["attempt"]]
    )
    if attempt is None:
        return None
    if row["retried_from_attempt"] == row["attempt"]:
        return None  # a retry requeued the job and nothing has been claimed since
    if status in (*_RUNNING, "held"):
        return attempt if attempt["status"] == status else None
    if attempt["status"] != status or attempt["reason"] != row["reason"]:
        return None  # a cancel of a queued job leaves an earlier attempt behind
    return attempt


def _json_or_none(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def attempt_object(attempt: sqlite3.Row) -> dict[str, Any]:
    """The Attempt object (interface.md, Attempt object) of an `attempts` row."""
    return {
        "attempt": attempt["n"],
        "status": attempt["status"],
        "reason": attempt["reason"],
        "hold_reason": attempt["hold_reason"],
        "claimed_at": attempt["claimed_at"],
        "running_at": attempt["running_at"],
        "ended_at": attempt["ended_at"],
        "exit": _json_or_none(attempt["exit"]),
        "leftovers": attempt["leftovers"],
        "resolved_path": attempt["resolved_path"],
        "effective": _json_or_none(attempt["effective"]),
        "escalated_hooks": json.loads(attempt["escalated_hooks"]),
        "vetoed_by": attempt["vetoed_by"],
        "hook_messages": json.loads(attempt["hook_messages"]),
        "decision_ids": json.loads(attempt["decision_ids"]),
        "force_requested_by": attempt["force_requested_by"],
        "resolved_by": attempt["resolved_by"],
        "note": attempt["note"],
        "jobsctl_version": attempt["jobsctl_version"],
    }


def effective_settings(
    config: Config, queue: str, overrides: dict[str, Any], snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    if snapshot is not None:
        return {
            "timeout": snapshot["timeout"],
            "max_retries": snapshot["max_retries"],
            "kill_grace": snapshot["kill_grace"],
        }
    settings = config.queue(queue)
    timeout = overrides.get("timeout", settings.timeout)
    return {
        "timeout": None if timeout == "none" else timeout,
        "max_retries": overrides.get("max_retries", settings.max_retries),
        "kill_grace": overrides.get("kill_grace", settings.kill_grace),
    }


def job_labels(tx: Tx, job_id: str) -> list[str]:
    rows = tx.all("SELECT key, value FROM job_labels WHERE job_id = ?", [job_id])
    return sorted(f"{r['key']}={r['value']}" for r in rows)


def job_object(
    tx: Tx,
    row: JobRow,
    config: Config,
    *,
    env_file_unavailable: bool,
    now: str,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """The Job object of `row`, read in `tx`. `now` is a formatted timestamp (for `blocked_by`).

    `labels` replaces the stored labels (a `--dry-run` projection), sorted as given.
    """
    pass_env: list[str] = json.loads(row["pass_env"])
    overrides: dict[str, Any] = json.loads(row["overrides"])
    attempt = current_attempt(tx, row)
    snapshot = None
    if attempt is not None and row["status"] in _RUNNING and attempt["effective"] is not None:
        snapshot = json.loads(attempt["effective"])
    return {
        "id": row["id"],
        "queue": row["queue"],
        "status": row["status"],
        "reason": row["reason"],
        "hold_reason": row["hold_reason"],
        "argv": json.loads(row["argv"]),
        "cwd": row["cwd"],
        "env": json.loads(row["env"]),
        "pass_env": pass_env,
        "labels": job_labels(tx, row["id"]) if labels is None else sorted(labels),
        "unique_key": row["unique_key"],
        "priority": row["priority"],
        "pinned": bool(row["pinned"]),
        "rank": rank(tx, row) if row["status"] == "queued" else None,
        "attempt": row["attempt"],
        "retried_from_attempt": row["retried_from_attempt"],
        "overrides": overrides,
        "effective": effective_settings(config, row["queue"], overrides, snapshot),
        "not_before": row["not_before"],
        "blocked_by": blocked_by(
            config,
            status=row["status"],
            queue=row["queue"],
            not_before=row["not_before"],
            has_pass_env=bool(pass_env),
            env_file_unavailable=env_file_unavailable,
            now=now,
        ),
        "force_requested_by": None if attempt is None else attempt["force_requested_by"],
        "override_hooks": json.loads(row["override_hooks"]),
        "shim_lost": bool(row["shim_lost"]),
        "created_at": row["created_at"],
        "enqueued_at": row["enqueued_at"],
        "status_changed_at": row["status_changed_at"],
        "claimed_at": None if attempt is None else attempt["claimed_at"],
        "ended_at": row["ended_at"],
        "actor": row["actor"],
        "decision_ids": json.loads(row["decision_ids"]),
        "exit": None if attempt is None or attempt["exit"] is None else json.loads(attempt["exit"]),
        "leftovers": None if attempt is None else attempt["leftovers"],
        "resolved_path": None if attempt is None else attempt["resolved_path"],
        "resolved_by": row["resolved_by"],
        "note": row["note"],
    }
