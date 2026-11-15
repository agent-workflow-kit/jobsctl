"""`cancel --force` of a running job (interface.md, cancel, Human-only flags, Signal sequence).

Three steps, in this order, and the order is the safety property:

1. `request_force` (inside the command's write transaction): commits `force_requested_by` on the
   job's current attempt and the `cancel_force` audit record's pre-check. A request already
   committed is left alone (the first requester stays, no new audit record).
2. `deliver_request` (after the commit): SIGTERM to the shim, only through
   `proc_signal.signal_process`, which verifies the shim's recorded identity (boot id, PID, start)
   on a pidfd before sending. The SIGTERM is only a wake-up: the shim reads the committed request
   and runs the signal sequence. SIGTERM sent before the commit would end the job `failed`
   (`terminated`). Without a verified live shim (it is gone, the job is `shim_lost`, or the
   identity is unusable) a detached `_kill` is spawned instead, unless a live process holds the
   `job:<id>` lease.
3. `wait_until_ended`: polls read-only until the job leaves `running`, at most the wait timeout.

Nothing here ever signals a PID taken from the database without the identity check.
"""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Final

from jobsctl import gateway
from jobsctl.cli_registry import Context
from jobsctl.env_file import load_env_file
from jobsctl.errors import JobsctlError
from jobsctl.job_lookup import find_job, select_live_jobs
from jobsctl.job_view import effective_settings, job_object
from jobsctl.leases import live_holder, parse_identity
from jobsctl.log_files import check_appendable
from jobsctl.proc_host import default_host
from jobsctl.proc_signal import Result, signal_process
from jobsctl.spawn import spawn_internal
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from jobsctl.store_ops import update_attempt_by_claim
from jobsctl.timestamps import format_timestamp
from jobsctl.transitions_manual import Applied, Op, job_audit_inputs

POLL_SECONDS: Final = 1.0
_TERM: Final = 15  # signal.SIGTERM


@dataclass(frozen=True)
class ForceTarget:
    """A committed force request, as read at the commit: what the delivery needs."""

    job_id: str
    shim_identity: str | None
    shim_lost: bool
    kill_grace: float


def apply_reconcile(op: Op, row: sqlite3.Row) -> sqlite3.Row:
    """The Reconcile rules applied to the job before the request (with `--dry-run`: evaluated,
    not applied). Returns the job's row afterwards.

    TODO(#22): reconcile's rules for one job (result_file_finalized, boot_changed, job_gone,
    shim_lost, ...) run here; a job they finalize is reported in its finalized state and gets no
    request.
    """
    return row


def default_timeout(target: ForceTarget) -> int:
    """2 x the effective `kill_grace` (the shim's snapshot) + 10 seconds, rounded up."""
    return math.ceil(2 * target.kill_grace + 10)


def _kill_grace(op: Op, job: sqlite3.Row, attempt: sqlite3.Row) -> float:
    """The attempt's snapshot value (the shim does `float(...)` of it); an absent, null or
    malformed snapshot falls back to the job's configured value (queue settings and overrides)."""
    try:
        grace = json.loads(attempt["effective"] or "{}").get("kill_grace")
    except (ValueError, AttributeError):
        grace = None
    if isinstance(grace, (int, float)) and not isinstance(grace, bool) and math.isfinite(grace):
        return grace
    configured = effective_settings(op.config, job["queue"], json.loads(job["overrides"]), None)
    return configured["kill_grace"]


def request_force(op: Op, row: sqlite3.Row, targets: list[ForceTarget]) -> Applied:
    """Commit the force request of the running job `row` (the human check has passed).

    `targets` receives the delivery data once the transaction has committed (also for a request
    that was already committed: a repeated request re-sends the wake-up). With a dry run nothing
    is written and no target is added."""
    tx = op.tx
    assert tx is not None
    attempt = tx.one(
        "SELECT * FROM attempts WHERE job_id = ? AND n = ?", [row["id"], row["attempt"]]
    )
    if attempt is None:
        raise JobsctlError("internal", f"{row['id']} is running but has no attempt")
    existing = attempt["force_requested_by"]
    target = ForceTarget(
        str(row["id"]),
        attempt["shim_identity"],
        bool(row["shim_lost"]),
        _kill_grace(op, row, attempt),
    )
    if existing is not None:  # the first requester stays; no new audit record
        if op.write:
            tx.after_commit(lambda: targets.append(target))
        return Applied(False, op.render(row))
    if not op.write:
        job = op.render(row)
        job["force_requested_by"] = op.actor
        return Applied(True, job, "running")
    assert op.audit_path is not None
    check_appendable(op.audit_path)  # fail before committing a request whose audit cannot land
    if not update_attempt_by_claim(
        tx, row["id"], attempt["claim_id"], {"force_requested_by": op.actor}, job_status="running"
    ):
        raise JobsctlError("internal", f"{row['id']} changed concurrently")
    after = tx.one("SELECT * FROM jobs WHERE id = ?", [row["id"]])
    assert after is not None
    op.audits.append(
        {
            "action": "cancel_force",
            "actor": op.actor,
            "target": row["id"],
            "inputs": job_audit_inputs(row),
            "result": "running",  # the request leaves the job running until the shim ends it
            "decision_id": op.decision_ids,
        }
    )
    tx.after_commit(lambda: targets.append(target))
    return Applied(True, op.render(after), "running")


# -- delivery ------------------------------------------------------------------------------------


def spawn_kill(ctx: Context, job_id: str) -> subprocess.Popen[bytes]:
    """Start a detached `jobsctl _kill <id>` (`jobsctl.kill`; the caller reaps this exact handle)."""
    return spawn_internal(resolve_state_dir(), ctx.config, ctx.config_path, "_kill", [job_id])


def _warn(message: str) -> None:
    sys.stderr.write(f"jobsctl: warning: {message}\n")
    sys.stderr.flush()


def _needs_kill(ctx: Context, job_id: str) -> bool:
    """Still `running` and no live process holds the `job:<id>` lease."""
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
        found = store.query_one("SELECT status FROM jobs WHERE id = ?", [job_id])
        if found is None or found["status"] != "running":
            return False
        return live_holder(store, f"job:{job_id}", default_host(), gateway.utc_now()) is None


def deliver_request(ctx: Context, target: ForceTarget) -> subprocess.Popen[bytes] | None:
    """Wake the shim, or start `_kill` when no live shim can be woken. Returns the `_kill`
    handle when one was started."""
    ident = None if target.shim_lost else parse_identity(target.shim_identity or "")
    if ident is None and not target.shim_lost:  # nothing to verify: like UNKNOWN, not proven gone
        _warn(f"the shim of {target.job_id} has no usable recorded identity; nothing was sent")
        return None
    if ident is not None:
        outcome = signal_process(default_host(), ident, _TERM)
        if outcome.sent:
            return None
        if (
            outcome.result is not Result.NOT_ALIVE
        ):  # unknown, denied or an OS error: not proven gone
            _warn(f"the shim of {target.job_id} could not be signaled ({outcome.result.value})")
            return None
    if not _needs_kill(ctx, target.job_id):
        return None
    return spawn_kill(ctx, target.job_id)


# -- waiting -------------------------------------------------------------------------------------


def fresh_job(ctx: Context, job_id: str) -> dict[str, Any]:
    """The job as it is now (read-only)."""
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
        with store.read() as tx:
            row = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
            if row is None:
                raise JobsctlError("not_found", f"job {job_id} no longer exists (it was pruned)")
            unavailable = (
                row["status"] == "queued"
                and load_env_file(ctx.config.env_file).status == "unavailable"
            )
            return job_object(
                tx,
                row,
                ctx.config,
                env_file_unavailable=unavailable,
                now=format_timestamp(gateway.utc_now()),
            )


def cancel_pending(job_id: str, why: str) -> JobsctlError:
    return JobsctlError(
        "cancel_pending",
        f"{job_id} {why}; its force request stays committed",
        details={"job_id": job_id},
    )


def wait_until_ended(
    ctx: Context,
    target: ForceTarget,
    timeout: int,
    *,
    kill: subprocess.Popen[bytes] | None = None,
) -> None:
    """Poll until the job is no longer `running`; `cancel_pending` (exit 12) after `timeout`
    seconds. `kill` is reaped (by its exact handle) at every iteration."""
    deadline = gateway.monotonic() + timeout
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
        while True:
            store.raise_if_newer()
            with store.read() as tx:
                found = tx.one("SELECT status FROM jobs WHERE id = ?", [target.job_id])
            if found is None or found["status"] != "running":
                return
            remaining = deadline - gateway.monotonic()
            if remaining <= 0:
                raise cancel_pending(target.job_id, f"is still running after {timeout} seconds")
            if kill is not None:
                kill.poll()  # reaps it once it exited
            gateway.sleep(min(POLL_SECONDS, remaining))


def job_status(ctx: Context, job_id: str) -> str | None:
    """The job's status now (read-only); `None` when it is gone."""
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
        found = store.query_one("SELECT status FROM jobs WHERE id = ?", [job_id])
    return None if found is None else str(found["status"])


def committed_requests(ctx: Context, job_id: str | None) -> list[str]:
    """The ids of the running jobs that carry a committed force request, selected like the
    command does. Read-only; any failure to look means none. Taken before the command's write
    and again after an interrupt, the difference is what this invocation committed."""
    args = ctx.args
    found: list[str] = []
    try:
        with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
            with store.read() as tx:
                rows = (
                    [find_job(tx, job_id)]
                    if job_id is not None
                    else select_live_jobs(
                        tx, queue=args.queue, labels=args.label or (), unique_key=args.unique_key
                    )
                )
                for row in rows:
                    if row["status"] != "running":
                        continue
                    attempt = tx.one(
                        "SELECT force_requested_by FROM attempts WHERE job_id = ? AND n = ?",
                        [row["id"], row["attempt"]],
                    )
                    if attempt is not None and attempt["force_requested_by"] is not None:
                        found.append(str(row["id"]))
    except (JobsctlError, sqlite3.Error, OSError):
        return []
    return found
