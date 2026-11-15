"""Reconcile against real shims, jobs and `_kill` processes (design.md spike A5; interface.md
Reconcile). Every PID is recorded when it is started (`real_shims`) and only recorded processes
are signaled by the test; reconcile itself must signal nothing, which `kill_guard` enforces for
everything but the `_kill` child (a separate process).
"""

from __future__ import annotations

import subprocess
import threading
import time

from jobsctl import gateway
from jobsctl.config import Config
from jobsctl.reconcile import Reconciler, run_rules
from jobsctl.runner import Runner
from jobsctl.store_db import OpenMode, Store
from tests.shim_helpers import JOB
from tests.shim_real import RealShims
from tests.test_kill_processes import (
    STUBBORN,
    alive,
    announced,
    leader_alive,
    lose_the_shim,
    request_force,
    status,
)


class Ran:
    def __init__(self, runner: Runner, reconciled: list[dict[str, str]]) -> None:
        self.runner = runner
        self.reconciled = reconciled


def reconcile_once(real_shims: RealShims) -> Ran:
    """A `reconcile` run as the command does it (lease, rules, release), in this process."""
    with Store.open(mode=OpenMode.CLI) as store:
        runner = Runner(store, real_shims.config, real_shims.config_path)
        result = runner.run_reconcile()
        real_shims.handles.extend(c.popen for c in runner.children)
        return Ran(runner, result.reconciled)


def findings(ran: Ran) -> list[tuple[str, str]]:
    return [(f["finding"], f["action"]) for f in ran.reconciled]


def test_a_live_slow_shim_is_never_touched_while_reconcile_runs(real_shims: RealShims) -> None:
    real_shims.seed_and_spawn(["sleep", "2"])
    real_shims.wait_status("running")
    seen: list[list[tuple[str, str]]] = []
    while status(real_shims)[0] == "running":
        seen.append(findings(reconcile_once(real_shims)))
        time.sleep(0.1)
    real_shims.wait_status("done")
    assert all(f == [] for f in seen) and seen


def test_a_sigkilled_shim_is_marked_lost_once_and_the_job_is_not_signaled(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "600"])
    real_shims.wait_status("running")
    lose_the_shim(real_shims, popen)
    assert findings(reconcile_once(real_shims)) == [("shim_lost", "marked_shim_lost")]
    assert findings(reconcile_once(real_shims)) == []
    assert leader_alive(real_shims)
    assert tuple(status(real_shims)) == ("running", None)
    assert real_shims.row("SELECT shim_lost FROM jobs WHERE id = ?", JOB)[0] == 1


def test_force_resumed_starts_a_real_kill_that_finishes_the_cancel(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(STUBBORN, overrides={"kill_grace": 1})
    real_shims.wait_status("running")
    member, leaver = announced(real_shims)
    lose_the_shim(real_shims, popen)
    request_force(real_shims)
    ran = reconcile_once(real_shims)
    assert findings(ran) == [
        ("shim_lost", "marked_shim_lost"),
        ("force_resumed", "kill_started"),
    ]
    (kill,) = [c.popen for c in ran.runner.children if c.kind == "kill"]
    assert kill.wait(timeout=60) == 0
    assert tuple(status(real_shims)) == ("canceled", "forced")
    assert not leader_alive(real_shims) and not alive(real_shims, member)
    assert alive(real_shims, leaver)  # outside the group: never signaled
    assert findings(reconcile_once(real_shims)) == []


def test_a_job_that_died_with_its_shim_is_failed_lost_with_its_leftovers_counted(
    real_shims: RealShims,
) -> None:
    import signal

    from jobsctl.proc_host import default_host
    from jobsctl.proc_signal import signal_process
    from tests.kill_guard import allow

    popen = real_shims.seed_and_spawn(STUBBORN, overrides={"kill_grace": 1})
    real_shims.wait_status("running")
    member, _ = announced(real_shims)
    leader = real_shims.job_identity()
    lose_the_shim(real_shims, popen)
    allow(leader.pid)
    signal_process(default_host(), leader, signal.SIGKILL, allow_zombie=True)
    deadline = time.monotonic() + 60
    while leader_alive(real_shims) and time.monotonic() < deadline:
        time.sleep(0.05)
    ran = reconcile_once(real_shims)
    assert findings(ran) == [("job_gone", "failed_lost")]
    assert tuple(status(real_shims)) == ("failed", "lost")
    count = real_shims.row("SELECT leftovers FROM attempts WHERE job_id = ?", JOB)[0]
    assert count == 1 and alive(real_shims, member)  # counted, not signaled


def test_two_reconciles_at_once_finalize_the_job_exactly_once(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "600"])
    real_shims.wait_status("running")
    leader = real_shims.job_identity()
    lose_the_shim(real_shims, popen)
    import signal

    from jobsctl.proc_host import default_host
    from jobsctl.proc_signal import signal_process
    from tests.kill_guard import allow

    allow(leader.pid)
    signal_process(default_host(), leader, signal.SIGKILL, allow_zombie=True)
    while leader_alive(real_shims):
        time.sleep(0.05)
    results: list[int] = []
    barrier = threading.Barrier(4)

    def work() -> None:
        with Store.open(mode=OpenMode.CLI) as store:
            reconciler = Reconciler(Config(auto_run=False), default_host(), store.paths.shims)
            barrier.wait(timeout=30)
            applied = run_rules(
                reconciler,
                read=store.read,
                write=store.write,
                clock=gateway.utc_now,
                dry_run=False,
            )
            results.append(len(applied))

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == [0, 0, 0, 1]
    assert tuple(status(real_shims)) == ("failed", "lost")
    done = real_shims.row("SELECT count(*) FROM attempts WHERE status = 'failed'")[0]
    assert done == 1
    assert isinstance(popen, subprocess.Popen)
