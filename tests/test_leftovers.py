"""Counting leftovers (interface.md, Leftovers; spike C2) with a fake host, then real processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from jobsctl import gateway_proc
from jobsctl.leftovers import Leftovers, count_leftovers
from jobsctl.proc_host import LinuxHost
from tests.proc_fakes import FakeHost, proc

SHIM = 100
LEADER = 200


def host_with(*procs: object, hidden: bool = False) -> FakeHost:
    return FakeHost(procs={p.pid: p for p in procs}, hidden=hidden)  # type: ignore[attr-defined]


def test_no_other_process_means_zero_and_complete() -> None:
    host = host_with(proc(LEADER, pgid=LEADER, ppid=SHIM, zombie=True))
    got = count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got == Leftovers((), True) and got.recorded == 0


def test_group_members_other_than_the_leader_count() -> None:
    host = host_with(
        proc(LEADER, pgid=LEADER, ppid=SHIM, zombie=True),
        proc(201, pgid=LEADER, ppid=1),
        proc(202, pgid=LEADER, ppid=1),
    )
    got = count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got.pids == (201, 202) and got.count == 2


def test_zombie_group_members_do_not_count() -> None:
    host = host_with(proc(201, pgid=LEADER, ppid=1, zombie=True))
    assert count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM).count == 0


def test_adopted_orphans_that_left_the_group_count_once() -> None:
    host = host_with(
        proc(201, pgid=LEADER, ppid=SHIM),  # in the group and adopted: one process
        proc(300, pgid=300, ppid=SHIM),  # setsid daemon adopted by the shim
    )
    got = count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got.pids == (201, 300)


def test_adopted_zombies_are_reaped_by_exact_pid_and_not_counted() -> None:
    host = host_with(proc(300, pgid=300, ppid=SHIM, zombie=True), proc(301, pgid=301, ppid=SHIM))
    reaped: list[int] = []
    got = count_leftovers(
        host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM, reap=lambda p: reaped.append(p) or True
    )
    assert reaped == [300] and got.pids == (301,)


def test_the_leader_and_excluded_pids_never_count_or_get_reaped() -> None:
    host = host_with(
        proc(LEADER, pgid=LEADER, ppid=SHIM, zombie=True),
        proc(400, pgid=400, ppid=SHIM),  # a hook leader
        proc(401, pgid=401, ppid=SHIM, zombie=True),  # an exited hook leader, reaped elsewhere
    )
    reaped: list[int] = []
    got = count_leftovers(
        host,
        leader_pid=LEADER,
        pgid=LEADER,
        own_pid=SHIM,
        exclude=[400, 401],
        reap=lambda p: reaped.append(p) or True,
    )
    assert got.count == 0 and reaped == []


def test_other_processes_are_not_ours_to_count() -> None:
    host = host_with(proc(500, pgid=500, ppid=1), proc(501, pgid=501, ppid=999))
    assert count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM).count == 0


def test_an_incomplete_scan_is_a_lower_bound() -> None:
    host = host_with(proc(201, pgid=LEADER, ppid=1), hidden=True)
    got = count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got.count == 1 and not got.complete and got.recorded == 1


def test_an_empty_incomplete_scan_records_zero_as_a_lower_bound() -> None:
    got = count_leftovers(host_with(hidden=True), leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got.count == 0 and not got.complete
    assert got.recorded == 0 and type(got.recorded) is int


def test_a_complete_scan_records_its_count() -> None:
    host = host_with(proc(201, pgid=LEADER, ppid=1), proc(300, pgid=300, ppid=SHIM))
    got = count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got.complete and got.recorded == 2


def test_an_unreadable_entry_makes_the_scan_incomplete_and_the_found_pids_still_count() -> None:
    host = FakeHost(
        procs={201: proc(201, pgid=LEADER, ppid=1)}, info_errors={999: PermissionError("x")}
    )
    got = count_leftovers(host, leader_pid=LEADER, pgid=LEADER, own_pid=SHIM)
    assert got.recorded == 1


def test_the_unknown_exit_kind_keeps_null_for_an_empty_incomplete_scan() -> None:
    """`_kill` and `job_gone` finalize exit kind `unknown`, where null means "not proven"."""
    empty = Leftovers((), False)
    assert empty.recorded_unless_unproven is None
    assert Leftovers((7,), False).recorded_unless_unproven == 1
    assert Leftovers((), True).recorded_unless_unproven == 0


def test_real_processes_group_members_and_a_setsid_orphan_are_counted_and_not_signaled() -> None:
    """A real subreaper: this test process adopts a daemonized grandchild."""
    gateway_proc.prctl(gateway_proc.PR_SET_CHILD_SUBREAPER, 1)
    code = (
        "import os, subprocess, sys\n"
        "subprocess.Popen(['sleep', '60'])\n"  # stays in the leader's group
        "subprocess.Popen(['sleep', '60'], start_new_session=True)\n"  # leaves the group
        "print('up', flush=True)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        process_group=0,
    )
    assert leader.stdout is not None
    leader.stdout.readline()
    os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)
    pids: tuple[int, ...] = ()
    try:
        got = count_leftovers(
            LinuxHost(), leader_pid=leader.pid, pgid=leader.pid, own_pid=os.getpid()
        )
        pids = got.pids
        assert got.count == 2
        for pid in got.pids:
            os.kill(pid, 0)  # still alive: counted, never signaled
    finally:
        # teardown: exactly the recorded group (leader unreaped, pgid reserved) and the orphan
        os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=10)
        leader.stdout.close()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 10
        for pid in pids:
            while time.monotonic() < deadline and not gateway_proc.reap_if_exited(pid):
                time.sleep(0.02)
        gateway_proc.prctl(gateway_proc.PR_SET_CHILD_SUBREAPER, 0)
