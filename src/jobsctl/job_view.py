"""The Job object (interface.md, Job object) built from a `jobs` row.

Shared by the commands that print a job. `rank` and `blocked_by` are computed, never stored.
TODO(#10): `order.py` owns the rank computation and `blocked_by`; TODO(#11): `list` and `show`
share the current-attempt rule below.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from jobsctl.config import Config
from jobsctl.store_db import Tx

_RUNNING: Final = ("starting", "running")


def job_row(tx: Tx, job_id: str) -> sqlite3.Row | None:
    return tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])


def _rank(tx: Tx, row: sqlite3.Row) -> int:
    """1-based place among the queued jobs of the queue: pinned first, then priority (highest
    first), then position, then id."""
    found = tx.one(
        "SELECT count(*) AS n FROM jobs WHERE queue = :q AND status = 'queued' AND ("
        " pinned > :pin OR (pinned = :pin AND (priority > :pri OR (priority = :pri AND ("
        " position < :pos OR (position = :pos AND id < :id))))))",
        {
            "q": row["queue"],
            "pin": row["pinned"],
            "pri": row["priority"],
            "pos": row["position"],
            "id": row["id"],
        },
    )
    assert found is not None
    return int(found["n"]) + 1


def _current_attempt(tx: Tx, row: sqlite3.Row) -> sqlite3.Row | None:
    """The attempt in progress, or the one whose end or hold produced the job's status."""
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
    if status not in (*_RUNNING, "held") and attempt["status"] != status:
        return None  # a cancel of a queued job leaves an earlier attempt behind
    return attempt


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


def _blocked_by(
    config: Config, row: sqlite3.Row, pass_env: list[str], env_file_unavailable: bool, now: str
) -> str | None:
    """Why a queued job cannot start now: the first that applies (Job object, `blocked_by`)."""
    if row["status"] != "queued":
        return None
    if row["not_before"] is not None and row["not_before"] > now:
        return "not_before"
    if pass_env and env_file_unavailable:
        return "env_file_unavailable"
    if config.queue(row["queue"]).cap == 0:
        return "queue_paused"
    if config.global_cap == 0:
        return "global_paused"
    return None


def job_object(
    tx: Tx, row: sqlite3.Row, config: Config, *, env_file_unavailable: bool, now: str
) -> dict[str, Any]:
    """The Job object of `row`, read in `tx`. `now` is a formatted timestamp (for `blocked_by`)."""
    labels = tx.all("SELECT key, value FROM job_labels WHERE job_id = ?", [row["id"]])
    pass_env: list[str] = json.loads(row["pass_env"])
    overrides: dict[str, Any] = json.loads(row["overrides"])
    attempt = _current_attempt(tx, row)
    snapshot = None
    if attempt is not None and row["status"] in _RUNNING and attempt["effective"] is not None:
        snapshot = json.loads(attempt["effective"])
    return {
        "id": row["id"],
        "queue": row["queue"],
        "status": row["status"],
        "reason": row["reason"],
        "hold_reason": row["hold_reason"],
        # TODO(#11): argv and env values are stored losslessly (a non-UTF-8 byte is a lone
        # surrogate) but print_json shows one as the literal text `\xNN`, which cannot be told
        # from real `\xNN` text. Decide a distinguishable JSON form shared by list and show.
        "argv": json.loads(row["argv"]),
        "cwd": row["cwd"],
        "env": json.loads(row["env"]),
        "pass_env": pass_env,
        "labels": sorted(f"{r['key']}={r['value']}" for r in labels),
        "unique_key": row["unique_key"],
        "priority": row["priority"],
        "pinned": bool(row["pinned"]),
        "rank": _rank(tx, row) if row["status"] == "queued" else None,
        "attempt": row["attempt"],
        "retried_from_attempt": row["retried_from_attempt"],
        "overrides": overrides,
        "effective": effective_settings(config, row["queue"], overrides, snapshot),
        "not_before": row["not_before"],
        "blocked_by": _blocked_by(config, row, pass_env, env_file_unavailable, now),
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
