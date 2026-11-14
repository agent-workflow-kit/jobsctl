"""Post-hook outbox events (interface.md, Hooks, Payload, Delivery) written with a transition.

A transition and its post hook event are committed in one transaction. This module builds the
payload (`schemas/hooks/payload.json`) and inserts the event; running the hook is delivery
(TODO(#24)). Up to two events are written per transition and hook name: one when at least
one synchronous entry of that hook selects the job (`async` 0) and one when at least one `async`
entry does (`async` 1); each has its own event id (`queues`, `exclude_queues`, `labels`,
evaluated at transition time against the job as it is after the transition). The `output_tail`
is always `null` here: whether a hook opts into the tail (`include_output`) is decided per entry
at delivery, which reads the stored tail then (TODO(#24))."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from jobsctl.config import Config
from jobsctl.store_db import Tx
from jobsctl.store_ops import OutboxEvent, counter_incr, outbox_insert

EVENT_COUNTER = "hook_events"


def hook_job(job: dict[str, Any]) -> dict[str, Any]:
    """A job object with `env` reduced to its variable names (`env_names`)."""
    reduced = {k: v for k, v in job.items() if k != "env"}
    reduced["env_names"] = sorted(job["env"])
    return reduced


def attempt_object(row: sqlite3.Row) -> dict[str, Any]:
    """The Attempt object of an `attempts` row (interface.md, Attempt object)."""
    return {
        "attempt": row["n"],
        "status": row["status"],
        "reason": row["reason"],
        "hold_reason": row["hold_reason"],
        "claimed_at": row["claimed_at"],
        "running_at": row["running_at"],
        "ended_at": row["ended_at"],
        "exit": None if row["exit"] is None else json.loads(row["exit"]),
        "leftovers": row["leftovers"],
        "resolved_path": row["resolved_path"],
        "effective": None if row["effective"] is None else json.loads(row["effective"]),
        "escalated_hooks": json.loads(row["escalated_hooks"]),
        "vetoed_by": row["vetoed_by"],
        "hook_messages": json.loads(row["hook_messages"]),
        "decision_ids": json.loads(row["decision_ids"]),
        "force_requested_by": row["force_requested_by"],
        "resolved_by": row["resolved_by"],
        "note": row["note"],
        "jobsctl_version": row["jobsctl_version"],
    }


def _output_available(tx: Tx, attempt: sqlite3.Row | None) -> bool:
    """The job process of the attempt ran and a tail exists."""
    if attempt is None or attempt["exit"] is None:
        return False
    if json.loads(attempt["exit"]).get("kind") == "spawn_failed":
        return False
    found = tx.one(
        "SELECT 1 FROM outputs WHERE job_id = ? AND n = ?", [attempt["job_id"], attempt["n"]]
    )
    return found is not None


def selecting_entries(config: Config, hook: str, queue: str, labels: list[str]) -> list[Any]:
    """The configured entries of `hook` that select a job of `queue` with `labels` (`k=v`)."""
    chosen = []
    for entry in config.hooks.entries(hook):
        if entry.queues is not None and queue not in entry.queues:
            continue
        if queue in entry.exclude_queues:
            continue
        if not set(entry.labels) <= set(labels):
            continue
        chosen.append(entry)
    return chosen


def write_post_event(
    tx: Tx,
    config: Config,
    hook: str,
    *,
    job: dict[str, Any],
    previous_status: str,
    attempt: sqlite3.Row | None,
    now: str,
) -> bool:
    """Insert the outbox events of `hook` for a transition that just happened (`job` is the job
    object after it, `attempt` the attempt row after it, when the transition involved one): one
    for the selecting synchronous entries, then one for the selecting `async` entries. Returns
    whether an event was written."""
    entries = selecting_entries(config, hook, job["queue"], job["labels"])
    written = False
    for is_async in (False, True):
        if not any(bool(getattr(entry, "async_", False)) == is_async for entry in entries):
            continue
        number = counter_incr(tx, EVENT_COUNTER)
        event_id = f"{job['id']}:{hook}:{number}"
        payload = {
            "tool": "jobsctl",
            "hook": hook,
            "event_id": event_id,
            "object_type": "job",
            "object_id": job["id"],
            "repo": None,
            "labels": job["labels"],
            "previous_status": previous_status,
            "job": hook_job(job),
            "attempt": None if attempt is None else attempt_object(attempt),
            "output_available": _output_available(tx, attempt),
            "output_tail": None,
        }
        outbox_insert(
            tx,
            job["id"],
            OutboxEvent(
                event_id=event_id,
                hook=hook,
                payload=payload,
                created_at=now,
                is_async=is_async,
            ),
        )
        written = True
    return written
