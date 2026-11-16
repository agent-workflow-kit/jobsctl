"""SIGTERM to a real detached `_shim` while its job runs (docs/interface.md, Signal sequence).

Every PID is recorded by the `real_shims` fixture and signaled by its recorded identity.
"""

from __future__ import annotations

import os
import signal
import sys

from jobsctl.leases import parse_identity
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Liveness, check_identity
from tests.shim_helpers import JOB
from tests.shim_real import RealShims

TERM_IGNORER = [
    sys.executable, "-I", "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)",
]  # fmt: skip


def job_alive(real_shims: RealShims) -> bool:
    row = real_shims.row("SELECT job_identity FROM attempts WHERE job_id = ?", JOB)
    ident = parse_identity(row["job_identity"])
    assert ident is not None
    return check_identity(default_host(), ident).state is Liveness.ALIVE


def end(real_shims: RealShims) -> tuple[str, str | None]:
    row = real_shims.row("SELECT status, reason FROM jobs WHERE id = ?", JOB)
    return row["status"], row["reason"]


def test_sigterm_to_the_shim_ends_the_running_job_terminated(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "600"], overrides={"kill_grace": 5})
    real_shims.wait_status("running")
    os.kill(popen.pid, signal.SIGTERM)  # our own child, unreaped
    assert popen.wait(timeout=60) == 0
    assert end(real_shims) == ("failed", "terminated")
    assert not job_alive(real_shims)
    assert real_shims.row("SELECT 1 FROM leases WHERE name = ?", f"job:{JOB}") is None


def test_sigterm_to_the_shim_kills_a_sigterm_ignoring_job_after_the_grace(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(TERM_IGNORER, overrides={"kill_grace": 1})
    real_shims.wait_status("running")
    os.kill(popen.pid, signal.SIGTERM)
    os.kill(popen.pid, signal.SIGTERM)  # duplicated: one sequence
    assert popen.wait(timeout=60) == 0
    assert end(real_shims) == ("failed", "terminated")
    assert not job_alive(real_shims)


def test_sigterm_with_a_force_request_ends_canceled_forced(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "600"])
    real_shims.wait_status("running")
    real_shims.row("SELECT 1")  # a read; the write below goes through the store
    from jobsctl.store_db import OpenMode, Store

    with Store.open(mode=OpenMode.CLI) as store, store.write() as tx:
        tx.execute("UPDATE attempts SET force_requested_by = 'human:t' WHERE job_id = ?", [JOB])
    os.kill(popen.pid, signal.SIGTERM)
    assert popen.wait(timeout=60) == 0
    assert end(real_shims) == ("canceled", "forced")
    assert not job_alive(real_shims)


def test_a_timeout_in_a_real_shim_ends_the_job_failed_timeout(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "600"], overrides={"timeout": 1})
    assert popen.wait(timeout=60) == 0
    assert end(real_shims) == ("failed", "timeout")
    assert not job_alive(real_shims)
