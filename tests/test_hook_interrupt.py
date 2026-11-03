"""A command interrupted while it runs a synchronous hook stops the hook's group and exits
130 (SIGINT) or 1 (SIGHUP, SIGTERM); the event stays undelivered."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

import pytest

from jobsctl.errors import ExitCode, JobsctlError
from jobsctl.hook_env import build_hook_env
from jobsctl.hook_interrupt import InterruptGuard
from jobsctl.hook_run import HookTuning, run_hook
from tests.hook_real import HookTracker, recorded_pids, running_pids, tracker_fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process groups")
tracker = pytest.fixture(tracker_fixture)
FAST = HookTuning(term_grace=0.4, kill_wait=3.0, drain=0.3, stop_interval=0.05)


def fire(sig: int, after: float = 0.3) -> threading.Timer:
    """Deliver ``sig`` to this very process (the firing command) after ``after`` seconds."""
    timer = threading.Timer(after, os.kill, (os.getpid(), sig))
    timer.start()
    return timer


@pytest.mark.parametrize(
    ("sig", "code", "exit_code"),
    [
        (signal.SIGINT, "interrupted", ExitCode.INTERRUPTED),
        (signal.SIGHUP, "internal", ExitCode.INTERNAL),
        (signal.SIGTERM, "internal", ExitCode.INTERNAL),
    ],
)
def test_a_signal_stops_the_hook_group_and_maps_to_the_exit_code(
    tracker: HookTracker, tmp_path: Path, sig: signal.Signals, code: str, exit_code: int
) -> None:
    pidfile = tmp_path / "pids"
    env = build_hook_env({"PATH": os.environ["PATH"]}, hook="post-held", job_id="jb-abcdefgh")
    before = signal.getsignal(sig)
    timer = fire(sig)
    try:
        with InterruptGuard() as guard:
            res = run_hook(
                hook="post-held",
                hook_id="h",
                cmd=f"trap '' TERM; sleep 60 & echo $! > {pidfile}; sleep 60",
                payload=b"{}\n",
                timeout=60,
                env=env,
                cwd=tmp_path,
                stop=guard,
                tuning=FAST,
            )
            with pytest.raises(JobsctlError) as info:
                guard.check(res)
    finally:
        timer.cancel()
    assert res.stopped == sig.name and res.signals_sent == ("SIGTERM", "SIGKILL")
    assert info.value.code == code and info.value.exit_code == exit_code
    assert sig.name in info.value.message and "undelivered" in info.value.message
    pids = recorded_pids(pidfile)
    assert len(pids) == 1 and running_pids(pids) == []
    assert tracker.last.popen.returncode is not None  # the leader was held, then reaped
    assert signal.getsignal(sig) == before  # handlers restored


def test_no_signal_means_no_error(tracker: HookTracker, tmp_path: Path) -> None:
    with InterruptGuard() as guard:
        res = run_hook(
            hook="post-held",
            hook_id="h",
            cmd="exit 0",
            payload=b"{}\n",
            timeout=10,
            env={"PATH": os.environ["PATH"]},
            cwd=tmp_path,
            stop=guard,
            tuning=FAST,
        )
        guard.check(res)
    assert res.outcome == "allow" and res.stopped is None and guard() is None


def test_the_first_signal_decides_and_a_signal_before_start_runs_nothing(
    tracker: HookTracker, tmp_path: Path
) -> None:
    with InterruptGuard() as guard:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        res = run_hook(
            hook="post-held",
            hook_id="h",
            cmd="exit 0",
            payload=b"{}\n",
            timeout=10,
            env={"PATH": os.environ["PATH"]},
            cwd=tmp_path,
            stop=guard,
            tuning=FAST,
        )
        with pytest.raises(JobsctlError) as info:
            guard.check(res)
    assert tracker.spawned == [] and info.value.exit_code == ExitCode.INTERNAL


def test_check_raises_for_a_signal_that_arrived_after_the_last_stop_poll(
    tracker: HookTracker, tmp_path: Path
) -> None:
    with InterruptGuard() as guard:
        res = run_hook(
            hook="post-held",
            hook_id="h",
            cmd="exit 0",
            payload=b"{}\n",
            timeout=10,
            env={"PATH": os.environ["PATH"]},
            cwd=tmp_path,
            stop=guard,
            tuning=FAST,
        )
        assert res.stopped is None and res.outcome == "allow"  # it finished on its own
        os.kill(os.getpid(), signal.SIGINT)  # after the last poll
        with pytest.raises(JobsctlError) as info:
            guard.check(res)
    assert info.value.code == "interrupted" and info.value.exit_code == ExitCode.INTERRUPTED
