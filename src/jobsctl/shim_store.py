"""The shim's writes (docs/interface.md, Start sequence): every one a compare-and-set on the
attempt's `claim_id` (design.md D27), so a stale shim whose attempt was requeued and claimed again
matches nothing.

Functions that take a `Tx` run inside the caller's `BEGIN IMMEDIATE` transaction and return the
log records to append after the commit (a process never logs inside a transaction). The ones that
take a `Store` open their own transaction. Nothing here spawns or signals a process.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jobsctl.auto_retry import plan_requeue
from jobsctl.claims import LogRecord, transition_record
from jobsctl.config import Config
from jobsctl.hook_events import write_post_event
from jobsctl.job_view import effective_settings, job_object
from jobsctl.output_capture import TailSnapshot
from jobsctl.output_store import save_tail
from jobsctl.shim_exit import ExitRecord, Outcome, forced_outcome, post_hook
from jobsctl.store_db import Store, Tx
from jobsctl.store_ops import transition_job_by_claim, update_attempt_by_claim

_CLAIM_ROW = (
    "SELECT j.*, a.claim_id AS a_claim_id, a.status AS a_status, a.claimed_by AS a_claimed_by, "
    "a.shim_identity AS a_shim_identity, a.shim_confirmed_at AS a_shim_confirmed_at, "
    "a.force_requested_by AS a_force_requested_by "
    "FROM jobs j JOIN attempts a ON a.job_id = j.id AND a.n = j.attempt "
    "WHERE j.id = ? AND a.claim_id = ?"
)


@dataclass(frozen=True)
class ClaimView:
    """The job and its current attempt, when the attempt carries this shim's `claim_id`."""

    row: sqlite3.Row

    @property
    def status(self) -> str:
        return str(self.row["status"])

    @property
    def attempt(self) -> int:
        return int(self.row["attempt"])

    @property
    def queue(self) -> str:
        return str(self.row["queue"])

    @property
    def shim_identity(self) -> str | None:
        value = self.row["a_shim_identity"]
        return None if value is None else str(value)

    @property
    def claimed_by(self) -> str:
        return str(self.row["a_claimed_by"])

    @property
    def force_requested_by(self) -> str | None:
        value = self.row["a_force_requested_by"]
        return None if value is None else str(value)


def read_claim(store: Store, job_id: str, claim_id: str) -> ClaimView | None:
    """`None` when the job's current attempt is not the one `claim_id` names (a stale shim)."""
    row = store.query_one(_CLAIM_ROW, [job_id, claim_id])
    return None if row is None else ClaimView(row)


def read_claim_in(tx: Tx, job_id: str, claim_id: str) -> ClaimView | None:
    row = tx.one(_CLAIM_ROW, [job_id, claim_id])
    return None if row is None else ClaimView(row)


def confirm(store: Store, job_id: str, claim_id: str, identity: str, now: str) -> bool:
    """Start sequence step 2: the claim is this shim's and the identity the pass recorded is its
    own. Sets `shim_confirmed_at` and resets the job's `spawn_failures`; `False` if it is not."""
    with store.write() as tx:
        view = read_claim_in(tx, job_id, claim_id)
        if view is None or view.status != "starting" or view.shim_identity != identity:
            return False
        won = update_attempt_by_claim(
            tx, job_id, claim_id, {"shim_confirmed_at": now}, job_status="starting"
        )
        if won:
            tx.execute("UPDATE jobs SET spawn_failures = 0 WHERE id = ?", [job_id])
        return won


def effective_snapshot(config: Config, row: sqlite3.Row) -> dict[str, Any]:
    """The `timeout`, `max_retries` and `kill_grace` this attempt runs with."""
    overrides = json.loads(row["overrides"])
    return effective_settings(config, row["queue"], overrides, None)


def record_effective(store: Store, job_id: str, claim_id: str, effective: dict[str, Any]) -> bool:
    with store.write() as tx:
        return update_attempt_by_claim(
            tx,
            job_id,
            claim_id,
            {"effective": json.dumps(effective, sort_keys=True)},
            job_status="starting",
        )


def _post_event(
    tx: Tx, config: Config, hook: str, job_id: str, attempt: int, previous: str, now: str
) -> None:
    after = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
    row = tx.one("SELECT * FROM attempts WHERE job_id = ? AND n = ?", [job_id, attempt])
    assert after is not None
    job = job_object(tx, after, config, env_file_unavailable=False, now=now)
    write_post_event(tx, config, hook, job=job, previous_status=previous, attempt=row, now=now)


def fail_starting(
    tx: Tx,
    config: Config,
    view: ClaimView,
    claim_id: str,
    *,
    reason: str,
    exit_record: ExitRecord | None,
    now: str,
    resolved_path: str | None = None,
) -> tuple[bool, list[LogRecord]]:
    """`starting` -> `failed` with `reason` (`cwd_missing`, `missing_env`, `spawn_failed`,
    `terminated`) and its `post-fail` event, in the caller's transaction. `missing_env`,
    `spawn_failed` and `terminated` are eligible for automatic retry: the job then returns to
    `queued` with `not_before` instead (`auto_retry.plan_requeue`); the attempt is `failed` either
    way.
    """
    job_id = str(view.row["id"])
    plan = plan_requeue(tx, config, job_id, claim_id, reason=reason, now=now)
    won = transition_job_by_claim(
        tx,
        job_id,
        claim_id=claim_id,
        expect_status="starting",
        to_status="failed" if plan is None else "queued",
        updates={"reason": reason, "ended_at": now, "status_changed_at": now, "not_before": None}
        if plan is None
        else plan,
    )
    if not won:
        return False, []
    updates: dict[str, Any] = {"status": "failed", "reason": reason, "ended_at": now}
    updates["exit"] = None if exit_record is None else json.dumps(exit_record.as_json())
    if resolved_path is not None:
        updates["resolved_path"] = resolved_path
    update_attempt_by_claim(tx, job_id, claim_id, updates)
    _post_event(tx, config, "post-fail", job_id, view.attempt, "starting", now)
    return True, [
        transition_record(
            job_id,
            view.queue,
            "starting",
            "failed" if plan is None else "queued",
            view.attempt,
            reason=reason if plan is None else None,
            attempt_status="failed",
            attempt_reason=reason,
            retry_pending=plan is not None,
        )
    ]


def begin_running(
    tx: Tx,
    config: Config,
    view: ClaimView,
    claim_id: str,
    *,
    job_identity: str,
    pgid: int,
    resolved_path: str,
    now: str,
) -> tuple[bool, list[LogRecord]]:
    """Start sequence step 7, the database half: `starting` -> `running` with the job's identity
    and process group, and its `post-start` event."""
    job_id = str(view.row["id"])
    won = transition_job_by_claim(
        tx,
        job_id,
        claim_id=claim_id,
        expect_status="starting",
        to_status="running",
        updates={"status_changed_at": now},
    )
    if not won:
        return False, []
    update_attempt_by_claim(
        tx,
        job_id,
        claim_id,
        {
            "status": "running",
            "running_at": now,
            "job_identity": job_identity,
            "pgid": pgid,
            "resolved_path": resolved_path,
        },
    )
    _post_event(tx, config, "post-start", job_id, view.attempt, "starting", now)
    return True, [transition_record(job_id, view.queue, "starting", "running", view.attempt)]


@dataclass(frozen=True)
class FlushResult:
    still_running: bool
    force_requested_by: str | None


def flush_tail(
    store: Store, job_id: str, claim_id: str, snapshot: TailSnapshot, now: str
) -> FlushResult:
    """Write the live tail (compare-and-set on the claim and `running`) and read the attempt's
    `force_requested_by` (Start sequence step 8)."""
    with store.write() as tx:
        view = read_claim_in(tx, job_id, claim_id)
        if view is None or view.status != "running":
            return FlushResult(False, None)
        save_tail(tx, job_id, view.attempt, snapshot, now)
        return FlushResult(True, view.force_requested_by)


@dataclass(frozen=True)
class FinalResult:
    committed: bool
    outcome: Outcome | None
    records: tuple[LogRecord, ...] = ()


def finish_running(
    store: Store,
    config: Config,
    job_id: str,
    claim_id: str,
    *,
    outcome: Outcome,
    exit_record: ExitRecord,
    leftovers: int | None,
    tail: TailSnapshot,
    clock: Callable[[], str],
) -> FinalResult:
    """Start sequence step 9, the final transaction: the transition, the final tail and the hook
    events in one commit. It reads `force_requested_by`: a committed force request makes the end
    `canceled` (`forced`) whatever the exit (interface.md, Status transitions).

    `outcome` is what the shim decided: the exit's natural outcome, or `timeout` / `terminated` when
    its signal sequence ran. The force request read here overrides it. A failed attempt that
    automatic retry applies to (`auto_retry.plan_requeue`) returns the job to `queued` with
    `not_before` instead of ending it; the attempt is `failed` either way.
    """
    with store.write() as tx:
        now = clock()  # read inside the transaction, after the write lock is held
        view = read_claim_in(tx, job_id, claim_id)
        if view is None or view.status != "running":
            return FinalResult(False, None)
        if view.force_requested_by is not None:
            outcome = forced_outcome()
        plan = None
        if outcome.status == "failed" and outcome.reason is not None:
            plan = plan_requeue(tx, config, job_id, claim_id, reason=outcome.reason, now=now)
        job_updates: dict[str, Any] = {"status_changed_at": now, "not_before": None}
        if plan is not None:
            job_updates = plan
        elif outcome.terminal:
            job_updates.update(reason=outcome.reason, ended_at=now)
        else:
            job_updates.update(hold_reason=outcome.hold_reason)
        won = transition_job_by_claim(
            tx,
            job_id,
            claim_id=claim_id,
            expect_status="running",
            to_status=outcome.status if plan is None else "queued",
            updates=job_updates,
        )
        if not won:
            return FinalResult(False, None)
        update_attempt_by_claim(
            tx,
            job_id,
            claim_id,
            {
                "status": outcome.status,
                "reason": outcome.reason,
                "hold_reason": outcome.hold_reason,
                "ended_at": now,
                "exit": json.dumps(exit_record.as_json()),
                "leftovers": leftovers,
            },
        )
        save_tail(tx, job_id, view.attempt, tail, now)
        _post_event(tx, config, post_hook(outcome), job_id, view.attempt, "running", now)
        record = transition_record(
            job_id,
            view.queue,
            "running",
            outcome.status if plan is None else "queued",
            view.attempt,
            reason=outcome.reason if plan is None else None,
            hold_reason=outcome.hold_reason,
            attempt_status=outcome.status,
            attempt_reason=outcome.reason,
            retry_pending=plan is not None,
        )
        return FinalResult(True, outcome, (record,))
