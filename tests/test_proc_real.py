"""Real processes on Linux: identity, liveness, pidfd and ctypes fallback signals, dumpability,
group probing, held-leader reaping. Children are tracked and killed by exact handle only."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time

import pytest

from jobsctl import gateway_proc
from jobsctl.proc_group import probe_group
from jobsctl.proc_host import LinuxHost, ProcParseError
from jobsctl.proc_identity import Identity, Liveness, check_identity, identify
from jobsctl.proc_linux import (
    get_dumpable,
    is_child_subreaper,
    reap_held_leader,
    set_child_subreaper,
    set_undumpable,
    wait_exited_keep,
)
from jobsctl.proc_signal import Result, signal_group, signal_process

from . import proc_real
from .proc_real import Spawner, spawner_fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc and pidfd tests")

HOST = LinuxHost()
spawner = pytest.fixture(spawner_fixture)


def wait_until(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_identity_of_real_child_and_boot_id(spawner: Spawner):
    child = spawner.spawn()
    ident = identify(HOST, child.pid)
    assert ident.pid == child.pid and ident.start > 0
    assert ident.boot_id == open("/proc/sys/kernel/random/boot_id").read().strip()
    assert check_identity(HOST, ident).alive


@pytest.mark.parametrize("name", ["a) (b c", "x\ny) 1 2 3 (", ") ("])
def test_hostile_comm_in_real_proc(spawner: Spawner, name: str):
    child = spawner.spawn(name=name)
    info = HOST.process_info(child.pid)
    assert info is not None
    assert info.state in ("S", "R")
    comm = open(f"/proc/{child.pid}/comm", "rb").read().rstrip(b"\n")
    assert b")" in comm  # the odd name really reached /proc
    ident = identify(HOST, child.pid)
    # start must equal the independently parsed field after the last ")"
    raw = open(f"/proc/{child.pid}/stat", "rb").read()
    assert ident.start == int(raw[raw.rindex(b")") + 1 :].split()[19])


def test_reaped_pid_is_not_alive(spawner: Spawner):
    child = spawner.spawn()
    ident = identify(HOST, child.pid)
    assert signal_process(HOST, ident, signal.SIGKILL).sent
    child.popen.wait(timeout=10)
    res = check_identity(HOST, ident)
    assert res.state is Liveness.DEAD and res.reason == "gone"
    # and a signal to the recorded identity is refused: nothing to hit
    assert not signal_process(HOST, ident, signal.SIGTERM).sent


def test_zombie_not_alive_but_reserved_leader_is(spawner: Spawner):
    child = spawner.spawn()
    ident = identify(HOST, child.pid)
    os.kill(child.pid, signal.SIGKILL)  # own unreaped child, exact PID
    wait_exited_keep(child.pid)
    info = HOST.process_info(child.pid)
    assert info is not None and info.zombie
    assert check_identity(HOST, ident).reason == "zombie"
    assert check_identity(HOST, ident, allow_zombie=True).alive


def test_wrong_start_is_never_signaled(spawner: Spawner):
    child = spawner.spawn()
    real = identify(HOST, child.pid)
    stale = Identity(real.boot_id, real.pid, real.start + 1)
    out = signal_process(HOST, stale, signal.SIGKILL)
    assert out.result is Result.NOT_ALIVE and out.detail == "reused"
    assert child.popen.poll() is None
    assert check_identity(HOST, real).alive


def test_sigterm_ignored_then_sigkill_via_pidfd(spawner: Spawner):
    child = spawner.spawn()
    ident = identify(HOST, child.pid)
    out = signal_process(HOST, ident, signal.SIGTERM)
    assert out.sent and out.method == "pidfd"
    time.sleep(0.3)
    assert check_identity(HOST, ident).alive  # the child ignores SIGTERM
    assert signal_process(HOST, ident, signal.SIGKILL).sent
    assert child.popen.wait(timeout=10) == -signal.SIGKILL


def test_signal_via_ctypes_fallback_when_interpreter_lacks_pidfd(
    spawner: Spawner, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    if not gateway_proc._fallback_allowed_here():  # pyright: ignore[reportPrivateUsage]
        pytest.skip("architecture not on the allow list")
    child = spawner.spawn()  # its pidfd comes through the ctypes syscall too
    ident = identify(HOST, child.pid)
    assert HOST.pidfd_supported()
    out = signal_process(HOST, ident, signal.SIGKILL)
    assert out.sent and out.method == "pidfd"
    assert child.popen.wait(timeout=10) == -signal.SIGKILL


def test_pidfd_pid_reports_the_process(spawner: Spawner):
    child = spawner.spawn()
    assert gateway_proc.pidfd_pid(child.pidfd) == child.pid  # type: ignore[arg-type]


def test_pidfd_open_of_reaped_pid_is_esrch(spawner: Spawner):
    child = spawner.spawn()
    pid = child.pid
    os.kill(pid, signal.SIGKILL)
    child.popen.wait(timeout=10)
    with pytest.raises(OSError) as ei:
        gateway_proc.pidfd_open(pid)
    assert ei.value.errno == errno.ESRCH


def test_unreadable_stat_means_unknown_not_dead(spawner: Spawner, tmp_path):
    # a /proc tree whose stat is unreadable gives UNKNOWN, and nothing is signaled
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    root = tmp_path / "proc"
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text("b1\n")
    (root / "77").mkdir()
    (root / "77" / "stat").write_bytes(b"")
    (root / "77" / "stat").chmod(0)
    res = check_identity(LinuxHost(root), Identity("b1", 77, 5))
    assert res.state is Liveness.UNKNOWN


# --- spawner teardown, thread-group leaders, thread ids, non-blocking wait -----------------

THREAD_EXIT_CHILD = (
    "import ctypes, threading, time\n"
    "threading.Thread(target=time.sleep, args=(600,)).start()\n"
    "print('ready', flush=True)\n"
    "ctypes.CDLL(None).pthread_exit(None)\n"
)
THREAD_ID_CHILD = (
    "import threading, time\n"
    "def f():\n"
    "    print(threading.get_native_id(), flush=True); time.sleep(600)\n"
    "threading.Thread(target=f).start(); time.sleep(600)\n"
)


def start_python(spawner: Spawner, code: str):
    popen = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env={"HOME": os.environ["HOME"], "PATH": os.environ.get("PATH", "")},
    )
    child = spawner.track(popen)
    assert popen.stdout is not None
    return child, popen.stdout.readline().split()


@pytest.mark.parametrize("group", [False, True])
def test_teardown_kills_and_reaps_when_pidfd_open_fails(
    group: bool, monkeypatch: pytest.MonkeyPatch
):
    sp = Spawner()

    def boom(pid: int) -> int:
        raise OSError(errno.EMFILE, "forced")

    monkeypatch.setattr(proc_real, "_open_pidfd", boom)
    with pytest.raises(OSError):
        sp.spawn(group=group, grandchildren=1)
    monkeypatch.undo()
    (child,) = sp.children
    assert child.pidfd is None and child.popen.poll() is None  # the SIGTERM-ignoring sleeper runs
    sp.teardown()
    assert child.popen.returncode == -signal.SIGKILL


def test_main_thread_exit_leader_is_alive_and_signalable(spawner: Spawner):
    child, line = start_python(spawner, THREAD_EXIT_CHILD)
    assert line == [b"ready"]
    ident = identify(HOST, child.pid)

    def fields() -> list[bytes]:
        raw = open(f"/proc/{child.pid}/stat", "rb").read()
        return raw[raw.rindex(b")") + 1 :].split()

    assert wait_until(lambda: fields()[0] == b"Z")  # the main thread is gone
    assert int(fields()[17]) == 2  # one thread still runs
    res = check_identity(HOST, ident)
    assert res.alive and res.info is not None and res.info.state == "Z"
    out = signal_process(HOST, ident, signal.SIGTERM)
    assert out.sent
    assert child.popen.wait(timeout=10) == -signal.SIGTERM


def test_identify_refuses_a_real_thread_id(spawner: Spawner):
    child, line = start_python(spawner, THREAD_ID_CHILD)
    tid = int(line[0])
    assert tid != child.pid and HOST.process_info(tid) is not None  # /proc/<tid>/stat parses
    with pytest.raises(ProcParseError):
        identify(HOST, tid)
    assert identify(HOST, child.pid).pid == child.pid


def test_wait_exited_keep_nohang_then_held_zombie(spawner: Spawner):
    child = spawner.spawn()
    assert wait_exited_keep(child.pid, nohang=True) is None
    os.kill(child.pid, signal.SIGKILL)  # own unreaped child, exact PID
    assert wait_until(lambda: wait_exited_keep(child.pid, nohang=True) is not None)
    info = HOST.process_info(child.pid)
    assert info is not None and info.state == "Z"  # still unreaped
    assert child.popen.returncode is None
    assert reap_held_leader(child.popen) == -signal.SIGKILL


# --- dumpability (spike C4) -----------------------------------------------------------------

DUMPABLE_CHILD = (
    "import sys, time\n"
    "from jobsctl.proc_linux import set_undumpable, get_dumpable\n"
    "set_undumpable(); print(get_dumpable(), flush=True); time.sleep(600)\n"
)


def test_undumpable_child_denies_environ_to_sibling(spawner: Spawner):
    if os.geteuid() == 0:
        pytest.skip("root bypasses the dumpable check")
    popen = subprocess.Popen(
        [sys.executable, "-I", "-c", DUMPABLE_CHILD],
        stdout=subprocess.PIPE,
        env={"HOME": os.environ["HOME"], "PYTHONPATH": ":".join(sys.path)},
    )
    from .proc_real import Child, _open_pidfd

    child = Child(popen, _open_pidfd(popen.pid), None)
    spawner.children.append(child)
    assert popen.stdout is not None
    assert popen.stdout.readline().strip() == b"0"
    with pytest.raises(PermissionError):
        open(f"/proc/{popen.pid}/environ", "rb").read()
    open(f"/proc/{popen.pid}/cmdline", "rb").read()  # cmdline stays readable


def test_dumpable_helpers_in_this_process_are_reversible():
    before = get_dumpable()
    try:
        assert set_undumpable() is True
        assert get_dumpable() == 0
    finally:
        gateway_proc.prctl(gateway_proc.PR_SET_DUMPABLE, before or 1)


def test_subreaper_flag_roundtrip_in_child(spawner: Spawner):
    code = (
        "from jobsctl.proc_linux import set_child_subreaper, is_child_subreaper\n"
        "assert is_child_subreaper() is False\n"
        "assert set_child_subreaper() is True\n"
        "assert is_child_subreaper() is True\n"
    )
    res = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env={"HOME": os.environ["HOME"], "PYTHONPATH": ":".join(sys.path)},
        capture_output=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr


def test_non_linux_helpers_are_noops(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert set_undumpable() is False
    assert set_child_subreaper() is False
    assert get_dumpable() is None and is_child_subreaper() is None


# --- group probing, reservation and reaping (spikes C2, C3) -------------------------------


def test_group_probe_sees_members_and_zombie_members_count_dead(spawner: Spawner):
    child = spawner.spawn(group=True, grandchildren=2)
    probe = probe_group(HOST, child.pid, exclude=[child.pid])
    assert sorted(m.pid for m in probe.live) == sorted(child.grandchildren)
    assert probe.complete
    # include the leader
    assert probe_group(HOST, child.pid).count == 3


def test_held_zombie_leader_keeps_group_probeable_and_signalable(spawner: Spawner):
    child = spawner.spawn(group=True, grandchildren=2)
    leader = identify(HOST, child.pid)
    os.kill(child.pid, signal.SIGKILL)  # our own unreaped child
    wait_exited_keep(child.pid)
    # leader is a zombie, held; the pgid is still reserved and probeable
    assert HOST.process_info(child.pid).zombie  # type: ignore[union-attr]
    probe = probe_group(HOST, child.pid, exclude=[child.pid])
    assert sorted(m.pid for m in probe.live) == sorted(child.grandchildren)
    # SIGTERM-ignoring members survive a group SIGTERM; SIGKILL ends them
    assert signal_group(HOST, leader, signal.SIGTERM).sent
    time.sleep(0.3)
    assert probe_group(HOST, child.pid, exclude=[child.pid]).count == 2
    assert signal_group(HOST, leader, signal.SIGKILL).sent
    assert wait_until(lambda: probe_group(HOST, child.pid, exclude=[child.pid]).count == 0)
    # members were reparented and reaped by init; zombies there count as dead anyway
    # now reap the leader and fix Popen's view
    assert child.popen.returncode is None
    assert reap_held_leader(child.popen) == -signal.SIGKILL
    assert child.popen.returncode == -signal.SIGKILL
    assert child.popen.wait(timeout=5) == -signal.SIGKILL  # no ECHILD
    assert child.popen.poll() == -signal.SIGKILL
    assert reap_held_leader(child.popen) == -signal.SIGKILL  # idempotent


def test_group_signal_refused_after_leader_reaped(spawner: Spawner):
    child = spawner.spawn(group=True, grandchildren=1)
    leader = identify(HOST, child.pid)
    os.kill(child.pid, signal.SIGKILL)
    child.popen.wait(timeout=10)
    out = signal_group(HOST, leader, signal.SIGKILL)
    assert out.result is Result.NOT_ALIVE and out.detail == "gone"
    # the grandchild is untouched; teardown kills it by its recorded pidfd
    assert gateway_proc.pidfd_pid(child.grand_fds[0]) == child.grandchildren[0]


def test_group_signal_refused_for_non_leader(spawner: Spawner):
    child = spawner.spawn()  # same group as pytest, not its own leader
    ident = identify(HOST, child.pid)
    assert HOST.process_info(child.pid).pgid != child.pid  # type: ignore[union-attr]
    out = signal_group(HOST, ident, signal.SIGKILL)
    # no group is numbered by this PID: nothing to signal
    assert out.result is Result.NOT_ALIVE and out.detail == "gone"
    assert child.popen.poll() is None


def test_group_pidfd_flag_works_or_falls_back_on_this_kernel(spawner: Spawner):
    child = spawner.spawn(group=True)
    ident = identify(HOST, child.pid)
    out = signal_group(HOST, ident, signal.SIGKILL, use_pidfd=True)
    assert out.sent and out.method in ("pidfd-group", "killpg")  # 6.9+ vs older kernels
    assert child.popen.wait(timeout=10) == -signal.SIGKILL


def test_reap_held_leader_when_already_reaped_elsewhere(spawner: Spawner):
    child = spawner.spawn()
    os.kill(child.pid, signal.SIGKILL)
    os.waitpid(child.pid, 0)  # reaped behind Popen's back
    assert reap_held_leader(child.popen) is None
    child.popen.returncode = -9  # let teardown skip it
