"""Claiming a job for a shim, and the ways a claim ends before the shim confirmed it.

Everything here runs inside the caller's ``BEGIN IMMEDIATE`` transaction (``Tx``) and returns
the log records to append after the commit (``LogRecord``), so a pass never logs inside a
transaction (docs/interface.md, Pass; design.md D27).

* :func:`claim_jobs` is the claim loop: it reads the counts, picks jobs with
  :func:`jobsctl.order.select_next`, adds every claim to the counts it works from and records, per
  claim, the new attempt with the claiming pass's identity and a random ``claim_id``. The jobs are
  ``starting`` when the transaction commits, before any shim is spawned.
* :func:`fail_spawn` returns a job whose shim could not be spawned to ``queued`` (the 3rd
  consecutive failure ends it ``failed``, ``spawn_failed``), as a compare-and-set on ``starting``
  and the ``claim_id``.
* :func:`requeue_orphaned` does the same for a shim that exited without confirming its identity
  (``claim_orphaned``), which does not count as a spawn failure.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from jobsctl import __version__
from jobsctl.actors import RESERVED_ACTOR
from jobsctl.config import Config
from jobsctl.hook_events import write_post_event
from jobsctl.job_view import job_object
from jobsctl.order import Snapshot, key_between, load_snapshot, select_next
from jobsctl.store_db import Tx
from jobsctl.store_ops import transition_job, transition_job_by_claim, update_attempt_by_claim

SPAWN_FAILURE_LIMIT: Final = 3


@dataclass(frozen=True)
class LogRecord:
    event: str
    level: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class Claim:
    job_id: str
    queue: str
    attempt: int
    claim_id: str


@dataclass(frozen=True)
class SpawnFailure:
    """What :func:`fail_spawn` did: `final` when the 3rd consecutive failure ended the job."""

    job_id: str
    attempt: int
    final: bool
    message: str


def new_claim_id() -> str:
    return secrets.token_hex(16)


def transition_record(
    job_id: str, queue: str, from_status: str, to_status: str, attempt: int, **extra: Any
) -> LogRecord:
    fields: dict[str, Any] = {
        "job_id": job_id,
        "queue": queue,
        "from": from_status,
        "to": to_status,
        "reason": None,
        "hold_reason": None,
        "attempt": attempt,
        "actor": RESERVED_ACTOR,
        "decision_ids": [],
    }
    fields.update(extra)
    return LogRecord("transition", "info", fields)


# -- the claim loop ----------------------------------------------------------------------------


def _with_claim(snapshot: Snapshot, queue: str, job_id: str) -> Snapshot:
    """`snapshot` with `job_id` no longer queued and counted as running in `queue`."""
    queues = []
    for state in snapshot.queues:
        if state.name == queue:
            state = dataclasses.replace(
                state,
                running=state.running + 1,
                queued=tuple(c for c in state.queued if c.id != job_id),
            )
        queues.append(state)
    return dataclasses.replace(snapshot, queues=tuple(queues))


def claim_jobs(
    tx: Tx,
    config: Config,
    *,
    now: str,
    claimed_by: str,
    env_file_unavailable: Callable[[], bool],
    limit: int | None,
) -> tuple[list[Claim], list[LogRecord]]:
    """Claim jobs one at a time until none can start or `limit` claims were made (`None`: no
    limit). The counts come from one snapshot read in `tx`; each claim is added to them."""
    snapshot = load_snapshot(tx, config)
    claims: list[Claim] = []
    records: list[LogRecord] = []
    # `limit` counts claims: the caller passes what is left of `--max-starts`
    unavailable: bool | None = None
    while limit is None or len(claims) < limit:
        if unavailable is None and any(c.needs_env_file for q in snapshot.queues for c in q.queued):
            unavailable = env_file_unavailable()
        chosen = select_next(snapshot, now=now, env_file_unavailable=bool(unavailable))
        if chosen is None:
            break
        row = tx.one("SELECT attempt FROM jobs WHERE id = ?", [chosen.job_id])
        assert row is not None
        number = int(row["attempt"]) + 1
        won = transition_job(
            tx,
            chosen.job_id,
            expect_status="queued",
            expect_attempt=number - 1,
            to_status="starting",
            updates={"attempt": number, "status_changed_at": now},
        )
        if not won:  # unreachable inside BEGIN IMMEDIATE; the compare-and-set is the guard
            snapshot = _with_claim(snapshot, chosen.queue, chosen.job_id)
            continue
        claim_id = new_claim_id()
        tx.execute(
            "INSERT INTO attempts (job_id, n, claim_id, status, claimed_at, claimed_by, "
            "jobsctl_version) VALUES (?, ?, ?, 'starting', ?, ?, ?)",
            [chosen.job_id, number, claim_id, now, claimed_by, __version__],
        )
        claims.append(Claim(chosen.job_id, chosen.queue, number, claim_id))
        records.append(transition_record(chosen.job_id, chosen.queue, "queued", "starting", number))
        snapshot = _with_claim(snapshot, chosen.queue, chosen.job_id)
    return claims, records


# -- claims that end before the shim confirmed them ----------------------------------------------


def _unique_position(tx: Tx, queue: str, job_id: str, key: str) -> str:
    """`key`, or the nearest key after it that no other queued or held job of the queue uses
    (position keys are unique among live jobs; a job that was `starting` may have been tied by a
    newer job)."""
    live = sorted(
        str(r["position"])
        for r in tx.all(
            "SELECT position FROM jobs WHERE queue = ? AND status IN ('queued', 'held') "
            "AND id != ?",
            [queue, job_id],
        )
    )
    while key in live:
        higher = next((p for p in live if p > key), None)
        key = key_between(key, higher)
    return key


def _requeue(tx: Tx, row: Any, claim_id: str, now: str, updates: dict[str, Any]) -> bool:
    """`starting` -> `queued`, position and `enqueued_at` kept, the attempt row deleted."""
    key = _unique_position(tx, row["queue"], row["id"], row["position"])
    won = transition_job_by_claim(
        tx,
        row["id"],
        claim_id=claim_id,
        expect_status="starting",
        to_status="queued",
        updates={
            "attempt": row["attempt"] - 1,
            "position": key,
            "status_changed_at": now,
            **updates,
        },
    )
    if won:
        tx.execute(
            "DELETE FROM attempts WHERE job_id = ? AND n = ? AND claim_id = ?",
            [row["id"], row["attempt"], claim_id],
        )
    return won


def _starting_row(tx: Tx, job_id: str, claim_id: str) -> Any:
    return tx.one(
        "SELECT j.* FROM jobs j JOIN attempts a ON a.job_id = j.id AND a.n = j.attempt "
        "WHERE j.id = ? AND j.status = 'starting' AND a.claim_id = ?",
        [job_id, claim_id],
    )


def fail_spawn(
    tx: Tx, config: Config, job_id: str, claim_id: str, *, now: str, message: str
) -> tuple[SpawnFailure | None, list[LogRecord]]:
    """The shim of this claim could not be spawned. `None` when the claim is gone (the job was
    canceled meanwhile and keeps its attempt)."""
    row = _starting_row(tx, job_id, claim_id)
    if row is None:
        return None, []
    failures = int(row["spawn_failures"]) + 1
    number, queue = int(row["attempt"]), str(row["queue"])
    detail = {"message": message, "job_id": job_id, "queue": queue, "attempt": number}
    if failures < SPAWN_FAILURE_LIMIT:
        if not _requeue(tx, row, claim_id, now, {"spawn_failures": failures}):
            return None, []
        return SpawnFailure(job_id, number, False, message), [
            LogRecord("shim_spawn_failed", "warn", {**detail, "details": {"failures": failures}})
        ]
    # the 3rd consecutive failure is final: failed, spawn_failed, the attempt row kept
    won = transition_job_by_claim(
        tx,
        job_id,
        claim_id=claim_id,
        expect_status="starting",
        to_status="failed",
        updates={
            "reason": "spawn_failed",
            "ended_at": now,
            "status_changed_at": now,
            "spawn_failures": failures,
        },
    )
    if not won:
        return None, []
    update_attempt_by_claim(
        tx, job_id, claim_id, {"status": "failed", "reason": "spawn_failed", "ended_at": now}
    )
    after = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
    attempt = tx.one("SELECT * FROM attempts WHERE job_id = ? AND n = ?", [job_id, number])
    assert after is not None
    job = job_object(tx, after, config, env_file_unavailable=False, now=now)
    write_post_event(
        tx, config, "post-fail", job=job, previous_status="starting", attempt=attempt, now=now
    )
    return SpawnFailure(job_id, number, True, message), [
        LogRecord("shim_spawn_failed", "error", {**detail, "details": {"failures": failures}}),
        transition_record(
            job_id,
            queue,
            "starting",
            "failed",
            number,
            reason="spawn_failed",
            attempt_status="failed",
            attempt_reason="spawn_failed",
            retry_pending=False,
        ),
    ]


def requeue_orphaned(
    tx: Tx, job_id: str, claim_id: str, *, now: str
) -> tuple[bool, list[LogRecord]]:
    """A shim this pass spawned exited without confirming its identity (`claim_orphaned`,
    `requeued`): the job is `queued` again, position and `enqueued_at` kept, attempt deleted, no
    spawn failure counted. Only while the shim has not confirmed (`shim_confirmed_at` null)."""
    row = _starting_row(tx, job_id, claim_id)
    if row is None:
        return False, []
    confirmed = tx.one(
        "SELECT shim_confirmed_at FROM attempts WHERE job_id = ? AND claim_id = ?",
        [job_id, claim_id],
    )
    if confirmed is None or confirmed["shim_confirmed_at"] is not None:
        return False, []
    number, queue = int(row["attempt"]), str(row["queue"])
    if not _requeue(tx, row, claim_id, now, {}):
        return False, []
    return True, [
        LogRecord(
            "reconcile",
            "info",
            {
                "job_id": job_id,
                "queue": queue,
                "attempt": number,
                "finding": "claim_orphaned",
                "action": "requeued",
            },
        )
    ]
