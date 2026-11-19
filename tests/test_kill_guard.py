"""The guard that keeps a test from signaling a process it did not start (tests/kill_guard.py)."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

import pytest

from jobsctl import gateway_proc
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


def test_a_real_grandchild_is_a_descendant_and_an_unrelated_pid_is_not() -> None:
    """The parent walk goes through the platform host: a process we did not spawn directly (a
    child shell's `sleep`) is ours; PID 1 never is."""
    child = subprocess.Popen(
        ["sh", "-c", "sleep 600 & echo $!; wait"],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    grandchild = 0
    try:
        assert child.stdout is not None
        grandchild = int(child.stdout.readline())
        assert grandchild not in kill_guard._allowed  # only the walk can vouch for it
        assert kill_guard._descendant_of_us(grandchild) is True
        assert kill_guard._permitted(grandchild) is True
        assert kill_guard._descendant_of_us(1) is False
        assert kill_guard._permitted(1) is False
    finally:
        if grandchild:
            os.kill(grandchild, signal.SIGKILL)  # a descendant: the guard lets it through
        child.kill()
        child.wait(timeout=15)
        if child.stdout is not None:
            child.stdout.close()


def test_the_raw_group_flag_refuses_the_test_runners_own_group() -> None:
    """PIDFD_SIGNAL_PROCESS_GROUP on a pidfd of the test runner would signal its whole group."""
    calls: list[int] = []
    raw = kill_guard.guarded_raw(lambda number, *args: calls.append(number) or 0)
    me = os.getpid()
    if not gateway_proc.pidfd_supported():
        pytest.skip("no pidfd_open")
    fd = gateway_proc.pidfd_open(me)
    try:
        with pytest.raises(pytest.fail.Exception):
            raw(424, fd, signal.SIGKILL, 0, 4)  # the flag
    finally:
        os.close(fd)
    assert calls == []
    kill_guard.violations.clear()


def test_the_raw_group_flag_is_allowed_for_a_started_leader_in_its_own_group() -> None:
    if not gateway_proc.pidfd_supported():
        pytest.skip("no pidfd_open")
    calls: list[int] = []
    raw = kill_guard.guarded_raw(lambda number, *args: calls.append(number) or 0)
    proc = subprocess.Popen(["sleep", "30"], process_group=0)
    fd = gateway_proc.pidfd_open(proc.pid)
    try:
        raw(424, fd, signal.SIGKILL, 0, 4)
        assert calls == [424]
    finally:
        os.close(fd)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
