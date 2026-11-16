"""Automatic retry (interface.md, Automatic retry): the one place that decides whether a failed
attempt returns its job to `queued` instead of ending it `failed`.

Every process that ends an attempt (the shim today; `_kill` and reconcile later, TODO(#22)) asks
`plan_requeue` inside the transaction that records the failure. The decision uses the
`max_retries` the attempt recorded (`effective`), or the current config when it recorded none, and
the job's count of automatic retries since the last `retry` or `release`. The plan is only the job
columns of the requeue; the caller owns the compare-and-set, the attempt row, the post-fail event
and the log record (`retry_pending: true`).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Final

from jobsctl.config import Config
from jobsctl.job_view import effective_settings
from jobsctl.order import tail_position
from jobsctl.store_db import Tx
from jobsctl.timestamps import TimestampError, format_timestamp, parse_timestamp

# `spawn_failed` here is the shim's own failure (config load, `argv[0]`, exec); the 3rd
# consecutive failure to spawn the shim itself is final (`claims.fail_spawn`) and never asks.
RETRYABLE_REASONS: Final = frozenset(
    {"exit", "signaled", "timeout", "terminated", "lost", "spawn_failed", "missing_env"}
)


def _max_retries(tx: Tx, config: Config, job: Any, claim_id: str) -> int:
    attempt = tx.one(
        "SELECT effective FROM attempts WHERE job_id = ? AND claim_id = ?", [job["id"], claim_id]
    )
    snapshot = None if attempt is None or attempt["effective"] is None else attempt["effective"]
    settings = effective_settings(
        config,
        job["queue"],
        json.loads(job["overrides"]),
        None if snapshot is None else json.loads(snapshot),
    )
    return int(settings["max_retries"])


def plan_requeue(
    tx: Tx, config: Config, job_id: str, claim_id: str, *, reason: str, now: str
) -> dict[str, Any] | None:
    """The `jobs` columns that requeue `job_id` after an attempt failed with `reason`, or `None`
    when the job ends `failed`. `claim_id` names the failed attempt. Allocates the position (the
    bottom of the queue, so the bottom of any priority band)."""
    if reason not in RETRYABLE_REASONS:
        return None
    job = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
    if job is None or int(job["auto_retries"]) >= _max_retries(tx, config, job, claim_id):
        return None
    try:
        not_before = format_timestamp(
            parse_timestamp(now) + dt.timedelta(seconds=config.queue(job["queue"]).retry_delay)
        )
    except (TimestampError, OverflowError):
        return None  # a delay that cannot be represented: fail rather than guess
    return {
        "reason": None,
        "hold_reason": None,
        "ended_at": None,
        "shim_lost": 0,
        "not_before": not_before,
        "auto_retries": int(job["auto_retries"]) + 1,
        "enqueued_at": now,
        "status_changed_at": now,
        "position": tail_position(tx, str(job["queue"]), exclude=job_id),
    }
