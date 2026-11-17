"""`_deliver`: the delivery process (docs/interface.md, Pass step 5, Delivery, Hook processes).

One `_deliver` runs at a time, under the `deliver` lease. A round does two things:

1. the synchronous events of jobs whose `job:<id>` lease has no live holder (`hooks_redelivered`):
   per job it takes the lease without waiting (a live holder delivers its own events), delivers
   in outbox order and releases it;
2. every `async` event: at most 8 hook processes at once across all jobs, at most one per job,
   in outbox order per job. Each event runs in a worker thread that only runs the hooks; the
   main thread reads and writes the database, appends the log records and marks an event
   delivered once its hooks ended (exit or timeout).

A hook whose leader outlived SIGKILL (an `internal` error) does not stop the round: the other jobs
are delivered, that job is skipped for the rest of the round, its event stays undelivered and backs
off (`hook_retry`: skipped until its `retry_after`, then retried, at the capped interval for ever)
and the error is raised at the end of the round, so the process exits non-zero.

It re-reads the `deliver_requested` counter and releases the lease in one transaction and starts
over if the counter changed, so a request made while it ran is not lost. SIGTERM stops running
hooks (their events stay undelivered), releases the lease and exits.
"""

from __future__ import annotations

import signal
import sqlite3
import sys
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from jobsctl.config import Config
from jobsctl.delivery import Deliverer, EventPlan, UnkillableHook
from jobsctl.errors import JobsctlError
from jobsctl.hook_run import HookResult
from jobsctl.job_lease import Lease
from jobsctl.proc_host import Host, default_host
from jobsctl.shim import StopSignal
from jobsctl.store_db import Store
from jobsctl.store_ops import counter_get
from jobsctl.triggers import DELIVER_REQUESTED

MAX_ASYNC = 8
WAIT_SECONDS = 0.25
_FIRST_ASYNC = (
    "SELECT * FROM hook_outbox WHERE delivered_at IS NULL AND async = 1 AND seq IN "
    "(SELECT MIN(seq) FROM hook_outbox WHERE delivered_at IS NULL AND async = 1 GROUP BY job_id) "
    "ORDER BY seq"
)
_SYNC_JOBS = (
    "SELECT job_id FROM hook_outbox WHERE delivered_at IS NULL AND async = 0 "
    "GROUP BY job_id ORDER BY MIN(seq)"
)


@dataclass
class _Flight:
    row: sqlite3.Row
    plan: EventPlan
    results: list[HookResult] = field(default_factory=list)
    recorded: bool = False


def prepare_process(stop: StopSignal) -> None:
    """SIGHUP and SIGINT are ignored (by a no-op handler); SIGTERM stops the running hooks."""
    signal.signal(signal.SIGHUP, lambda signum, frame: None)
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())


class Delivery:
    def __init__(
        self,
        store: Store,
        config: Config,
        config_path: Any,
        *,
        deliverer: Deliverer | None = None,
        host: Host | None = None,
        stop: StopSignal | None = None,
        max_async: int = MAX_ASYNC,
        wait_seconds: float = WAIT_SECONDS,
        diag: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.config_path = config_path
        self.host = host or default_host()
        self.stop = stop or StopSignal()
        self.max_async = max_async
        self.wait_seconds = wait_seconds
        self.diag = diag or self._diag
        self.deliverer = deliverer or Deliverer(store, config, host=self.host, diag=self.diag)
        self._lease: Lease | None = None
        self._skipped: set[str] = set()  # jobs whose hook went unkillable, for this round
        self._unkillable: JobsctlError | None = None

    def _diag(self, message: str) -> None:
        sys.stderr.write(f"_deliver: {message}\n")
        sys.stderr.flush()

    def _stop_reason(self) -> str | None:
        return "SIGTERM" if self.stop.is_set else None

    # -- the run -------------------------------------------------------------------------------

    def run(self) -> int:
        lease = Lease.take(self.store, "deliver", host=self.host)
        if lease is None:
            self.diag("another live process holds the deliver lease; nothing to do")
            return 0
        self._lease = lease
        self._skipped = set()  # kept across the re-runs of one run, so a stuck job is not re-run
        self._unkillable = None
        try:
            while True:
                with self.store.read() as tx:
                    seen = counter_get(tx, DELIVER_REQUESTED)
                try:
                    self._round()
                except JobsctlError as exc:
                    if exc is not self._unkillable:
                        raise
                if self.stop.is_set or not lease.held:
                    return self._end_run()
                with self.store.write() as tx:
                    quiet = counter_get(tx, DELIVER_REQUESTED) == seen
                    if quiet:
                        lease.release_in(tx)
                if quiet:  # outside the transaction: raising inside it would undo the release
                    return self._end_run()
        finally:
            lease.release()

    def _end_run(self) -> int:
        """End the run: the deferred unkillable-hook error (if any) is raised only now, after the
        counter re-check and the lease release, so a request made meanwhile is not lost."""
        if self._unkillable is not None:
            raise self._unkillable
        return 0

    def _round(self) -> None:
        self.deliverer.refresh()
        self._redeliver_sync()
        if not self.stop.is_set:
            self._run_async()
        if self._unkillable is not None:
            raise self._unkillable

    def _note_unkillable(self, job_id: str, exc: JobsctlError) -> None:
        """A hook of ``job_id`` outlived SIGKILL: skip the job for the rest of the round and
        raise the error when the round ends."""
        self._skipped.add(job_id)
        self._unkillable = self._unkillable or exc
        self.diag(f"a hook of {job_id} outlived SIGKILL; its event stays undelivered")

    # -- synchronous events of jobs with no live holder ----------------------------------------

    def _redeliver_sync(self) -> None:
        deliver_lease = self._lease
        assert deliver_lease is not None
        for row in self.store.query_all(_SYNC_JOBS):
            if self.stop.is_set or not deliver_lease.held:
                return
            job_id = str(row["job_id"])
            if job_id in self._skipped:
                continue
            first = self.deliverer.next_event(job_id, is_async=False)
            if first is not None and self.deliverer.defer_notice(first):
                continue  # backing off: its later events wait behind it, as the order requires
            lease = Lease.take(self.store, f"job:{job_id}", host=self.host)
            if lease is None:  # a live holder delivers its own events
                continue

            def tick(lease: Lease = lease) -> None:
                lease.tick()
                deliver_lease.tick()

            try:
                self.deliverer.deliver_sync(
                    job_id,
                    stop=self._stop_reason,
                    held=lambda lease=lease: lease.held and deliver_lease.held,
                    tick=tick,
                )
            except JobsctlError as exc:
                if exc.code != "internal":
                    raise
                self._note_unkillable(job_id, exc)  # the other jobs are still delivered
            finally:
                lease.release()

    # -- async events --------------------------------------------------------------------------

    def _run_async(self) -> None:
        lease = self._lease
        assert lease is not None
        flights: dict[Future[bool], _Flight] = {}
        busy: set[str] = set()
        error: BaseException | None = None  # ends the round: no more dispatching, then raised

        def fail(exc: BaseException) -> None:
            nonlocal error
            error = error or exc
            self.stop.set()  # the running hooks are stopped; the flights are drained below

        with ThreadPoolExecutor(max_workers=self.max_async) as pool:
            while True:
                if error is None and not self.stop.is_set and lease.held:
                    try:
                        while self._dispatch(pool, flights, busy):
                            pass  # events marked without a hook may unblock the job's next one
                    except Exception as exc:  # noqa: BLE001 - drain the flights, then raise
                        fail(exc)
                if not flights:
                    break
                done, _ = wait(
                    list(flights), timeout=self.wait_seconds, return_when=FIRST_COMPLETED
                )
                try:
                    lease.tick()
                except Exception as exc:  # noqa: BLE001
                    fail(exc)
                for future in done:
                    flight = flights.pop(future)
                    busy.discard(flight.plan.job_id)
                    try:
                        complete = future.result()
                    except JobsctlError as exc:
                        self._record_once(flight)
                        if exc.code == "internal":  # an unkillable hook: only its job is skipped
                            if isinstance(exc, UnkillableHook):
                                self.deliverer.note_unkillable(flight.plan.event_id)
                            self._note_unkillable(flight.plan.job_id, exc)
                        else:
                            fail(exc)
                    except BaseException as exc:  # noqa: BLE001 - finish the others, then raise
                        self._record_once(flight)
                        fail(exc)
                    else:
                        try:
                            self._finish(flight, complete)
                        except BaseException as exc:  # noqa: BLE001
                            fail(exc)
        if error is not None:
            raise error

    def _dispatch(
        self, pool: ThreadPoolExecutor, flights: dict[Future[bool], _Flight], busy: set[str]
    ) -> bool:
        """Start the oldest async event of every idle job (up to the limit). ``True`` when an
        event was marked delivered without a hook to run (none selected, damaged, no longer
        configured): the job's next event is then due, so the caller dispatches again."""
        marked = False
        if len(flights) >= self.max_async:
            return False
        for row in self.store.query_all(_FIRST_ASYNC):
            job_id = str(row["job_id"])
            if job_id in busy or job_id in self._skipped:
                continue
            if self.deliverer.defer_notice(row):
                continue  # backing off: the job's later events wait behind it
            if len(flights) >= self.max_async:
                return marked
            plan = self.deliverer.plan_event(row)
            if plan.problem is not None or not plan.hooks:
                if plan.problem is not None:
                    self.deliverer.record_problem(plan)
                self.deliverer.mark_delivered(plan.event_id)
                marked = True
                continue
            flight = _Flight(row, plan)
            future = pool.submit(
                self.deliverer.run_hooks,
                plan,
                stop=self._stop_reason,
                on_result=flight.results.append,
            )
            flights[future] = flight
            busy.add(job_id)
        return marked

    def _record_once(self, flight: _Flight) -> None:
        if not flight.recorded:
            flight.recorded = True
            for result in flight.results:
                self.deliverer.record(flight.plan, result)

    def _finish(self, flight: _Flight, complete: bool) -> None:
        self._record_once(flight)
        if complete:
            self.deliverer.mark_delivered(flight.plan.event_id)
