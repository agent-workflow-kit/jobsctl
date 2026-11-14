"""The transitions a human or a script makes by hand on jobs that are not running:
`hold`, `release` and `cancel` of a queued, held or starting job (interface.md, Status
transitions).

Each is one compare-and-set on `(id, status, attempt)` inside the command's single
`BEGIN IMMEDIATE` transaction, with the post-hook outbox event written in the same transaction.
Log and audit records are appended only after the commit (`run_manual`). With `--dry-run` the
same decisions run in a read transaction on a read-only connection and the projected job is
built from the row with the changes merged in; nothing is written.

TODO(#14): `release` and `cancel` of a `starting` job request a pass once the runner exists.
TODO(#15): the shim reacts to a `starting` job canceled under it (stops its hook, delivers).
TODO(#24): hook delivery (the synchronous hooks of `hold` and `cancel` run after the commit).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jobsctl import gateway
from jobsctl.cli_registry import Context
from jobsctl.config import Config
from jobsctl.env_file import EnvFile
from jobsctl.errors import JobsctlError, refused
from jobsctl.hook_events import write_post_event
from jobsctl.job_view import job_object, job_row
from jobsctl.log_records import argv_sha256
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.store_dir import resolve_state_dir
from jobsctl.store_ops import transition_job, update_attempt_by_claim
from jobsctl.timestamps import format_timestamp

TERMINAL = ("done", "failed", "canceled")


@dataclass
class Op:
    """One command invocation: the transaction (`None`: a dry run found no database), the clock
    reading, who acts, and the records to append after the commit."""

    tx: Tx | None
    config: Config
    actor: str
    decision_ids: list[str]
    now: str
    write: bool
    load_env: Callable[[], EnvFile]
    logs: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    _env_unavailable: bool | None = None

    def env_unavailable(self) -> bool:
        if self._env_unavailable is None:
            self._env_unavailable = self.load_env().status == "unavailable"
        return self._env_unavailable

    def render(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        """The job object of a row (or of a projected row)."""
        assert self.tx is not None
        needs_env = row["status"] == "queued" and bool(json.loads(row["pass_env"]))
        return job_object(
            self.tx,
            row,  # type: ignore[arg-type]  # job_object only indexes by column name
            self.config,
            env_file_unavailable=needs_env and self.env_unavailable(),
            now=self.now,
        )


@dataclass
class Applied:
    changed: bool
    job: dict[str, Any]


# -- running a command -----------------------------------------------------------------------


def _emit(writer: RecordWriter, op: Op) -> None:
    """Append the records after the commit. The change is committed, so a failed append must
    not fail the command (a retry would find the work done); it warns on stderr, naming only the
    exception's type."""
    failed = 0
    last = ""
    for audit in op.audits:
        try:
            writer.audit(**audit)
        except Exception as exc:
            failed, last = failed + 1, type(exc).__name__
    for event, fields, level in op.logs:
        try:
            writer.log(event, level=level, fields=fields)
        except Exception as exc:
            failed, last = failed + 1, type(exc).__name__
    if failed:
        sys.stderr.write(
            f"jobsctl: warning: the change was committed but appending to the log or audit "
            f"file failed ({last}); {failed} record(s) were not written\n"
        )
        sys.stderr.flush()


def run_manual[T](ctx: Context, body: Callable[[Op], T], *, load_env: Callable[[], EnvFile]) -> T:
    """Run `body` in one write transaction, or with `--dry-run` in a read transaction on a
    read-only connection that creates, repairs and migrates nothing (a missing or uninitialized
    database means no job exists)."""
    now = format_timestamp(gateway.utc_now())

    def make(tx: Tx | None, write: bool) -> Op:
        return Op(
            tx=tx,
            config=ctx.config,
            actor=ctx.actor,
            decision_ids=list(ctx.args.decision_id),
            now=now,
            write=write,
            load_env=load_env,
        )

    if ctx.args.dry_run:
        if not os.path.lexists(resolve_state_dir() / "state.db"):
            return body(make(None, False))
        with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
            if store.schema_version == 0:
                return body(make(None, False))
            with store.read() as tx:
                return body(make(tx, False))
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        writer = RecordWriter(store)
        with store.write() as tx:
            op = make(tx, True)
            result = body(op)
            tx.after_commit(lambda: _emit(writer, op))
        return result


# -- the shared transition ---------------------------------------------------------------------


def _attempt_row(tx: Tx, row: sqlite3.Row | dict[str, Any]) -> sqlite3.Row | None:
    return tx.one("SELECT * FROM attempts WHERE job_id = ? AND n = ?", [row["id"], row["attempt"]])


def _audit_inputs(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "argv_sha256": argv_sha256(json.loads(row["argv"])),
        "attempt": row["attempt"],
        "unique_key": row["unique_key"],
        "queue": row["queue"],
    }


def _transition(
    op: Op,
    row: sqlite3.Row,
    *,
    to: str,
    updates: dict[str, Any],
    attempt_updates: dict[str, Any] | None = None,
    hook: str | None = None,
    audit: str | None = None,
) -> Applied:
    """Move `row` to `to`. `attempt_updates` is set when the transition changes the job's
    current attempt; `hook` names the post hook whose event is written with it; `audit` the
    audit action recorded for it."""
    tx = op.tx
    assert tx is not None
    updates = {"status_changed_at": op.now, **updates}
    attempt = _attempt_row(tx, row) if attempt_updates is not None else None
    if attempt_updates is not None and attempt is None:
        raise JobsctlError("internal", f"{row['id']} has no attempt {row['attempt']}")
    merged = {**dict(row), **updates, "status": to}
    if op.write:
        won = transition_job(
            tx,
            row["id"],
            expect_status=row["status"],
            expect_attempt=row["attempt"],
            to_status=to,
            updates=updates,
        )
        if not won:  # unreachable inside BEGIN IMMEDIATE; kept as the compare-and-set's guard
            raise JobsctlError("internal", f"{row['id']} changed concurrently")
        if attempt is not None:
            assert attempt_updates is not None
            update_attempt_by_claim(tx, row["id"], attempt["claim_id"], attempt_updates)
            attempt = _attempt_row(tx, row)
        after = job_row(tx, row["id"])
        assert after is not None
        job = op.render(after)
        if hook is not None:
            write_post_event(
                tx,
                op.config,
                hook,
                job=job,
                previous_status=row["status"],
                attempt=attempt,
                now=op.now,
            )
    else:
        job = op.render(merged)
        if attempt is not None:  # the projection shows the attempt the real change would end
            job["claimed_at"] = attempt["claimed_at"]
            job["exit"] = None if attempt["exit"] is None else json.loads(attempt["exit"])
            job["leftovers"] = attempt["leftovers"]
            job["resolved_path"] = attempt["resolved_path"]
            job["force_requested_by"] = attempt["force_requested_by"]
    if op.write:
        record: dict[str, Any] = {
            "job_id": row["id"],
            "queue": row["queue"],
            "from": row["status"],
            "to": to,
            "reason": merged["reason"],
            "hold_reason": merged["hold_reason"],
            "attempt": row["attempt"],
            "actor": op.actor,
            "decision_ids": op.decision_ids,
        }
        if attempt_updates is not None:
            record["attempt_status"] = attempt_updates["status"]
            record["attempt_reason"] = attempt_updates.get("reason")
            record["retry_pending"] = False
        op.logs.append(("transition", record, "info"))
        if audit is not None:
            op.audits.append(
                {
                    "action": audit,
                    "actor": op.actor,
                    "target": row["id"],
                    "inputs": _audit_inputs(row),
                    "result": to,
                    "decision_id": op.decision_ids,
                }
            )
    return Applied(True, job)


def unchanged(op: Op, row: sqlite3.Row) -> Applied:
    return Applied(False, op.render(row))


# -- hold, release, cancel ---------------------------------------------------------------------


def hold(op: Op, row: sqlite3.Row) -> Applied:
    """A queued job becomes `held` (`manual`), keeping its place and `not_before`."""
    status = row["status"]
    if status in ("starting", "running"):
        raise refused("live", f"{row['id']} is {status}; jobsctl cannot pause a process")
    if status != "queued":
        return unchanged(op, row)  # held or terminal: no-op
    return _transition(op, row, to="held", updates={"hold_reason": "manual"}, hook="post-held")


def release(
    op: Op, row: sqlite3.Row, *, override: bool, require_human: Callable[[], None]
) -> Applied:
    """A held job returns to `queued` at its previous rank (its position key is untouched),
    with `not_before` cleared. `require_human` runs after the state checks, only when the
    override flag takes effect."""
    status = row["status"]
    if status == "queued":
        return unchanged(op, row)  # the flag takes no effect: no human check
    if status != "held":
        raise refused("not_held", f"{row['id']} is {status}, not held", details={"status": status})
    hold_reason = row["hold_reason"]
    if hold_reason == "exit_13" and not override:
        raise refused(
            "not_releasable",
            f"{row['id']} was held for exit 13; retry it to continue the work, or resolve it",
        )
    if hold_reason == "escalated" and not override:
        raise refused(
            "override_required",
            f"{row['id']} was escalated by a pre-start hook; release it with --override-escalation",
        )
    if override and hold_reason != "escalated":
        raise refused("not_escalated", f"{row['id']} is not an escalated hold")
    updates: dict[str, Any] = {
        "hold_reason": None,
        "not_before": None,
        "enqueued_at": op.now,
        "auto_retries": 0,
        "spawn_failures": 0,
    }
    audit = None
    if override:
        require_human()
        assert op.tx is not None
        attempt = _attempt_row(op.tx, row)
        if attempt is None:
            raise JobsctlError("internal", f"{row['id']} is escalated but has no attempt")
        updates["override_hooks"] = attempt["escalated_hooks"]
        audit = "release_override"
    # TODO(#25): the human check and the override are complete; the pre-start side that honours
    # `override_hooks` belongs to the shim.
    return _transition(op, row, to="queued", updates=updates, audit=audit)


def cancel_held_or_queued_or_starting(op: Op, row: sqlite3.Row) -> Applied:
    """`queued`, `held` and `starting` become `canceled` (`canceled`) at once. A `held` job
    with an attempt (`escalated`, `exit_13`) and a `starting` job end that attempt too."""
    status = row["status"]
    assert status in ("queued", "held", "starting")
    updates: dict[str, Any] = {
        "reason": "canceled",
        "hold_reason": None,
        "not_before": None,
        "ended_at": op.now,
        "override_hooks": "[]",
    }
    attempt_updates: dict[str, Any] | None = None
    if status == "starting":
        attempt_updates = {
            "status": "canceled",
            "reason": "canceled",
            "hold_reason": None,
            "ended_at": op.now,
        }
    elif status == "held" and row["hold_reason"] != "manual":
        attempt_updates = {"status": "canceled", "reason": "canceled", "hold_reason": None}
    return _transition(
        op,
        row,
        to="canceled",
        updates=updates,
        attempt_updates=attempt_updates,
        hook="post-cancel",
        audit="cancel_held" if status == "held" else None,
    )
