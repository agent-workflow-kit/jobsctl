"""`_kill` as a real detached process, after a real shim was SIGKILLed (design.md spike A5).

Every PID is recorded when it is started or announced (`real_shims`), and only recorded
processes are ever signaled by the test; the code under test signals only through identity checks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Liveness, check_identity
from jobsctl.spawn import spawn_internal
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from tests.shim_helpers import JOB, log_records
from tests.shim_real import RealShims

# A leader that ignores SIGTERM, with a child in its group and one that left it (own session).
SCRIPT = (
    "import signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "member = subprocess.Popen(['sleep', '600'])\n"
    "leaver = subprocess.Popen(['sleep', '600'], start_new_session=True)\n"
    "print(member.pid, leaver.pid, flush=True)\n"
    "time.sleep(600)\n"
)
STUBBORN = [sys.executable, "-I", "-c", SCRIPT]


def alive(real_shims: RealShims, pid: int) -> bool:
    for ident in real_shims.extras:
        if ident.pid == pid:
            return check_identity(default_host(), ident).state is Liveness.ALIVE
    raise AssertionError(f"pid {pid} was not tracked")


def leader_alive(real_shims: RealShims) -> bool:
    return check_identity(default_host(), real_shims.job_identity()).state is Liveness.ALIVE


def announced(real_shims: RealShims) -> tuple[int, int]:
    """The two PIDs the job printed, recorded with their identities the moment they are read."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        row = real_shims.row("SELECT tail FROM outputs WHERE job_id = ?", JOB)
        words = bytes(row["tail"]).split() if row is not None and row["tail"] else []
        if len(words) == 2:
            member, leaver = (int(w) for w in words)
            real_shims.track(member)
            real_shims.track(leaver)
            return member, leaver
        time.sleep(0.05)
    raise AssertionError("the job did not announce its processes")


def lose_the_shim(real_shims: RealShims, popen: subprocess.Popen[bytes]) -> None:
    popen.kill()  # our own unreaped child
    popen.wait(timeout=30)


def request_force(real_shims: RealShims) -> None:
    with Store.open(mode=OpenMode.CLI) as store, store.write() as tx:
        tx.execute("UPDATE attempts SET force_requested_by = 'human:t' WHERE job_id = ?", [JOB])


def spawn_kill(real_shims: RealShims) -> subprocess.Popen[bytes]:
    popen = spawn_internal(
        resolve_state_dir(), real_shims.config, real_shims.config_path, "_kill", [JOB]
    )
    real_shims.handles.append(popen)
    return popen


def status(real_shims: RealShims) -> Any:
    return real_shims.row("SELECT status, reason FROM jobs WHERE id = ?", JOB)


def test_sigkill_of_the_shim_leaves_the_job_running_and_holding_its_slot(
    real_shims: RealShims,
) -> None:
    """Spike A5: no SIGHUP, no PDEATHSIG; jobsctl does not kill the job either."""
    popen = real_shims.seed_and_spawn(["sleep", "600"])
    real_shims.wait_status("running")
    lose_the_shim(real_shims, popen)
    time.sleep(1.0)
    assert leader_alive(real_shims)
    assert tuple(status(real_shims)) == ("running", None)
    # the dead shim's lease row stays until a new holder finds its holder dead
    assert real_shims.row("SELECT 1 FROM leases WHERE name = ?", f"job:{JOB}") is not None


def test_a_kill_process_finishes_a_forced_cancel_of_a_shim_lost_job(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(STUBBORN, overrides={"kill_grace": 1})
    real_shims.wait_status("running")
    member, leaver = announced(real_shims)
    lose_the_shim(real_shims, popen)
    request_force(real_shims)
    kill = spawn_kill(real_shims)
    assert kill.wait(timeout=60) == 0
    assert tuple(status(real_shims)) == ("canceled", "forced")
    assert not leader_alive(real_shims)
    assert not alive(real_shims, member)  # the group got the sequence
    assert alive(real_shims, leaver)  # a process that left the group is never signaled
    att = real_shims.row("SELECT * FROM attempts WHERE job_id = ?", JOB)
    assert att["status"] == "canceled" and att["leftovers"] == 0
    assert json.loads(att["exit"])["kind"] == "unknown"
    assert real_shims.row("SELECT 1 FROM leases WHERE name = ?", f"job:{JOB}") is None
    assert len([r for r in log_records("transition") if r.get("to") == "canceled"]) == 1


def test_duplicate_kill_processes_finalize_the_job_once(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(STUBBORN, overrides={"kill_grace": 1})
    real_shims.wait_status("running")
    announced(real_shims)
    lose_the_shim(real_shims, popen)
    request_force(real_shims)
    first, second = spawn_kill(real_shims), spawn_kill(real_shims)
    assert first.wait(timeout=60) in (0, 1) and second.wait(timeout=60) in (0, 1)
    assert tuple(status(real_shims)) == ("canceled", "forced")
    assert len([r for r in log_records("transition") if r.get("to") == "canceled"]) == 1


def test_a_kill_process_with_no_force_request_or_a_live_shim_signals_nothing(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "600"])
    real_shims.wait_status("running")
    assert spawn_kill(real_shims).wait(timeout=60) == 0  # live shim and no request
    assert leader_alive(real_shims) and popen.poll() is None
    lose_the_shim(real_shims, popen)
    assert spawn_kill(real_shims).wait(timeout=60) == 0  # shim gone, but no committed request
    assert leader_alive(real_shims)
    assert tuple(status(real_shims)) == ("running", None)


def test_sigterm_stops_a_kill_process_midway_and_a_second_one_finishes(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(STUBBORN, overrides={"kill_grace": 3})
    real_shims.wait_status("running")
    announced(real_shims)
    lose_the_shim(real_shims, popen)
    request_force(real_shims)
    first = spawn_kill(real_shims)
    diag = resolve_state_dir() / "diag" / f"kill-{first.pid}.log"
    deadline = time.monotonic() + 60
    while not (diag.exists() and "running the signal sequence" in diag.read_text()):
        assert time.monotonic() < deadline and first.poll() is None
        time.sleep(0.05)
    os.kill(first.pid, signal.SIGHUP)  # ignored
    time.sleep(0.3)
    assert first.poll() is None
    os.kill(first.pid, signal.SIGTERM)  # stops it between steps
    assert first.wait(timeout=60) == 1
    assert tuple(status(real_shims)) == ("running", None)
    assert real_shims.row("SELECT 1 FROM leases WHERE name = ?", f"job:{JOB}") is None
    second = spawn_kill(real_shims)
    assert second.wait(timeout=60) == 0
    assert tuple(status(real_shims)) == ("canceled", "forced")
