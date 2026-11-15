"""The shim: supervises one attempt (docs/interface.md, Shim and Start sequence; design.md D10).

:meth:`Shim.run` is the start sequence, step by step:

1. (:func:`prepare_process`, by the command) signals, child subreaper, non-dumpable;
2. confirm the identity the pass recorded, take the `job:<id>` lease, record `effective`;
3. the `pre-start` hooks (seam, TODO(#25), TODO(#23));
4. cwd, 5. job environment, 6. `argv[0]`;
7. the atomic start: re-check, spawn in a new process group and set `running` in one
   `BEGIN IMMEDIATE` transaction;
8. the output pump, 9. the exit, the leftovers, the result file and the final commit;
10. delivery of the job's events (seam, TODO(#24)).

Every write is a compare-and-set on the attempt's `claim_id` (`shim_store`). The shim signals
nothing in this issue except the group it just created when the start cannot be recorded: the
signal sequence, the timeout and the force-request reaction are the seams marked `TODO(#19)`.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobsctl import gateway, gateway_proc
from jobsctl.argv_resolve import resolve_argv0
from jobsctl.claims import LogRecord
from jobsctl.config import Config
from jobsctl.env_canonical import build_canonical_env
from jobsctl.env_file import load_env_file
from jobsctl.env_job import MissingEnvError, build_job_env
from jobsctl.env_logging import log_env_file_state, log_path_state
from jobsctl.env_mask import masking_values
from jobsctl.errors import JobsctlError
from jobsctl.leases import (
    RENEW_SECONDS,
    expiry,
    identity_text,
    liveness_probe,
    own_identity,
    parse_identity,
)
from jobsctl.leftovers import Leftovers, count_leftovers
from jobsctl.log_writer import RecordWriter
from jobsctl.output_capture import OutputCapture, TailSnapshot
from jobsctl.proc_host import Host, default_host
from jobsctl.proc_identity import Liveness, check_identity
from jobsctl.proc_linux import reap_held_leader, set_child_subreaper, set_undumpable
from jobsctl.result_file import (
    ResultFileError,
    ResultRecord,
    ResultTail,
    delete_result,
    write_result,
)
from jobsctl.shim_exit import SPAWN_FAILED_EXIT, ExitRecord, exit_record, natural_outcome
from jobsctl.shim_pump import DiscardCapture, Pump
from jobsctl.shim_store import (
    ClaimView,
    begin_running,
    confirm,
    effective_snapshot,
    fail_starting,
    finish_running,
    flush_tail,
    read_claim,
    read_claim_in,
    record_effective,
)
from jobsctl.store_db import Store
from jobsctl.store_ops import IsAlive, lease_release, lease_renew, take_lease
from jobsctl.timestamps import format_timestamp
from jobsctl.triggers import request_pass

POLL_SECONDS = 0.1
FINAL_COMMIT_SECONDS = 600.0  # the final transaction is retried with backoff for up to 10 minutes
BACKOFF_START = 0.2
BACKOFF_MAX = 10.0
PRE_START_BUSY_SECONDS = 120.0
CONFIRM_UNKNOWN_SECONDS = 60.0  # the claiming pass's identity may be inconclusive this long


def _noop_handler(signum: int, frame: object) -> None:
    return None


class StopSignal:
    """SIGTERM, recorded by the handler and looked at by the loops."""

    def __init__(self) -> None:
        self.is_set = False

    def set(self) -> None:
        self.is_set = True


def prepare_process(stop: StopSignal) -> None:
    """Start sequence step 1. SIGHUP and SIGINT are *handled* by a function that does nothing
    rather than set to `SIG_IGN`: the shim ignores them either way, but an ignored signal stays
    ignored across `exec`, so the job would inherit it (`gateway.spawn_job`). SIGPIPE is ignored
    (Python's default) and restored to its default in the job by `Popen`. SIGTERM is recorded and
    acted on by the loops."""
    signal.signal(signal.SIGHUP, _noop_handler)
    signal.signal(signal.SIGINT, _noop_handler)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    set_child_subreaper(True)
    set_undumpable()


@dataclass
class _StartState:
    """What one attempt of the start transaction spawned (nothing is retried after a spawn)."""

    popen: Any = None


@dataclass
class _Job:
    """A started job: the unreaped leader, its pipe and what the shim keeps about it."""

    popen: Any  # subprocess.Popen[bytes]
    pid: int
    capture: OutputCapture


class Shim:
    def __init__(
        self,
        store: Store,
        config: Config,
        config_path: Path | None,
        job_id: str,
        claim_id: str,
        *,
        config_error: JobsctlError | None = None,
        host: Host | None = None,
        clock: Callable[[], Any] = gateway.utc_now,
        environ: Mapping[str, str] | None = None,
        stop: StopSignal | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.config_path = config_path
        self.job_id = job_id
        self.claim_id = claim_id
        self.config_error = config_error
        self.host = host or default_host()
        self.clock = clock
        self.environ = os.environ if environ is None else environ
        self.writer = RecordWriter(store, clock=clock)
        self.stop = stop or StopSignal()
        self._ident = ""
        self._lease = f"job:{job_id}"
        self._lease_held = False
        self._last_renew = 0.0
        self._term_noted = False

    # -- helpers -------------------------------------------------------------------------------

    @property
    def terminated(self) -> bool:
        return self.stop.is_set

    def request_stop(self) -> None:
        """SIGTERM: recorded here, acted on at the loops' next look."""
        self.stop.set()

    def _now(self) -> str:
        return format_timestamp(self.clock())

    def _diag(self, message: str) -> None:
        sys.stderr.write(f"_shim {self.job_id}: {message}\n")
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
        except Exception as exc:  # a log that is not written never stops the supervision
            self._diag(f"a log record was not written ({type(exc).__name__})")

    def _log(
        self, event: str, view: ClaimView, *, level: str = "info", message: str, **details: Any
    ) -> None:
        self._best_effort(
            lambda: self.writer.log(
                event,
                level=level,
                fields={
                    "job_id": self.job_id,
                    "queue": view.queue,
                    "attempt": view.attempt,
                    "message": message,
                    "details": details,
                },
            )
        )

    def _retrying(self, action: Callable[[], Any], seconds: float) -> Any:
        """Run a database action, retrying with backoff on any error (a busy database, an I/O
        failure, anything else), for at most `seconds`; the last error is raised."""
        deadline = gateway.monotonic() + seconds
        delay = BACKOFF_START
        while True:
            try:
                return action()
            except Exception:  # any error: the final commit is retried until the deadline
                if gateway.monotonic() + delay >= deadline:
                    raise
                self._tick()
                gateway.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)

    # -- the job lease -------------------------------------------------------------------------

    def _take_lease(self) -> bool:
        """Hold `job:<id>` for the shim's whole life. While a live process holds it the shim
        waits, without a deadline, and writes nothing; `False` if SIGTERM arrives first or the job
        stops being `starting` under this claim while it waits (canceled, say)."""
        probe = liveness_probe(self.host)
        while not self.terminated:
            if self._try_lease(probe):
                self._lease_held = True
                self._last_renew = gateway.monotonic()
                return True
            try:
                gone = self._still_starting() is None
            except (JobsctlError, sqlite3.Error):
                gone = False  # a failed read is not a verdict: keep waiting, look again
            if gone:
                self._diag("the job is no longer starting under this claim; not waiting longer")
                return False
            gateway.sleep(POLL_SECONDS * 2)
        return False

    def _try_lease(self, probe: IsAlive) -> bool:
        def attempt() -> bool:
            now = self.clock()
            return take_lease(
                self.store,
                self._lease,
                self._ident,
                now=format_timestamp(now),
                expires_at=expiry(now),
                is_alive=probe,
            )

        return bool(self._retrying(attempt, PRE_START_BUSY_SECONDS))

    def _tick(self) -> None:
        """Renew the lease every 15 seconds (best effort: a busy database is tried again)."""
        if not self._lease_held or gateway.monotonic() - self._last_renew < RENEW_SECONDS:
            return
        try:
            with self.store.write() as tx:
                renewed = lease_renew(tx, self._lease, self._ident, expiry(self.clock()))
            self._last_renew = gateway.monotonic()
            if not renewed:
                self._lease_held = False
                self._diag("the job lease was taken over by another holder; not renewing it")
        except (JobsctlError, sqlite3.Error) as exc:
            self._diag(f"the job lease was not renewed ({type(exc).__name__})")

    def _release_lease(self) -> None:
        if not self._lease_held:
            return
        try:
            with self.store.write() as tx:
                lease_release(tx, self._lease, self._ident)
        except (JobsctlError, sqlite3.Error) as exc:
            self._diag(f"the job lease was not released ({type(exc).__name__}); it will expire")
        self._lease_held = False

    # -- the start sequence --------------------------------------------------------------------

    def run(self) -> int:
        self._ident = own_identity(self.host)
        if self._confirm() is None:
            return 0
        try:
            if not self._take_lease():
                return 0  # SIGTERM while waiting: nothing written (Signal sequence)
            return self._with_lease()
        finally:
            self._release_lease()

    def _confirm(self) -> ClaimView | None:
        """Step 2, the identity. Polls every 100 ms while the claiming pass is alive and has not
        recorded the shim; exits when a different identity is recorded, the `claim_id` differs, or
        the claiming pass died without recording one. An inconclusive identity check of the pass
        (`UNKNOWN`) is waited out for at most `CONFIRM_UNKNOWN_SECONDS` of continuous
        inconclusiveness, then treated like a dead pass (docs/interface.md, Start sequence 2)."""
        unknown_since: float | None = None
        while not self.terminated:
            view = read_claim(self.store, self.job_id, self.claim_id)
            if view is None or view.status != "starting":
                return None
            recorded = view.shim_identity
            if recorded is None:
                claimer = parse_identity(view.claimed_by)
                if claimer is None:
                    return None
                state = check_identity(self.host, claimer).state
                if state is Liveness.DEAD:
                    return None
                if state is Liveness.UNKNOWN:
                    if unknown_since is None:
                        unknown_since = gateway.monotonic()
                    if gateway.monotonic() - unknown_since >= CONFIRM_UNKNOWN_SECONDS:
                        self._diag("the claiming pass stayed inconclusive; exiting unconfirmed")
                        return None
                else:
                    unknown_since = None
                gateway.sleep(POLL_SECONDS)
                continue
            if recorded != self._ident:
                return None
            if self._retrying(
                lambda: confirm(self.store, self.job_id, self.claim_id, self._ident, self._now()),
                PRE_START_BUSY_SECONDS,
            ):
                return view
            # the claim changed under us (canceled, requeued): look again, then exit
            if read_claim(self.store, self.job_id, self.claim_id) is None:
                return None
            gateway.sleep(POLL_SECONDS)
        return None

    def _still_starting(self) -> ClaimView | None:
        """ "Still starting?": `None` when the job is no longer `starting` with this claim (e.g. it
        was canceled): the shim then runs no further hooks, delivers if it holds the lease and
        exits."""
        view = read_claim(self.store, self.job_id, self.claim_id)
        if view is None or view.status != "starting":
            return None
        return view

    def _leave_unstarted(self) -> int:
        # TODO(#24): deliver the job's undelivered events (the `post-cancel` of a canceled
        # `starting` job among them) while holding the `job:<id>` lease, then exit.
        self._diag("the job is no longer starting under this claim; exiting")
        return 0

    def _with_lease(self) -> int:
        view = self._still_starting()
        if view is None:
            return self._leave_unstarted()
        # TODO(#24): deliver every undelivered synchronous event of this job, in order, here.
        if self.terminated:
            return 0
        if self.config_error is not None:
            self._log(
                "config_invalid",
                view,
                level="error",
                message="the shim could not load its config",
                error=self.config_error.code,
            )
            self._diag(f"config error ({self.config_error.code}); ending the job spawn_failed")
            self._fail(view, "spawn_failed", exit_record=None)
            return 0
        effective = effective_snapshot(self.config, view.row)
        if not self._retrying(
            lambda: record_effective(self.store, self.job_id, self.claim_id, effective),
            PRE_START_BUSY_SECONDS,
        ):
            return self._leave_unstarted()
        if self.terminated:  # still step 2, before the hooks: exit without writing
            return 0
        # TODO(#25), TODO(#23): run the `pre-start` hooks here (veto -> canceled/vetoed,
        # escalation -> held/escalated, `override_hooks`, decision ids). Before each hook and
        # while one runs call `_still_starting()`; a canceled job stops the hook's process group.
        view = self._still_starting()
        if view is None:
            return self._leave_unstarted()
        if self.terminated:  # from the hooks on, SIGTERM ends the job `terminated`
            self._fail(view, "terminated", exit_record=None)
            return 0
        return self._start_and_supervise(view)

    def _fail(
        self,
        view: ClaimView,
        reason: str,
        *,
        exit_record: ExitRecord | None,
        resolved_path: str | None = None,
    ) -> None:
        """End a `starting` attempt `failed`."""

        def write() -> tuple[bool, list[LogRecord]]:
            with self.store.write() as tx:
                fresh = read_claim_in(tx, self.job_id, self.claim_id)
                if fresh is None or fresh.status != "starting":
                    return False, []
                return fail_starting(
                    tx,
                    self.config,
                    fresh,
                    self.claim_id,
                    reason=reason,
                    exit_record=exit_record,
                    resolved_path=resolved_path,
                    now=self._now(),
                )

        won, records = self._retrying(write, FINAL_COMMIT_SECONDS)
        self._emit(records)
        if won:
            self._request_pass()
            # TODO(#24): deliver the `post-fail` event.

    def _request_pass(self) -> None:
        """Start trigger after the final commit; never fails the shim."""
        try:
            request_pass(self.store, self.config, self.config_path, host=self.host)
        except (JobsctlError, sqlite3.Error, OSError) as exc:
            self._diag(f"could not request a pass ({type(exc).__name__})")

    def _start_and_supervise(self, view: ClaimView) -> int:
        row = view.row
        cwd = str(row["cwd"])
        if not _is_directory(cwd):
            self._fail(view, "cwd_missing", exit_record=None)
            return 0
        env_file = load_env_file(self.config.env_file)
        canonical = build_canonical_env(self.config, self.environ)
        self._best_effort(lambda: log_path_state(self.writer, canonical))
        self._best_effort(lambda: log_env_file_state(self.writer, self.config.env_file, env_file))
        try:
            job_env = build_job_env(
                self.config,
                canonical,
                env_file,
                job_id=self.job_id,
                attempt=view.attempt,
                env=json.loads(row["env"]),
                pass_env=json.loads(row["pass_env"]),
            )
        except MissingEnvError as err:
            self._log(
                "missing_env",
                view,
                level="error",
                message="a --pass-env variable is not available",
                variable=err.variable,
                reason=err.reason,
            )
            self._fail(view, "missing_env", exit_record=None)
            return 0
        masking = masking_values(self.config, canonical, env_file, job_env.pass_env_values)
        self.writer = RecordWriter(self.store, masking=masking, clock=self.clock)
        for warning in masking.warnings:
            self._log(
                "short_secret_not_masked",
                view,
                level="warn",
                message="a secret shorter than 8 characters is not masked",
                variable=warning.variable,
            )
        argv: list[str] = json.loads(row["argv"])
        resolved = resolve_argv0(argv[0], job_env.values, cwd) if argv else None
        if resolved is None:
            self._diag("argv[0] could not be resolved to an executable file")
            self._fail(view, "spawn_failed", exit_record=SPAWN_FAILED_EXIT)
            return 0
        settings = self.config.queue(view.queue)
        capture = OutputCapture(
            masking, tail_bytes=settings.tail_bytes, tail_lines=settings.tail_lines
        )
        if self.terminated:  # from the hooks on, SIGTERM ends the job `terminated`
            self._fail(view, "terminated", exit_record=None)
            return 0
        job = self._start(view, argv, resolved, dict(job_env.values), cwd, capture)
        if job is None:
            return 0
        return self._supervise(view, job)

    def _start(
        self,
        view: ClaimView,
        argv: list[str],
        resolved: str,
        env: dict[str, str],
        cwd: str,
        capture: OutputCapture,
    ) -> _Job | None:
        """Step 7: re-check, spawn and set `running` in one `BEGIN IMMEDIATE` transaction. The
        only process spawn inside a transaction: a `cancel` that loses the race sees `running`,
        and one that wins keeps the job from ever being spawned."""
        deadline = gateway.monotonic() + PRE_START_BUSY_SECONDS
        delay = BACKOFF_START
        while True:
            state = _StartState()
            try:
                return self._start_once(view, argv, resolved, env, cwd, capture, state)
            except (JobsctlError, sqlite3.Error):
                # The transaction could not be had (busy, I/O): retry, but only while nothing was
                # spawned; once a job exists the failure is final (it was aborted above).
                if state.popen is not None or gateway.monotonic() + delay >= deadline:
                    raise
                self._tick()
                gateway.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
                if self.terminated:
                    self._fail(view, "terminated", exit_record=None)
                    return None

    def _start_once(
        self,
        view: ClaimView,
        argv: list[str],
        resolved: str,
        env: dict[str, str],
        cwd: str,
        capture: OutputCapture,
        state: _StartState,
    ) -> _Job | None:
        committed = False
        records: list[LogRecord] = []
        try:
            with self.store.write() as tx:
                fresh = read_claim_in(tx, self.job_id, self.claim_id)
                if fresh is None or fresh.status != "starting":
                    return None
                if self.terminated:  # SIGTERM after the checks: end `terminated`, spawn nothing
                    _, records = fail_starting(
                        tx,
                        self.config,
                        fresh,
                        self.claim_id,
                        reason="terminated",
                        exit_record=None,
                        now=self._now(),
                    )
                    failed = True
                else:
                    try:
                        state.popen = gateway.spawn_job(argv, executable=resolved, env=env, cwd=cwd)
                    except (OSError, ValueError) as exc:
                        # ValueError: an embedded NUL or an illegal environment entry
                        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
                        self._diag(f"the job could not be started ({detail})")
                        _, records = fail_starting(
                            tx,
                            self.config,
                            fresh,
                            self.claim_id,
                            reason="spawn_failed",
                            exit_record=SPAWN_FAILED_EXIT,
                            resolved_path=resolved,
                            now=self._now(),
                        )
                        failed = True
                    else:
                        failed = False
                        popen = state.popen
                        pgid = os.getpgid(popen.pid)
                        if pgid != popen.pid:
                            raise JobsctlError("internal", "the job did not lead its own group")
                        won, records = begin_running(
                            tx,
                            self.config,
                            fresh,
                            self.claim_id,
                            job_identity=identity_text(self.host, popen.pid),
                            pgid=pgid,
                            resolved_path=resolved,
                            now=self._now(),
                        )
                        if not won:
                            raise JobsctlError("internal", "the claim was lost inside its own lock")
            committed = True
        except BaseException:
            if state.popen is not None and not committed:
                self._abort_unrecorded(state.popen)
            raise
        self._emit(records)
        if failed:
            self._request_pass()
            return None
        return _Job(state.popen, state.popen.pid, capture)

    def _abort_unrecorded(self, popen: Any) -> None:
        """A job was spawned but `running` was not committed: stop it, so that no process runs
        that the database does not know. The leader is our unreaped child, so its group id is
        reserved and the signal can only reach the group the shim created; the child itself is
        also signaled directly, in case it left that group before the start was checked (a group
        the shim did not create is never signaled)."""
        try:
            gateway_proc.killpg(popen.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            gateway_proc.kill(popen.pid, signal.SIGKILL)  # our unreaped child: the PID is ours
        except OSError:
            pass
        self._diag("the start was not recorded; the job it spawned was killed")
        try:
            popen.wait(timeout=10)
        except Exception:
            pass
        if popen.stdout is not None:
            popen.stdout.close()

    # -- supervising the job -------------------------------------------------------------------

    def _supervise(self, view: ClaimView, job: _Job) -> int:
        popen = job.popen
        assert popen.stdout is not None
        settings_interval = float(self.config.tail_flush_interval)
        stdout_fd = popen.stdout.fileno()
        pump = Pump(
            stdout_fd,
            job.pid,
            job.capture,
            interval=settings_interval,
            periodic=lambda: self._flush(job),
            tick=self._tick,
        )
        record: ExitRecord
        try:
            waited = pump.run()
            pump.drain()
            record = exit_record(waited.si_code, waited.si_status)
        except Exception as exc:  # a programming error must not orphan the job's bookkeeping
            self._diag(f"the output pump failed ({type(exc).__name__}); waiting for the job")
            record = self._wait_blind(job)
        if self.terminated:
            self._diag("SIGTERM received; the job has exited and its end is unchanged")
        leftovers = count_leftovers(
            self.host, leader_pid=job.pid, pgid=job.pid, own_pid=os.getpid()
        )
        if leftovers.count:
            self._log_leftovers(view, leftovers)
        tail = job.capture.snapshot(final=True)
        outcome = natural_outcome(record)
        try:
            self._write_result(view, record, outcome, leftovers, tail)
            committed = self._finish(job, record, outcome, leftovers, tail)
        finally:
            self._reap(job)
            popen.stdout.close()
        if committed is None:
            return 1  # the result file stays for reconcile
        self._delete_result(view)
        # TODO(#24): deliver this job's undelivered events, the post hooks included.
        return 0

    def _wait_blind(self, job: _Job) -> ExitRecord:
        """After a pump failure: keep reading (and discarding) the pipe so the job cannot block on
        a full one, keep the lease renewed, and watch the unreaped leader without blocking; the
        pipe is drained for a bounded time after the exit. If its status cannot be read, no exit
        status was observed (`unknown`)."""
        assert job.popen.stdout is not None
        try:
            pump = Pump(
                job.popen.stdout.fileno(),
                job.pid,
                DiscardCapture(),
                interval=float(self.config.tail_flush_interval),
                periodic=lambda: None,
                tick=self._tick,
            )
            waited = pump.run()
            pump.drain()
            return exit_record(waited.si_code, waited.si_status)
        except Exception as exc:
            self._diag(f"the exit status could not be read ({type(exc).__name__})")
            return ExitRecord("unknown")

    def _flush(self, job: _Job) -> None:
        """Every `tail_flush_interval`: write the live tail and read the force request."""
        snapshot = job.capture.snapshot(final=False)
        try:
            result = flush_tail(self.store, self.job_id, self.claim_id, snapshot, self._now())
        except (JobsctlError, sqlite3.Error) as exc:
            self._diag(f"the tail was not flushed ({type(exc).__name__})")
            return
        if not result.still_running:
            self._diag("the job is no longer running under this claim")
        # TODO(#19): when `result.force_requested_by` is set, run the signal sequence; also the
        # timeout (`effective.timeout`) and a SIGTERM to the shim (`self.terminated`) start it.
        if self.terminated and not self._term_noted:
            self._term_noted = True
            self._diag(
                "SIGTERM received while the job runs; the signal sequence is not implemented yet"
            )

    def _log_leftovers(self, view: ClaimView, leftovers: Leftovers) -> None:
        self._log(
            "leftovers",
            view,
            level="warn",
            message="processes outlived the job's leader; they were not signaled",
            count=leftovers.count,
            complete=leftovers.complete,
        )

    def _write_result(
        self,
        view: ClaimView,
        record: ExitRecord,
        outcome: Any,
        leftovers: Leftovers,
        tail: TailSnapshot,
    ) -> None:
        shims = self.store.paths.shims
        try:
            write_result(
                shims,
                ResultRecord(
                    job_id=self.job_id,
                    attempt=view.attempt,
                    claim_id=self.claim_id,
                    status=outcome.status,
                    reason=outcome.reason,
                    hold_reason=outcome.hold_reason,
                    exit=record,
                    leftovers=leftovers.recorded,
                    tail=ResultTail(tail.text, tail.bytes_seen, tail.truncated),
                    ended_at=self._now(),
                ),
            )
        except ResultFileError as exc:
            self._diag(f"the result file was not written ({exc})")

    def _delete_result(self, view: ClaimView) -> None:
        try:
            delete_result(self.store.paths.shims, self.job_id, view.attempt)
        except ResultFileError as exc:
            self._diag(f"the result file was not removed ({exc})")

    def _finish(
        self,
        job: _Job,
        record: ExitRecord,
        outcome: Any,
        leftovers: Leftovers,
        tail: TailSnapshot,
    ) -> bool | None:
        """Step 9, the final transaction, retried with backoff for up to 10 minutes. Returns
        whether it committed, or `None` when it never could."""
        try:
            result = self._retrying(
                lambda: finish_running(
                    self.store,
                    self.config,
                    self.job_id,
                    self.claim_id,
                    outcome=outcome,
                    exit_record=record,
                    leftovers=leftovers.recorded,
                    tail=tail,
                    now=self._now(),
                ),
                FINAL_COMMIT_SECONDS,
            )
        except Exception as exc:  # any error, until the deadline (Start sequence step 9)
            self._diag(f"the final commit failed ({type(exc).__name__}); the result file stays")
            return None
        self._emit(result.records)
        if result.committed:
            self._request_pass()
        else:
            self._diag("the attempt was already finalized by someone else; nothing written")
        return result.committed

    def _reap(self, job: _Job) -> None:
        try:
            reap_held_leader(job.popen)
        except OSError as exc:
            self._diag(f"could not reap the job leader ({exc.strerror})")


def _is_directory(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return False
