"""Compare-and-set helpers, outbox, counters and leases on top of :mod:`jobsctl.store_db`.

Every helper takes the open write transaction (:class:`~jobsctl.store_db.Tx`) so callers compose
several in one ``BEGIN IMMEDIATE`` transaction, and returns ``False`` when its compare-and-set
lost (zero rows matched) without changing anything. Column names in dynamic SQL come from the
live schema, never from the caller's text, so an injection-shaped key is a ``ValueError``.

Ids, timestamps and identities are plain strings and ints here. TODO(#4): a validated job-id type
and the timestamp grammar plug in at the marked places; the store only compares and stores them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jobsctl.errors import JobsctlError
from jobsctl.store_db import Store, Tx
from jobsctl.store_schema import ALL_STATUSES, NON_TERMINAL_STATUSES

LEASE_TTL_SECONDS = 60  # callers compute `expires_at` = now + this (interface.md, Leases)
_LEASE_NAME_RE = re.compile(r"^(runner|deliver|job:[a-z0-9-]{1,64})\Z")  # TODO(#4): job id type
# The store's own fixed-width UTC form (interface.md, Timestamps), so leases compare as strings.
# TODO(#4): switch to the shared timestamp parser.
_LEASE_TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\Z")
_TAKE_LEASE_TRIES = 3
_UNIQUE_KEY_CONFLICT = "UNIQUE constraint failed: jobs.unique_key"

# Columns a caller may never set through the generic helpers: identity and the CAS keys.
_JOB_FROZEN = frozenset({"id", "status"})
_ATTEMPT_FROZEN = frozenset({"job_id", "n", "claim_id"})


class UniqueKeyConflict(JobsctlError):
    """A transition into a non-terminal status collides with a live job's ``unique_key``.

    Refused (exit 20, ``duplicate_key``) with ``details.job_id`` of the job holding the key
    (interface.md, Unique keys and ``retry``). Any other constraint violation, such as a
    duplicate outbox ``event_id``, stays a :class:`sqlite3.IntegrityError`.
    """

    def __init__(self, job_id: str, holder: str | None) -> None:
        super().__init__(
            "refused",
            f"another non-terminal job ({holder or 'unknown'}) holds the unique key of {job_id}",
            reason="duplicate_key",
            details=None if holder is None else {"job_id": holder},
        )
        self.job_id = job_id
        self.holder = holder


@dataclass(frozen=True)
class OutboxEvent:
    """A post-hook event committed with its transition (interface.md, Delivery)."""

    event_id: str
    hook: str
    payload: Mapping[str, Any]
    created_at: str  # TODO(#4): timestamp grammar
    is_async: bool = False


def _assignments(
    store: Store, table: str, updates: Mapping[str, Any], frozen: frozenset[str]
) -> tuple[str, list[Any]]:
    allowed = store.columns(table)
    parts: list[str] = []
    values: list[Any] = []
    for column, value in updates.items():
        if column not in allowed or column in frozen:
            raise ValueError(f"cannot set {table}.{column!s}")
        parts.append(f'"{column}" = ?')
        values.append(value)
    return ", ".join(parts), values


def _check_status(status: str) -> None:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown status {status!r}")


# -- jobs --------------------------------------------------------------------------------------


def insert_job(tx: Tx, values: Mapping[str, Any]) -> bool:
    """Insert a job row; ``False`` when a non-terminal job already holds its ``unique_key``.

    Only that one conflict is absorbed (the partial unique index decides, so racing inserts leave
    exactly one row); a missing NOT NULL column or a bad status still raises.
    """
    allowed = tx.store.columns("jobs")
    if not values or any(k not in allowed for k in values):
        raise ValueError("unknown or no job columns")
    _check_status(str(values.get("status")))
    columns = ", ".join(f'"{k}"' for k in values)
    marks = ", ".join("?" for _ in values)
    live = ", ".join(f"'{s}'" for s in NON_TERMINAL_STATUSES)
    cur = tx.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({marks}) "
        f"ON CONFLICT (unique_key) WHERE unique_key IS NOT NULL AND status IN ({live}) "
        "DO NOTHING",
        list(values.values()),
    )
    return cur.rowcount == 1


def _update_job(
    tx: Tx, job_id: str, sql: str, params: list[Any], updates: Mapping[str, Any]
) -> int:
    """Run a job ``UPDATE``; a live ``unique_key`` collision becomes :class:`UniqueKeyConflict`."""
    try:
        return tx.execute(sql, params).rowcount
    except sqlite3.IntegrityError as exc:
        if str(exc) != _UNIQUE_KEY_CONFLICT:
            raise
        key = updates.get("unique_key")
        if key is None:
            row = tx.one("SELECT unique_key FROM jobs WHERE id = ?", [job_id])
            key = None if row is None else row["unique_key"]
        live = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
        holder = tx.one(
            f"SELECT id FROM jobs WHERE unique_key = ? AND status IN ({live}) AND id != ?",
            [key, *NON_TERMINAL_STATUSES, job_id],
        )
        raise UniqueKeyConflict(job_id, None if holder is None else str(holder["id"])) from exc


def transition_job(
    tx: Tx,
    job_id: str,
    *,
    expect_status: str,
    expect_attempt: int,
    to_status: str,
    updates: Mapping[str, Any] | None = None,
    events: Iterable[OutboxEvent] = (),
) -> bool:
    """Compare-and-set a job's transition on ``(id, status, attempt)``.

    One ``UPDATE ... WHERE id = ? AND status = ? AND attempt = ?``. When it wins, ``events`` are
    inserted in the same transaction; when it loses nothing is written. Raises
    :class:`UniqueKeyConflict` when the job would become non-terminal while another non-terminal
    job holds its ``unique_key``.
    """
    _check_status(expect_status)
    _check_status(to_status)
    updates = updates or {}
    sets, values = _assignments(tx.store, "jobs", updates, _JOB_FROZEN)
    sql = "UPDATE jobs SET status = ?" + (f", {sets}" if sets else "")
    sql += " WHERE id = ? AND status = ? AND attempt = ?"
    params = [to_status, *values, job_id, expect_status, expect_attempt]
    if _update_job(tx, job_id, sql, params, updates) != 1:
        return False
    for event in events:
        outbox_insert(tx, job_id, event)
    return True


def transition_job_by_claim(
    tx: Tx,
    job_id: str,
    *,
    claim_id: str,
    expect_status: str,
    to_status: str,
    updates: Mapping[str, Any] | None = None,
    events: Iterable[OutboxEvent] = (),
) -> bool:
    """Like :func:`transition_job` for a shim: the job's current attempt must carry ``claim_id``.

    A stale shim (its attempt was requeued and claimed again) matches nothing (D27).
    """
    _check_status(expect_status)
    _check_status(to_status)
    updates = updates or {}
    sets, values = _assignments(tx.store, "jobs", updates, _JOB_FROZEN)
    sql = "UPDATE jobs SET status = ?" + (f", {sets}" if sets else "")
    sql += (
        " WHERE id = ? AND status = ? AND EXISTS (SELECT 1 FROM attempts a"
        " WHERE a.job_id = jobs.id AND a.n = jobs.attempt AND a.claim_id = ?)"
    )
    params = [to_status, *values, job_id, expect_status, claim_id]
    if _update_job(tx, job_id, sql, params, updates) != 1:
        return False
    for event in events:
        outbox_insert(tx, job_id, event)
    return True


def update_attempt_by_claim(tx: Tx, job_id: str, claim_id: str, updates: Mapping[str, Any]) -> bool:
    """Compare-and-set a write to the job's current attempt on ``claim_id`` (D27)."""
    sets, values = _assignments(tx.store, "attempts", updates, _ATTEMPT_FROZEN)
    if not sets:
        raise ValueError("no attempt columns to update")
    cur = tx.execute(
        f"UPDATE attempts SET {sets} WHERE job_id = ? AND claim_id = ? "
        "AND n = (SELECT attempt FROM jobs WHERE id = ?)",
        [*values, job_id, claim_id, job_id],
    )
    return cur.rowcount == 1


# -- outbox ------------------------------------------------------------------------------------


def outbox_insert(tx: Tx, job_id: str, event: OutboxEvent) -> int:
    """Insert an undelivered event; returns its ``seq`` (the delivery order)."""
    cur = tx.execute(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            event.event_id,
            job_id,
            event.hook,
            json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            int(event.is_async),
            event.created_at,
        ],
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def outbox_pending(tx: Tx, job_id: str, *, is_async: bool | None = None) -> list[sqlite3.Row]:
    """A job's undelivered events in delivery order."""
    sql = "SELECT * FROM hook_outbox WHERE job_id = ? AND delivered_at IS NULL"
    params: list[Any] = [job_id]
    if is_async is not None:
        sql += " AND async = ?"
        params.append(int(is_async))
    return tx.all(sql + " ORDER BY seq", params)


def outbox_mark_delivered(tx: Tx, event_id: str, delivered_at: str) -> bool:
    """Mark an event delivered; ``False`` if it was already (or is unknown)."""
    cur = tx.execute(
        "UPDATE hook_outbox SET delivered_at = ? WHERE event_id = ? AND delivered_at IS NULL",
        [delivered_at, event_id],
    )
    return cur.rowcount == 1


# -- counters ----------------------------------------------------------------------------------


def counter_get(tx: Tx, name: str) -> int:
    row = tx.one("SELECT value FROM counters WHERE name = ?", [name])
    return 0 if row is None else int(row["value"])


def counter_incr(tx: Tx, name: str, by: int = 1) -> int:
    """Add ``by`` (at least 1) to the counter, creating it at 0, and return the new value.

    Inside a write transaction this is the gap-free allocator for ``seq``: a rollback undoes the
    increment, so values are consecutive, and two writers never receive the same value.
    """
    if by < 1:
        raise ValueError("a counter only grows")
    row = tx.one(
        "INSERT INTO counters (name, value) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = value + excluded.value RETURNING value",
        [name, by],
    )
    assert row is not None
    return int(row["value"])


def counter_cas(tx: Tx, name: str, expected: int, new: int) -> bool:
    """Set the counter to ``new`` only if it is ``expected`` (an absent counter reads 0).

    A lost compare-and-set changes nothing: a missing counter is created only when it wins.
    """
    if expected == 0:
        cur = tx.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) ON CONFLICT DO NOTHING", [name, new]
        )
        if cur.rowcount == 1:
            return True
    cur = tx.execute(
        "UPDATE counters SET value = ? WHERE name = ? AND value = ?", [new, name, expected]
    )
    return cur.rowcount == 1


# -- leases ------------------------------------------------------------------------------------

# Liveness of a holder identity, injected by the process layer (TODO(#7)): True = alive,
# False = dead (including a different boot id), None = the check was inconclusive.
IsAlive = Callable[[str], bool | None]


def _check_lease_name(name: str) -> None:
    if not _LEASE_NAME_RE.match(name):
        raise ValueError(f"not a lease name: {name!r}")


def _check_lease_time(value: str) -> None:
    if not _LEASE_TIME_RE.match(value):
        raise ValueError(f"not a fixed-width UTC timestamp (2026-10-31T12:00:00.000Z): {value!r}")


def take_lease(
    store: Store, name: str, holder: str, *, now: str, expires_at: str, is_alive: IsAlive
) -> bool:
    """Take (or refresh, if ``holder`` already holds it) a lease with one compare-and-set.

    A lease whose holder is alive is never taken; one whose holder is dead is free; expiry frees
    it only when the liveness check is inconclusive (interface.md, Leases). ``now`` and
    ``expires_at`` are fixed-width UTC timestamps, so they compare as strings. The liveness check
    runs outside any transaction; the single ``UPDATE``/``INSERT`` carries the observed row, so
    a concurrent taker makes it lose rather than steal. When the row changed between the
    observation and the compare-and-set (e.g. it was released), the take observes again, up to
    3 times in all, before it returns ``False``.
    """
    _check_lease_name(name)
    _check_lease_time(now)
    _check_lease_time(expires_at)
    for _ in range(_TAKE_LEASE_TRIES):
        won = _take_lease_once(
            store, name, holder, now=now, expires_at=expires_at, is_alive=is_alive
        )
        if won is not None:
            return won
    return False


def _take_lease_once(
    store: Store, name: str, holder: str, *, now: str, expires_at: str, is_alive: IsAlive
) -> bool | None:
    """One observe-then-compare-and-set; ``None`` when the row changed in between."""
    row = store.query_one("SELECT holder_identity, expires_at FROM leases WHERE name = ?", [name])
    if row is None:
        with store.write() as tx:
            cur = tx.execute(
                "INSERT INTO leases (name, holder_identity, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO NOTHING",
                [name, holder, expires_at],
            )
            return True if cur.rowcount == 1 else None
    observed_holder, observed_expiry = str(row["holder_identity"]), str(row["expires_at"])
    if observed_holder != holder:
        alive = is_alive(observed_holder)
        if alive is True:
            return False
        if alive is None and observed_expiry > now:
            return False
    with store.write() as tx:
        cur = tx.execute(
            "UPDATE leases SET holder_identity = ?, expires_at = ? "
            "WHERE name = ? AND holder_identity = ? AND expires_at = ?",
            [holder, expires_at, name, observed_holder, observed_expiry],
        )
        return True if cur.rowcount == 1 else None


def lease_renew(tx: Tx, name: str, holder: str, expires_at: str) -> bool:
    """Extend a lease; ``False`` when ``holder`` no longer holds it (it was taken over)."""
    _check_lease_name(name)
    _check_lease_time(expires_at)
    cur = tx.execute(
        "UPDATE leases SET expires_at = ? WHERE name = ? AND holder_identity = ?",
        [expires_at, name, holder],
    )
    return cur.rowcount == 1


def lease_release(tx: Tx, name: str, holder: str) -> bool:
    """Delete the lease only if ``holder`` holds it."""
    _check_lease_name(name)
    cur = tx.execute("DELETE FROM leases WHERE name = ? AND holder_identity = ?", [name, holder])
    return cur.rowcount == 1
