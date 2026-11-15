"""`_deliver`: the delivery process (docs/interface.md, Pass step 5, Delivery, Hook processes).

One `_deliver` runs at a time, under the `deliver` lease. A round does two things:

1. the synchronous events of jobs whose `job:<id>` lease has no live holder (`hooks_redelivered`):
   per job it takes the lease without waiting (a live holder delivers its own events), delivers
   in outbox order and releases it;
2. every `async` event: at most 8 hook processes at once across all jobs, at most one per job,
   in outbox order per job. Each event runs in a worker thread that only runs the hooks; the
   main thread reads and writes the database, appends the log records and marks an event
   delivered once its hooks ended (exit or timeout).

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
from jobsctl.delivery import Deliverer, EventPlan
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
        try:
            while True:
                with self.store.read() as tx:
                    seen = counter_get(tx, DELIVER_REQUESTED)
                self._round()
                if self.stop.is_set or not lease.held:
                    return 0
                with self.store.write() as tx:
                    if counter_get(tx, DELIVER_REQUESTED) == seen:
                        lease.release_in(tx)
                        return 0
        finally:
            lease.release()

    def _round(self) -> None:
        self.deliverer.refresh()
        self._redeliver_sync()
        if not self.stop.is_set:
            self._run_async()

    # -- synchronous events of jobs with no live holder ----------------------------------------

    def _redeliver_sync(self) -> None:
        deliver_lease = self._lease
        assert deliver_lease is not None
        for row in self.store.query_all(_SYNC_JOBS):
            if self.stop.is_set or not deliver_lease.held:
                return
            job_id = str(row["job_id"])
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
            finally:
                lease.release()

    # -- async events --------------------------------------------------------------------------

    def _run_async(self) -> None:
        lease = self._lease
        assert lease is not None
        flights: dict[Future[bool], _Flight] = {}
        busy: set[str] = set()
        error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=self.max_async) as pool:
            while True:
                if error is None and not self.stop.is_set and lease.held:
                    self._dispatch(pool, flights, busy)
                if not flights:
                    break
                done, _ = wait(
                    list(flights), timeout=self.wait_seconds, return_when=FIRST_COMPLETED
                )
                lease.tick()
                for future in done:
                    flight = flights.pop(future)
                    busy.discard(flight.plan.job_id)
                    try:
                        complete = future.result()
                        self._finish(flight, complete)
                    except BaseException as exc:  # noqa: BLE001 - finish the others, then raise
                        error = error or exc
                        for result in flight.results:
                            self.deliverer.record(flight.plan, result)
        if error is not None:
            raise error

    def _dispatch(
        self, pool: ThreadPoolExecutor, flights: dict[Future[bool], _Flight], busy: set[str]
    ) -> None:
        if len(flights) >= self.max_async:
            return
        for row in self.store.query_all(_FIRST_ASYNC):
            job_id = str(row["job_id"])
            if job_id in busy:
                continue
            if len(flights) >= self.max_async:
                return
            plan = self.deliverer.plan_event(row)
            if plan.problem is not None or not plan.hooks:
                if plan.problem is not None:
                    self.deliverer.record_problem(plan)
                self.deliverer.mark_delivered(plan.event_id)
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

    def _finish(self, flight: _Flight, complete: bool) -> None:
        for result in flight.results:
            self.deliverer.record(flight.plan, result)
        if complete:
            self.deliverer.mark_delivered(flight.plan.event_id)
