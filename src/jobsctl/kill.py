"""`_kill`: the forced cancel of a job whose shim is gone (docs/interface.md, Shim loss and Signal
sequence; design.md D38).

`_kill` is not the job's parent. It takes the `job:<id>` lease and proceeds only if the job is
still `running` with a committed force request and its shim is proven dead; otherwise it exits
(the pending events are delivered here once `_deliver` exists). The whole of it:

1. take the lease (a live holder: a shim, another `_kill`, a delivering command, so exit);
2. re-read the attempt under the lease; a result file the dead shim left for it is the exit
   record; nothing is signaled when the job leader is gone;
3. otherwise run the signal sequence (`shim_signals`) against the recorded leader, with no
   unchecked fallback (``own_child`` is false: the leader is not ours, its PID could be reused)
   and a zombie leader counted as gone. A group signal is only ever sent to the group whose
   leader's recorded identity (boot id, PID, start time) still holds; once the leader is gone
   nothing more is signaled, and a process that left the group is never signaled;
4. count the leftovers and finalize the attempt `canceled` (`forced`), exit kind `unknown`, with
   its `post-cancel` event, in one compare-and-set on the attempt's `claim_id`.

A forced end is never retried automatically (`auto_retry.plan_requeue` applies to `failed` ends
only), so there is no `lost` outcome here. A `_kill` that is killed, or gives up, finalizes
nothing: a repeated `cancel --force` or reconcile (`force_resumed`) starts another one.

Group signals use `killpg` after the identity re-check; the 6.9+ pidfd path is TODO(#35).
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jobsctl import gateway
from jobsctl.claims import LogRecord
from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.job_view import effective_settings
from jobsctl.leases import RENEW_SECONDS, expiry, liveness_probe, own_identity, parse_identity
from jobsctl.leftovers import Leftovers, count_leftovers
from jobsctl.log_writer import RecordWriter
from jobsctl.output_capture import TailSnapshot
from jobsctl.proc_host import Host, default_host
from jobsctl.proc_identity import Identity, Liveness, check_identity
from jobsctl.result_file import ResultFileError, ResultRecord, delete_result, read_result
from jobsctl.shim import StopSignal
from jobsctl.shim_exit import ExitRecord, forced_outcome
from jobsctl.shim_signals import SignalSequence
from jobsctl.shim_store import finish_running
from jobsctl.store_db import Store
from jobsctl.store_ops import lease_release, lease_renew, take_lease
from jobsctl.timestamps import format_timestamp
from jobsctl.triggers import request_pass

POLL_SECONDS = 0.1
CHECK_SECONDS = 1.0  # the attempt and the leader's identity are looked at this often
UNKNOWN_LIMIT_SECONDS = 60.0  # a leader whose identity stays unreadable this long: give up
STEP_FAILURE_LIMIT = 30  # consecutive failed sequence steps before giving up
FINAL_COMMIT_SECONDS = 600.0
BACKOFF_START = 0.2
BACKOFF_MAX = 10.0
_READ_ATTEMPT = (
    "SELECT j.status, j.queue, j.attempt, j.overrides, a.claim_id, a.shim_identity, "
    "a.job_identity, a.pgid, a.force_requested_by, a.effective "
    "FROM jobs j JOIN attempts a ON a.job_id = j.id AND a.n = j.attempt WHERE j.id = ?"
)


class AlreadyHandled(Exception):
    """Another process finalized or took over the attempt: nothing is left to do (exit 0)."""


class GaveUp(Exception):
    """`_kill` stops without finalizing; a repeated request or reconcile starts another."""


@dataclass(frozen=True)
class Target:
    """The attempt `_kill` acts on, as read under the lease."""

    queue: str
    attempt: int
    claim_id: str
    shim: Identity | None
    leader: Identity | None
    pgid: int | None
    kill_grace: float


def prepare_process(stop: StopSignal) -> None:
    """SIGHUP and SIGINT are ignored (by a no-op handler); SIGTERM stops `_kill` between steps,
    without finalizing and with the lease released: another `_kill` replaces it."""
    signal.signal(signal.SIGHUP, lambda signum, frame: None)
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())


class Killer:
    def __init__(
        self,
        store: Store,
        config: Config,
        config_path: Any,
        job_id: str,
        *,
        host: Host | None = None,
        clock: Callable[[], Any] = gateway.utc_now,
        stop: StopSignal | None = None,
        monotonic: Callable[[], float] = gateway.monotonic,
        sleep: Callable[[float], None] = gateway.sleep,
    ) -> None:
        self.store = store
        self.config = config
        self.config_path = config_path
        self.job_id = job_id
        self.host = host or default_host()
        self.clock = clock
        self.stop = stop or StopSignal()
        self._mono = monotonic
        self._sleep = sleep
        self.writer = RecordWriter(store, clock=clock)
        self._lease = f"job:{job_id}"
        self._ident = ""
        self._held = False
        self._last_renew = 0.0

    # -- helpers -------------------------------------------------------------------------------

    def _now(self) -> str:
        return format_timestamp(self.clock())

    def _diag(self, message: str) -> None:
        sys.stderr.write(f"_kill {self.job_id}: {message}\n")
        sys.stderr.flush()

    def _emit(self, records: list[LogRecord] | tuple[LogRecord, ...]) -> None:
        for record in records:
            self._best_effort(
                lambda record=record: self.writer.log(
                    record.event, level=record.level, fields=record.fields
                )
            )

    def _best_effort(self, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:  # a log that is not written never stops the kill
            self._diag(f"a log record was not written ({type(exc).__name__})")

    # -- the lease -----------------------------------------------------------------------------

    def _take_lease(self) -> bool:
        """One attempt, never waiting: a live holder (the shim, another `_kill`, a delivering
        command) means this one has nothing to do."""
        now = self.clock()
        self._held = take_lease(
            self.store,
            self._lease,
            self._ident,
            now=format_timestamp(now),
            expires_at=expiry(now),
            is_alive=liveness_probe(self.host),
        )
        self._last_renew = self._mono()
        return self._held

    def _tick(self) -> None:
        """Renew the lease every 15 seconds. Losing it means another process took it over: stop."""
        if self._mono() - self._last_renew < RENEW_SECONDS:
            return
        try:
            with self.store.write() as tx:
                renewed = lease_renew(tx, self._lease, self._ident, expiry(self.clock()))
        except (JobsctlError, sqlite3.Error) as exc:
            self._diag(f"the job lease was not renewed ({type(exc).__name__})")
            return  # a busy database is tried again at the next tick
        self._last_renew = self._mono()
        if not renewed:
            self._held = False
            raise GaveUp("the job lease was taken over by another holder")

    def _release_lease(self) -> None:
        if not self._held:
            return
        try:
            with self.store.write() as tx:
                lease_release(tx, self._lease, self._ident)
        except (JobsctlError, sqlite3.Error) as exc:
            self._diag(f"the job lease was not released ({type(exc).__name__}); it will expire")
        self._held = False

    # -- reading the attempt -------------------------------------------------------------------

    def _read(self) -> Target | None:
        """The attempt when it is `running` with a committed force request; else `None`."""
        row = self.store.query_one(_READ_ATTEMPT, [self.job_id])
        if row is None or row["status"] != "running" or row["force_requested_by"] is None:
            return None
        return Target(
            queue=str(row["queue"]),
            attempt=int(row["attempt"]),
            claim_id=str(row["claim_id"]),
            shim=_identity(row["shim_identity"]),
            leader=_identity(row["job_identity"]),
            pgid=None if row["pgid"] is None else int(row["pgid"]),
            kill_grace=self._kill_grace(row),
        )

    def _kill_grace(self, row: Any) -> float:
        """The shim's snapshot (`effective`); absent or malformed, the configured value."""
        try:
            grace = json.loads(row["effective"] or "{}").get("kill_grace")
        except (ValueError, AttributeError):
            grace = None
        if isinstance(grace, (int, float)) and not isinstance(grace, bool) and grace >= 0:
            return float(grace)
        configured = effective_settings(
            self.config, row["queue"], json.loads(row["overrides"]), None
        )
        return float(configured["kill_grace"])

    def _shim_is_gone(self, target: Target) -> bool:
        """Only a shim proven dead (also: another boot) lets `_kill` act. Alive, unknown and
        unrecorded identities all mean "not proven gone"."""
        if target.shim is None:
            self._diag("the attempt records no usable shim identity; nothing was signaled")
            return False
        state = check_identity(self.host, target.shim).state
        if state is not Liveness.DEAD:
            self._diag(f"the shim is not proven gone ({state.value}); nothing was signaled")
            return False
        return True

    # -- the run -------------------------------------------------------------------------------

    def run(self) -> int:
        self._ident = own_identity(self.host)
        if not self._take_lease():
            self._diag("another live process holds the job lease; nothing to do")
            return 0
        try:
            return self._with_lease()
        except AlreadyHandled as why:
            self._diag(f"{why}; nothing to do")
            return 0
        except GaveUp as why:
            self._diag(f"{why}; nothing finalized")
            return 1
        finally:
            self._release_lease()

    def _with_lease(self) -> int:
        target = self._read()
        if target is None or not self._shim_is_gone(target):
            # TODO(#24): deliver this job's undelivered events (under the lease), then exit.
            return 0
        saved = self._from_result_file(target)
        if saved is not None:
            record, count, tail = saved
            self._finish(target, record, count, None, tail)
            self._drop_result_file(target)
            return 0
        record, count, leftovers = ExitRecord("unknown"), None, None
        if target.leader is not None and target.pgid == target.leader.pid:
            state = check_identity(self.host, target.leader).state
            if state is Liveness.UNKNOWN:
                raise GaveUp("the job leader's identity is unreadable; nothing was signaled")
            if state is Liveness.ALIVE:
                self._run_sequence(target, target.leader)
                leftovers = self._leftovers(target.leader)
                count = leftovers.recorded
            # DEAD: gone, reused or another boot. Nothing is signaled; the group cannot be probed.
        elif target.leader is not None:
            self._diag(f"the recorded group {target.pgid} is not the leader's; nothing signaled")
        self._finish(target, record, count, leftovers, None)
        return 0

    # -- the signal sequence -------------------------------------------------------------------

    def _run_sequence(self, target: Target, leader: Identity) -> None:
        seq = SignalSequence(
            self.host,
            leader,
            kill_grace=target.kill_grace,
            leader_exited=lambda: self._leader_state(leader) is Liveness.DEAD,
            own_child=lambda: False,  # not our child: no signal without the identity check
            now=self._mono,
            diag=self._diag,
            allow_zombie=False,
        )
        failures = 0
        last_check = self._mono()
        unknown_since: float | None = None
        started = False
        self._diag("running the signal sequence")
        while True:
            if self.stop.is_set:
                raise GaveUp("SIGTERM received")
            self._tick()
            now = self._mono()
            if now - last_check >= CHECK_SECONDS:
                last_check = now
                self._recheck(target)
                if self._leader_state(leader) is Liveness.UNKNOWN:
                    unknown_since = now if unknown_since is None else unknown_since
                    if now - unknown_since >= UNKNOWN_LIMIT_SECONDS:
                        raise GaveUp("the job leader's identity stayed unreadable")
                else:
                    unknown_since = None
            if self._leader_state(leader) is Liveness.DEAD:
                # No signal is sent once the leader is gone: its PID (the group id) is no longer
                # reserved, so a group signal could reach a stranger. Unlike the shim's
                # step-2-at-once rule, the sequence ends here: members that remain are counted
                # as leftovers, not signaled.
                # TODO(#35): signal them via PIDFD_SIGNAL_PROCESS_GROUP (Linux 6.9+).
                if started:
                    return
            try:
                if not started:
                    seq.start("forced")
                    started = True
                seq.advance()
                failures = 0
            except Exception as exc:
                failures += 1
                if failures == 1:
                    self._diag(f"a signal sequence step failed ({type(exc).__name__}); retrying")
                if failures >= STEP_FAILURE_LIMIT:
                    raise GaveUp("the signal sequence failed too often") from exc
            if seq.done:
                return
            self._sleep(POLL_SECONDS)

    def _leader_state(self, leader: Identity) -> Liveness:
        return check_identity(self.host, leader).state

    def _recheck(self, target: Target) -> None:
        """The attempt is still this one, `running`, and still has its force request (a reconcile
        or a second `_kill` that finalized it meanwhile ends this one)."""
        try:
            now = self._read()
        except (JobsctlError, sqlite3.Error) as exc:
            self._diag(f"the attempt was not re-read ({type(exc).__name__})")
            return
        if now is None or now.claim_id != target.claim_id:
            raise AlreadyHandled("the attempt is no longer running with a force request")

    def _leftovers(self, leader: Identity) -> Leftovers:
        """Counted, never signaled. The leader was verified when the sequence began."""
        return count_leftovers(
            self.host,
            leader_pid=leader.pid,
            pgid=leader.pid,
            own_pid=os.getpid(),
            reap=lambda pid: False,  # `_kill` reaps nothing
        )

    # -- the result file and the final commit --------------------------------------------------

    def _from_result_file(
        self, target: Target
    ) -> tuple[ExitRecord, int | None, TailSnapshot] | None:
        """The shim's own record of this attempt, when it got that far before it died: the real
        exit record, leftovers count and tail. The job is gone then; nothing is signaled."""
        try:
            found: ResultRecord | None = read_result(
                self.store.paths.shims, self.job_id, target.attempt
            )
        except ResultFileError as exc:
            self._diag(f"the result file is not usable ({exc}); finalizing without it")
            return None
        if found is None or found.claim_id != target.claim_id:
            return None
        tail = TailSnapshot(found.tail.text, found.tail.bytes_seen, found.tail.truncated)
        return found.exit, found.leftovers, tail

    def _drop_result_file(self, target: Target) -> None:
        try:
            delete_result(self.store.paths.shims, self.job_id, target.attempt)
        except ResultFileError as exc:
            self._diag(f"the result file was not removed ({exc})")

    def _finish(
        self,
        target: Target,
        record: ExitRecord,
        count: int | None,
        leftovers: Leftovers | None,
        tail: TailSnapshot | None,
    ) -> None:
        result = self._retrying(
            lambda: finish_running(
                self.store,
                self.config,
                self.job_id,
                target.claim_id,
                outcome=forced_outcome(),
                exit_record=record,
                leftovers=count,
                tail=tail,
                clock=self._now,
            )
        )
        if not result.committed:
            self._diag("the attempt was already finalized by someone else; nothing written")
            return
        self._emit(result.records)
        if leftovers is not None and leftovers.count:
            self._best_effort(
                lambda: self.writer.log(
                    "leftovers",
                    level="warn",
                    fields={
                        "job_id": self.job_id,
                        "queue": target.queue,
                        "attempt": target.attempt,
                        "message": "processes outlived the job's leader",
                        "details": {"count": leftovers.count, "complete": leftovers.complete},
                    },
                )
            )
        try:
            request_pass(self.store, self.config, self.config_path, host=self.host)
        except (JobsctlError, sqlite3.Error, OSError) as exc:
            self._diag(f"could not request a pass ({type(exc).__name__})")
        # TODO(#24): deliver this job's undelivered events, the post-cancel hook included.

    def _retrying(self, action: Callable[[], Any]) -> Any:
        """The final transaction is retried with backoff for up to 10 minutes (the lease is kept
        renewed meanwhile); the last error is raised."""
        deadline = self._mono() + FINAL_COMMIT_SECONDS
        delay = BACKOFF_START
        while True:
            try:
                return action()
            except Exception as exc:  # a busy database, an I/O failure: tried until the deadline
                if self.stop.is_set:
                    raise GaveUp("SIGTERM received") from exc
                if self._mono() + delay >= deadline:
                    raise
                self._tick()
                self._sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)


def _identity(text: object) -> Identity | None:
    return None if not text else parse_identity(str(text))
