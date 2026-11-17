"""Retry backoff of an event whose hook leader outlived SIGKILL (docs/interface.md, Delivery).

Each such failure of an event adds one to its ``attempts`` and sets ``retry_after`` to
``now + min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_CAP_SECONDS)``; deliveries skip
the event until then. From ``STUCK_ATTEMPTS`` attempts on, the event is reported as the
``hook_delivery_stuck`` finding, and it is still retried at the cap: a retry is never abandoned.
Ordinary hook failures, timeouts and vetoes are not retried at all and never touch these columns.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Final

from jobsctl.store_db import Tx
from jobsctl.timestamps import format_timestamp

BACKOFF_BASE_SECONDS: Final = 30
BACKOFF_CAP_SECONDS: Final = 3600
STUCK_ATTEMPTS: Final = 5
STUCK_FINDING: Final = "hook_delivery_stuck"

# The events a delivery may run: no undelivered event of the same job and kind, this one included,
# has a ``retry_after`` in the future (events of a job are delivered in order). ``o`` is the
# outbox alias; the one parameter is the current time as a timestamp text.
NOT_DEFERRED_SQL: Final = (
    "NOT EXISTS (SELECT 1 FROM hook_outbox d WHERE d.job_id = o.job_id AND d.async = o.async "
    "AND d.delivered_at IS NULL AND d.seq <= o.seq AND d.retry_after > ?)"
)


def backoff_seconds(attempts: int) -> int:
    """The wait after the ``attempts``-th failure (1 = the first): 30 s, 60 s, ... capped."""
    exponent = min(max(attempts, 1) - 1, 20)  # far past the cap; keeps the number small
    return min(BACKOFF_BASE_SECONDS * 2**exponent, BACKOFF_CAP_SECONDS)


def retry_after_text(now: dt.datetime, attempts: int) -> str:
    return format_timestamp(now + dt.timedelta(seconds=backoff_seconds(attempts)))


def record_unkillable(tx: Tx, event_id: str, now: dt.datetime) -> int | None:
    """Count one unkillable-hook failure of ``event_id`` and defer its next delivery; returns the
    new ``attempts`` (``None`` when the event is unknown or already delivered)."""
    row = tx.one(
        "SELECT attempts FROM hook_outbox WHERE event_id = ? AND delivered_at IS NULL", [event_id]
    )
    if row is None:
        return None
    attempts = int(row["attempts"]) + 1
    tx.execute(
        "UPDATE hook_outbox SET attempts = ?, retry_after = ? WHERE event_id = ?",
        [attempts, retry_after_text(now, attempts), event_id],
    )
    return attempts


def deferred_until(row: sqlite3.Row, now: dt.datetime) -> str | None:
    """``retry_after`` of an outbox row when it is still in the future, else ``None``."""
    until = row["retry_after"]
    if until is not None and str(until) > format_timestamp(now):
        return str(until)
    return None


def stuck_events(tx: Tx) -> list[Any]:
    """Undelivered events with at least ``STUCK_ATTEMPTS`` attempts, in outbox order."""
    return tx.all(
        "SELECT seq, event_id, job_id, attempts, retry_after FROM hook_outbox "
        "WHERE delivered_at IS NULL AND attempts >= ? ORDER BY seq",
        [STUCK_ATTEMPTS],
    )


def stuck_findings(tx: Tx) -> list[dict[str, str | int]]:
    """The ``hook_delivery_stuck`` findings (reconcile output shape), one per stuck event."""
    return [
        {
            "job_id": str(row["job_id"]),
            "finding": STUCK_FINDING,
            "action": "reported",
            "event_id": str(row["event_id"]),
            "attempts": int(row["attempts"]),
        }
        for row in stuck_events(tx)
    ]
