"""The pass (docs/interface.md, Pass; design.md D5, D27-D29, D33).

One :meth:`Runner.run_pass`:

1. takes the ``runner`` lease (a live holder: ``already_running``, nothing done);
2. hands the identity checks to ``reconcile`` (a hook point, ``TODO(#22)``) and reaps the shims
   this process spawned, by PID;
3. claims jobs in one ``BEGIN IMMEDIATE`` transaction (counts read and claims written together,
   so the caps hold under any number of concurrent passes), recording the ``pass_requested``
   counter it started from;
4. spawns one shim per claim (after the claim committed) and records its identity in a follow-up
   transaction;
5. requests delivery, 6. runs retention, 7. checkpoints the WAL and, in one transaction, re-reads
   the counter and releases the lease; a changed counter goes back to step 3.

Every step renews the lease first. Nothing is signaled here, and only processes this runner
spawned are polled (``Popen.poll`` waits for that exact PID).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsctl import gateway
from jobsctl.claims import (
    Claim,
    LogRecord,
    claim_jobs,
    fail_spawn,
    requeue_orphaned,
)
from jobsctl.config import Config
from jobsctl.env_file import EnvFile, load_env_file
from jobsctl.env_logging import log_env_file_state
from jobsctl.errors import JobsctlError
from jobsctl.leases import (
    LEASE_COLUMNS,
    RENEW_SECONDS,
    Holder,
    expiry,
    holder_of,
    identity_text,
    liveness_probe,
    own_identity,
)
from jobsctl.log_writer import RecordWriter
from jobsctl.proc_host import Host, default_host
from jobsctl.retention import run_retention
from jobsctl.spawn import spawn_internal
from jobsctl.store_db import Store
from jobsctl.store_ops import (
    counter_get,
    lease_release,
    lease_renew,
    take_lease,
    update_attempt_by_claim,
)
from jobsctl.timestamps import format_timestamp
from jobsctl.triggers import PASS_REQUESTED, request_delivery, spawn_pass

Reconcile = Callable[["Runner", "PassResult"], None]


def no_reconcile(runner: Runner, result: PassResult) -> None:
    """The identity-check hook point of pass step 2."""
    # TODO(#22): reconcile's rules (boot change, claim_orphaned for dead claimers, shim_dead,
    # job_gone, shim_lost, force_resumed, redelivery) run here, under the lease.


@dataclass
class PassResult:
    already_running: bool = False
    holder: Holder | None = None
    started: list[str] = field(default_factory=list)
    reconciled: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    changed: bool = False

    def as_json(self) -> dict[str, Any]:
        holder = None
        if self.already_running and self.holder is not None and self.holder.pid is not None:
            holder = {"pid": self.holder.pid, "since": self.holder.since}
        return {
            "already_running": self.already_running,
            "holder": holder,
            "started": list(self.started),
            "reconciled": list(self.reconciled),
            "warnings": list(self.warnings),
        }


@dataclass
class Child:
    """A process this runner spawned. `claim_id` is set for a shim."""

    popen: subprocess.Popen[bytes]
    kind: str
    job_id: str | None = None
    claim_id: str | None = None


class LeaseLost(JobsctlError):
    def __init__(self) -> None:
        super().__init__("internal", "the runner lease was taken over; the pass stops")


class Runner:
    def __init__(
        self,
        store: Store,
        config: Config,
        config_path: Path | None,
        *,
        host: Host | None = None,
        clock: Callable[[], dt.datetime] = gateway.utc_now,
        python: str | None = None,
        reconcile: Reconcile = no_reconcile,
    ) -> None:
        self.store = store
        self.config = config
        self.config_path = config_path
        self.host = host or default_host()
        self.clock = clock
        self.python = python
        self.reconcile = reconcile
        self.writer = RecordWriter(store, clock=clock)
        self.children: list[Child] = []
        self.lease_taken = False  # for a triggered pass: busy "before the lease" re-spawns
        self._holder = ""
        self._env_file: EnvFile | None = None
        self._last_renew = 0.0
        self._seen: int | None = None
        self.probe: Callable[[str], None] | None = None  # tests: called at the start of each step

    # -- the pass ------------------------------------------------------------------------------

    def run_pass(self, *, max_starts: int | None = None) -> PassResult:
        self._env_file = None
        self.lease_taken = False
        result = PassResult()
        now = self.clock()
        self._holder = own_identity(self.host)
        probe = liveness_probe(self.host)
        # The baseline of a pass that stops before its first claim: read before the take, so a
        # request that arrives once this process is the live holder is never missed.
        self._seen = self._counter_now()
        for _ in range(2):
            if take_lease(
                self.store,
                "runner",
                self._holder,
                now=format_timestamp(now),
                expires_at=expiry(now),
                is_alive=probe,
            ):
                break
            if self._already_running(result):
                return result
            # the holder vanished between the failed take and the read: take the lease once more
        else:
            return result
        self.lease_taken = True
        self._last_renew = gateway.monotonic()
        released = False
        try:
            released = self._locked_pass(result, max_starts)
        finally:
            if not released:
                self._abandon()
        return result

    def _counter_now(self) -> int:
        row = self.store.query_one("SELECT value FROM counters WHERE name = ?", [PASS_REQUESTED])
        return 0 if row is None else int(row["value"])

    def _already_running(self, result: PassResult) -> bool:
        """Report a live holder in `result`; `False` when there is none (released or dead between
        the failed take and this read), so the caller takes the lease again."""
        now = self.clock()
        row = self.store.query_one(LEASE_COLUMNS, ["runner"])
        holder = holder_of(row, self.host, now)
        if holder is None:
            return False
        result.already_running = True
        result.holder = holder
        if holder.stuck(now):
            self._safely(
                lambda: self.writer.warn_once(
                    "runner_stuck",
                    holder.identity,
                    "runner_stuck",
                    fields={
                        "message": "the runner lease holder is alive but has not renewed it",
                        "details": {"pid": holder.pid, "since": holder.since},
                    },
                )
            )
        return True

    def _locked_pass(self, result: PassResult, max_starts: int | None) -> bool:
        self._step("reconcile")
        self.reap(result)
        self.reconcile(self, result)
        total = 0
        while True:
            self._step("claim")
            seen, claims = self._claim(result, None if max_starts is None else max_starts - total)
            self._seen = seen
            total += len(claims)
            for claim in claims:
                self._step("spawn")
                self._spawn_one(claim, result)
            self._step("delivery")
            self._guarded("delivery", result, self._request_delivery)
            self._step("retention")
            self._guarded("retention", result, self._retention)
            self._checkpoint()
            self._step("release")
            if self._release_if_unchanged(seen):
                break
        if result.started or result.reconciled or result.changed:
            self._safely(
                lambda: self.writer.log(
                    "pass",
                    fields={
                        "details": {
                            "started": len(result.started),
                            "reconciled": len(result.reconciled),
                        }
                    },
                )
            )
        return True

    # -- steps ---------------------------------------------------------------------------------

    def _step(self, name: str) -> None:
        """Start a step: call the test probe, then renew the lease if a third of the renewal
        interval has passed (one cheap compare-and-set)."""
        if self.probe is not None:
            self.probe(name)
        if gateway.monotonic() - self._last_renew < RENEW_SECONDS / 3:
            return
        now = self.clock()
        with self.store.write() as tx:
            if not lease_renew(tx, "runner", self._holder, expiry(now)):
                raise LeaseLost
        self._last_renew = gateway.monotonic()

    def _env_unavailable(self) -> bool:
        if self._env_file is None:
            self._env_file = load_env_file(self.config.env_file)
        return self._env_file.status == "unavailable"

    def _claim(self, result: PassResult, limit: int | None) -> tuple[int, list[Claim]]:
        if limit is not None and limit <= 0:
            with self.store.write() as tx:
                return counter_get(tx, PASS_REQUESTED), []
        now = format_timestamp(self.clock())
        with self.store.write() as tx:
            seen = counter_get(tx, PASS_REQUESTED)
            claims, records = claim_jobs(
                tx,
                self.config,
                now=now,
                claimed_by=self._holder,
                env_file_unavailable=self._env_unavailable,
                limit=limit,
            )
            tx.after_commit(lambda: self._emit(records))
        if self._env_file is not None:
            self._safely(lambda: self._log_env_state(result))
        return seen, claims

    def _log_env_state(self, result: PassResult) -> None:
        env_file = self._env_file
        assert env_file is not None
        log_env_file_state(self.writer, self.config.env_file, env_file)
        unsafe = next((w for w in env_file.warnings if w.code == "env_file_unsafe"), None)
        if unsafe is not None and not any(w["code"] == "env_file_unsafe" for w in result.warnings):
            result.warnings.append(
                {
                    "code": "env_file_unsafe",
                    "message": "env_file is not trusted and is treated as unavailable",
                }
            )

    def _spawn_one(self, claim: Claim, result: PassResult) -> None:
        """One claim's spawn; its failure (a busy database, a database error) returns that job to
        `queued` when it can and never stops the remaining claims."""
        try:
            self._spawn_shim(claim, result)
        except LeaseLost:
            raise
        except (JobsctlError, sqlite3.Error) as exc:
            try:
                self._spawn_failed(
                    claim, result, f"the shim could not be started ({type(exc).__name__})"
                )
            except LeaseLost:
                raise
            except (JobsctlError, sqlite3.Error):
                pass  # the claim stays `starting`; reconcile (or the shim itself) deals with it

    def _spawn_shim(self, claim: Claim, result: PassResult) -> None:
        """Step 4 for one claim. The claim is committed; a failure returns the job to `queued`."""
        try:
            popen = spawn_internal(
                self.store.paths.root,
                self.config,
                self.config_path,
                "_shim",
                [claim.job_id, claim.claim_id],
                python=self.python,
            )
        except OSError as exc:
            self._spawn_failed(claim, result, f"the shim could not be spawned ({exc.strerror})")
            return
        self.children.append(Child(popen, "shim", claim.job_id, claim.claim_id))
        try:
            text = identity_text(self.host, popen.pid)
        except Exception as exc:  # the shim runs, but its identity cannot be recorded
            self._spawn_failed(
                claim, result, f"the shim identity cannot be read ({type(exc).__name__})"
            )
            return
        with self.store.write() as tx:
            recorded = update_attempt_by_claim(
                tx,
                claim.job_id,
                claim.claim_id,
                {"shim_identity": text},
                job_status="starting",
            )
        if recorded:
            result.started.append(claim.job_id)
            self._safely(
                lambda: self.writer.log(
                    "shim_spawned",
                    fields={
                        "job_id": claim.job_id,
                        "queue": claim.queue,
                        "attempt": claim.attempt,
                        "details": {"pid": popen.pid},
                    },
                )
            )

    def _spawn_failed(self, claim: Claim, result: PassResult, message: str) -> None:
        now = format_timestamp(self.clock())
        with self.store.write() as tx:
            outcome, records = fail_spawn(
                tx, self.config, claim.job_id, claim.claim_id, now=now, message=message
            )
            tx.after_commit(lambda: self._emit(records))
        if outcome is not None:
            result.changed = True
            result.warnings.append(
                {"code": "shim_spawn_failed", "message": message, "job_id": claim.job_id}
            )

    def reap(self, result: PassResult) -> None:
        """Poll the processes this runner spawned, by their handles (never "any child"). A shim
        that exited without confirming its identity makes its attempt `claim_orphaned`."""
        still: list[Child] = []
        for child in self.children:
            if child.popen.poll() is None:
                still.append(child)
                continue
            if child.kind != "shim":
                continue
            assert child.job_id is not None and child.claim_id is not None
            now = format_timestamp(self.clock())
            with self.store.write() as tx:
                requeued, records = requeue_orphaned(tx, child.job_id, child.claim_id, now=now)
                tx.after_commit(lambda records=records: self._emit(records))
            if requeued:
                result.reconciled.append(
                    {"job_id": child.job_id, "finding": "claim_orphaned", "action": "requeued"}
                )
        self.children = still

    def _request_delivery(self, result: PassResult) -> None:
        child = request_delivery(
            self.store, self.config, self.config_path, host=self.host, now=self.clock()
        )
        if child is not None:
            self.children.append(Child(child, "deliver"))

    def _retention(self, result: PassResult) -> None:
        outcome = run_retention(
            self.store,
            self.writer,
            retention=self.config.retention,
            configured_queues=self.config.queues,
            now=self.clock(),
        )
        if outcome.pruned or outcome.orphans_removed or outcome.queues_forgotten:
            result.changed = True
        if outcome.audit_error is not None:
            self._fail_step("retention", type(outcome.audit_error).__name__, "audit append failed")

    def _guarded(self, name: str, result: PassResult, step: Callable[[PassResult], None]) -> None:
        """Run a step whose failure must not skip the checkpoint and the lease release."""
        try:
            step(result)
        except LeaseLost:
            raise
        except (JobsctlError, sqlite3.Error, OSError) as exc:
            self._fail_step(name, type(exc).__name__, "the step failed")

    def _fail_step(self, name: str, error: str, message: str) -> None:
        self._safely(
            lambda: self.writer.log(
                "pass",
                level="warn",
                fields={
                    "message": f"pass step {name} failed: {message}",
                    "details": {"step": name, "error": error},
                },
            )
        )

    def _checkpoint(self) -> None:
        try:
            self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:
            pass  # a busy checkpoint changes nothing; the next pass tries again

    def _release_if_unchanged(self, seen: int) -> bool:
        """Re-read the counter and release the lease in one `BEGIN IMMEDIATE` transaction."""
        with self.store.write() as tx:
            if counter_get(tx, PASS_REQUESTED) != seen:
                return False
            if not lease_release(tx, "runner", self._holder):
                raise LeaseLost
        return True

    def _abandon(self) -> None:
        """A pass that stops early (an error) still releases its lease; a request that arrived
        meanwhile is passed on to a new pass rather than lost."""
        try:
            with self.store.write() as tx:
                released = lease_release(tx, "runner", self._holder)
                changed = self._seen is not None and counter_get(tx, PASS_REQUESTED) != self._seen
            if released and changed and self.config.auto_run:
                self.children.append(Child(self.respawn_pass(0), "pass"))
        except Exception as exc:  # the lease expires; cron or the next trigger starts the work
            self._safely_warn(f"could not release the runner lease ({type(exc).__name__})")

    # -- helpers -------------------------------------------------------------------------------

    def _emit(self, records: list[LogRecord]) -> None:
        for record in records:
            self._safely(
                lambda record=record: self.writer.log(
                    record.event, level=record.level, fields=record.fields
                )
            )

    def _safely_warn(self, message: str) -> None:
        sys.stderr.write(f"jobsctl: warning: {message}\n")
        sys.stderr.flush()

    def _safely(self, action: Callable[[], object]) -> None:
        """Log without failing the pass: the work is committed, a log is best-effort."""
        try:
            action()
        except Exception as exc:
            sys.stderr.write(
                f"jobsctl: warning: a log record was not written ({type(exc).__name__})\n"
            )
            sys.stderr.flush()

    def respawn_pass(self, retried: int) -> subprocess.Popen[bytes]:
        return spawn_pass(self.store.paths.root, self.config, self.config_path, retried=retried)
