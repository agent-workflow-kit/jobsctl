"""The guard that keeps a test from signaling a process it did not start (tests/kill_guard.py)."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

import pytest

from tests import kill_guard

FABRICATED = 4_000_001  # no real call may be made for it: the fakes below record any


def test_the_guard_is_installed_on_the_real_entry_points() -> None:
    assert os.kill.__module__ == "tests.kill_guard"
    assert os.killpg.__module__ == "tests.kill_guard"


def test_a_signal_to_an_unrecorded_pid_fails_before_the_underlying_call() -> None:
    calls: list[tuple[Any, ...]] = []
    kill = kill_guard.guarded_kill(lambda *args: calls.append(args))
    killpg = kill_guard.guarded_killpg(lambda *args: calls.append(args))
    for call in (kill, killpg):
        with pytest.raises(pytest.fail.Exception):
            call(FABRICATED, signal.SIGKILL)
    assert calls == []
    assert len(kill_guard.violations) == 2
    kill_guard.violations.clear()  # expected here: the teardown would otherwise fail the test


def test_the_real_os_kill_is_guarded_too() -> None:
    with pytest.raises(pytest.fail.Exception):
        os.kill(FABRICATED, signal.SIGTERM)
    with pytest.raises(pytest.fail.Exception):
        os.killpg(FABRICATED, signal.SIGTERM)
    kill_guard.violations.clear()


def test_the_raw_pidfd_syscall_is_guarded() -> None:
    calls: list[int] = []
    raw = kill_guard.guarded_raw(lambda number, *args: calls.append(number) or 0)
    pidfd = os.pipe()[0]  # not a pidfd: the target cannot be resolved, which is refused
    try:
        with pytest.raises(pytest.fail.Exception):
            raw(424, pidfd, signal.SIGKILL, 0, 0)
    finally:
        os.close(pidfd)
    assert calls == []
    kill_guard.violations.clear()


def test_signal_zero_and_our_own_children_pass() -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(FABRICATED, 0)  # a probe signals nothing: the real answer comes through
    child = subprocess.Popen(["sleep", "600"])
    try:
        os.kill(child.pid, 0)
    finally:
        os.kill(child.pid, signal.SIGKILL)  # the exact child this test started
        child.wait()


def test_a_registered_pid_passes_the_guard() -> None:
    calls: list[tuple[Any, ...]] = []
    kill = kill_guard.guarded_kill(lambda *args: calls.append(args))
    with pytest.raises(pytest.fail.Exception):
        kill(FABRICATED, signal.SIGTERM)
    kill_guard.violations.clear()
    kill_guard.allow(FABRICATED)
    kill(FABRICATED, signal.SIGTERM)
    assert calls == [(FABRICATED, signal.SIGTERM)]
