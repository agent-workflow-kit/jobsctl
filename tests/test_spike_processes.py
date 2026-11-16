"""Platform probes for spikes A and C (design.md, Live verification): processes, signals, /proc.

These test the host kernel and Python, not jobsctl. They exist to fail when a platform change
breaks something the design relies on (pidfd semantics, process groups, zombie-leader reaping,
SIGPIPE, dumpability). The traceability map from every spike item to its tests is in
design.md, "Regression coverage". Each test names its item (a1, c2, ...).
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import select
import signal
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.kill_guard import guarded_pidfd_send
from tests.spike_helpers import (
    PY,
    Procs,
    alive,
    linux_only,
    read_line,
    start_time,
    stat_fields,
    wait_for,
)

pytestmark = linux_only

LIBC = ctypes.CDLL(None, use_errno=True)
SYS_PIDFD_OPEN, SYS_PIDFD_SEND_SIGNAL = 434, 424


def py(code: str) -> list[str]:
    return [*PY, "-c", textwrap.dedent(code)]


def kernel() -> tuple[int, int]:
    match = re.match(r"(\d+)\.(\d+)", os.uname().release)
    assert match is not None, os.uname().release
    return int(match[1]), int(match[2])


# -- A1: detach mechanics ------------------------------------------------------------------

A1_RUNNER = """
    import os, signal, subprocess, sys, time
    signal.signal(signal.SIGHUP, signal.SIG_DFL)  # a launcher may have left SIGHUP ignored
    rfd, wfd = os.pipe()
    os.set_inheritable(rfd, True)
    os.set_inheritable(wfd, True)
    probe = (
        "import os, sys, time\\n"
        "try:\\n"
        "    os.fstat(int(sys.argv[2])); state = 'open'\\n"
        "except OSError:\\n"
        "    state = 'closed'\\n"
        "open(sys.argv[1], 'w').write(state)\\n"
        "time.sleep(600)\\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", probe, sys.argv[1], str(rfd)],
        start_new_session=True, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(child.pid, flush=True)
    time.sleep(600)
"""


def test_a1_popen_detach_survives_the_runner_hangup_and_closes_inherited_fds(
    spike_procs: Procs, tmp_path: Path
) -> None:
    marker = tmp_path / "fdstate"
    runner = spike_procs.popen(
        py(A1_RUNNER) + [str(marker)], stdout=subprocess.PIPE, start_new_session=True
    )
    child = spike_procs.adopt(int(read_line(runner)))
    assert wait_for(marker.exists)
    assert wait_for(lambda: marker.read_text() != "")
    assert marker.read_text() == "closed"  # close_fds=True: a held descriptor does not leak
    os.killpg(runner.pid, signal.SIGHUP)  # hangup to the runner's whole group
    assert runner.wait(timeout=15) == -signal.SIGHUP
    assert wait_for(lambda: not alive(runner.pid))
    assert start_time(child) is not None and alive(child)  # the detached child is in no part of it
    fields = stat_fields(child)
    assert fields is not None and int(fields[2]) == child  # own session, own group (new session)


# -- A3: /proc start time ---------------------------------------------------------------------


def test_a3_proc_start_time_is_stable_survives_zombies_and_parses_odd_names(
    spike_procs: Procs,
) -> None:
    code = """
        import ctypes, sys, time
        ctypes.CDLL(None).prctl(15, b"a) (b 1 2", 0, 0, 0)  # PR_SET_NAME
        print("ready", flush=True)
        time.sleep(600)
    """
    p = spike_procs.popen(py(code), stdout=subprocess.PIPE)
    assert read_line(p) == "ready"
    first = start_time(p.pid)
    assert first is not None and first.isdigit()
    raw = Path(f"/proc/{p.pid}/stat").read_bytes()
    assert b"a) (b 1 2" in raw
    naive = raw.split()  # a naive split sees the name as several fields
    assert naive[21].decode() != first  # field 22 (index 21) lands elsewhere
    assert start_time(p.pid) == first  # stable between reads
    os.kill(p.pid, signal.SIGKILL)
    assert wait_for(lambda: (f := stat_fields(p.pid)) is not None and f[0] == "Z")
    assert start_time(p.pid) == first  # a zombie keeps it
    p.wait(timeout=15)
    with pytest.raises(FileNotFoundError):  # a reaped PID is gone
        Path(f"/proc/{p.pid}/stat").read_bytes()


# -- A4: signal chain, subreaper --------------------------------------------------------------


def test_a4_sigterm_to_a_shell_leaves_its_children_and_the_group_signal_stops_them(
    spike_procs: Procs,
) -> None:
    sh = spike_procs.popen(
        ["/bin/sh", "-c", "sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    sleeper = spike_procs.adopt(int(read_line(sh)))
    os.kill(sh.pid, signal.SIGTERM)
    assert sh.wait(timeout=15) == -signal.SIGTERM
    assert alive(sleeper)  # orphaned, still running
    fields = stat_fields(sleeper)
    assert fields is not None and int(fields[2]) == sh.pid  # still in the job's own group
    os.killpg(sh.pid, signal.SIGTERM)
    assert wait_for(lambda: not alive(sleeper))


def test_a4_child_subreaper_adopts_an_orphan_that_left_its_group_unprivileged(
    spike_procs: Procs,
) -> None:
    code = """
        import ctypes, os, time
        libc = ctypes.CDLL(None)
        assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
        flag = ctypes.c_int(0)
        assert libc.prctl(37, ctypes.byref(flag), 0, 0, 0) == 0 and flag.value == 1
        r, w = os.pipe()
        mid = os.fork()
        if mid == 0:
            if os.fork() == 0:
                os.setsid()  # lost its group and session
                os.write(w, b"%d\\n" % os.getpid())
                time.sleep(600)
                os._exit(0)
            os._exit(0)
        os.waitpid(mid, 0)
        gpid = int(os.fdopen(r).readline())
        ppid = open("/proc/%d/stat" % gpid, "rb").read().rsplit(b")", 1)[1].split()[1]
        print(gpid, int(ppid) == os.getpid(), flush=True)
        time.sleep(600)
    """
    p = spike_procs.popen(py(code), stdout=subprocess.PIPE)
    gpid, adopted = read_line(p).split()
    spike_procs.adopt(int(gpid))
    assert adopted == "True"


# -- A5: shim death ------------------------------------------------------------------------------


def test_a5_the_job_survives_sigkill_of_its_parent_no_hangup_no_pdeathsig(
    spike_procs: Procs,
) -> None:
    shim_code = """
        import subprocess, time
        job = subprocess.Popen(["sleep", "60"], process_group=0)
        print(job.pid, flush=True)
        time.sleep(600)
    """
    shim = spike_procs.popen(py(shim_code), stdout=subprocess.PIPE, start_new_session=True)
    job = spike_procs.adopt(int(read_line(shim)))
    os.kill(shim.pid, signal.SIGKILL)
    assert shim.wait(timeout=15) == -signal.SIGKILL
    assert wait_for(lambda: not alive(shim.pid))
    # PDEATHSIG would have ended it within about 50 ms; give a generous margin.
    assert not wait_for(lambda: not alive(job), timeout=0.6)
    assert alive(job)


# -- A6: exit codes ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 13, 255])
def test_a6_exit_codes_pass_through(code: int) -> None:
    assert subprocess.run(["/bin/sh", "-c", f"exit {code}"], timeout=30).returncode == code


def test_a6_signal_deaths_are_negative_and_a_shell_reports_143(spike_procs: Procs) -> None:
    p = spike_procs.popen(["sleep", "60"])
    os.kill(p.pid, signal.SIGTERM)
    assert p.wait(timeout=15) == -signal.SIGTERM
    sh = spike_procs.popen(
        ["/bin/sh", "-c", "sleep 60 & kill -TERM $!; wait $!; echo $?"], stdout=subprocess.PIPE
    )
    assert read_line(sh) == "143"
    assert sh.wait(timeout=15) == 0


def test_a6_a_missing_program_raises_before_anything_starts(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        subprocess.Popen([str(tmp_path / "no-such-program")])
    not_exec = tmp_path / "plain"
    not_exec.write_text("x")
    with pytest.raises(PermissionError):
        subprocess.Popen([str(not_exec)])


# -- A7: stdin ---------------------------------------------------------------------------------


def test_a7_dev_null_stdin_is_a_clean_eof() -> None:
    res = subprocess.run(
        [*PY, "-c", "import sys; print(repr(sys.stdin.read()))"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert res.stdout.strip() == b"''" and res.returncode == 0


A7_CLOSED_FD_0 = """
    import errno, os
    os.close(0)  # what a program sees when its stdin was closed instead of given /dev/null
    try:
        os.read(0, 1)
    except OSError as exc:
        print(errno.errorcode[exc.errno])
    else:
        print("read-succeeded")
"""


def test_a7_a_closed_fd_0_breaks_programs() -> None:
    # Python child, not sh/cat: how a given cat or shell reacts to a closed fd 0 varies by
    # implementation (some reopen /dev/null or print nothing); the kernel's EBADF does not.
    closed = subprocess.run(
        py(A7_CLOSED_FD_0), stdin=subprocess.DEVNULL, capture_output=True, timeout=30
    )
    assert closed.stdout.strip() == b"EBADF" and closed.returncode == 0, closed.stderr
    null = subprocess.run(
        py("import os; print(len(os.read(0, 1)))"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert null.stdout.strip() == b"0" and null.returncode == 0  # /dev/null: end of file


def test_a7_a_terminal_stdin_hands_the_keystrokes_to_the_program() -> None:
    try:
        master, slave = os.openpty()
    except OSError as exc:
        pytest.skip(f"no pseudo-terminal available here: {exc}")
    try:
        os.write(master, b"k\n")
        res = subprocess.run(
            py("import os; print(os.read(0, 2))"), stdin=slave, capture_output=True, timeout=30
        )
        assert res.stdout.strip() == b"b'k\\n'"  # why a detached job must not inherit it
    finally:
        os.close(master)
        os.close(slave)


# -- C1: pidfd --------------------------------------------------------------------------------


def raw_pidfd_open(pid: int) -> int:
    fd = LIBC.syscall(ctypes.c_long(SYS_PIDFD_OPEN), ctypes.c_long(pid), ctypes.c_long(0))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(fd)


def raw_pidfd_send(fd: int, sig: int, flags: int = 0) -> None:
    if sig != 0:  # the same target check as every other signal a test sends
        guarded_pidfd_send(lambda *a, **k: None)(fd, sig)
    res = LIBC.syscall(
        ctypes.c_long(SYS_PIDFD_SEND_SIGNAL),
        ctypes.c_long(fd),
        ctypes.c_long(sig),
        ctypes.c_long(0),
        ctypes.c_long(flags),
    )
    if res < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def readable(fd: int, timeout: float) -> bool:
    return bool(select.select([fd], [], [], timeout)[0])


def test_c1_pidfd_open_check_and_signal_work_for_a_child_and_become_readable_on_exit(
    spike_procs: Procs,
) -> None:
    p = spike_procs.popen(["sleep", "60"])
    before = start_time(p.pid)
    fd = raw_pidfd_open(p.pid)
    try:
        assert start_time(p.pid) == before  # the identity check after the open
        raw_pidfd_send(fd, 0)
        assert not readable(fd, 0.1)  # alive: not readable
        raw_pidfd_send(fd, signal.SIGKILL)
        assert readable(fd, 15)  # readable once the process exits (before it is reaped)
        p.wait(timeout=15)
        with pytest.raises(OSError) as info:  # a reaped target
            raw_pidfd_send(fd, signal.SIGTERM)
        assert info.value.errno == errno.ESRCH
    finally:
        os.close(fd)
    with pytest.raises(OSError) as gone:
        raw_pidfd_open(p.pid)
    assert gone.value.errno == errno.ESRCH


def test_c1_pidfd_signals_a_non_child(spike_procs: Procs) -> None:
    code = """
        import subprocess, time
        print(subprocess.Popen(["sleep", "60"]).pid, flush=True)
        time.sleep(600)
    """
    parent = spike_procs.popen(py(code), stdout=subprocess.PIPE)
    grand = spike_procs.adopt(int(read_line(parent)))
    fd = raw_pidfd_open(grand)
    try:
        assert start_time(grand) is not None
        raw_pidfd_send(fd, 0)
        raw_pidfd_send(fd, signal.SIGKILL)
        assert readable(fd, 15)
    finally:
        os.close(fd)


@pytest.mark.skipif(
    not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")),
    reason="this CPython build lacks os.pidfd_open or signal.pidfd_send_signal (C1: the raw "
    "syscalls above are the fallback)",
)
def test_c1_the_stdlib_pidfd_agrees_with_the_raw_syscall(spike_procs: Procs) -> None:
    p = spike_procs.popen(["sleep", "60"])
    fd = os.pidfd_open(p.pid)
    try:
        signal.pidfd_send_signal(fd, 0)
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        assert readable(fd, 15)
    finally:
        os.close(fd)


def test_c1_pidfd_signal_flags_are_einval_before_linux_6_9(spike_procs: Procs) -> None:
    p = spike_procs.popen(["sleep", "60"], process_group=0)
    fd = raw_pidfd_open(p.pid)
    try:
        if kernel() < (6, 9):
            for flags in (1, 2, 4):  # THREAD, THREAD_GROUP, PROCESS_GROUP
                with pytest.raises(OSError) as info:
                    raw_pidfd_send(fd, 0, flags)
                assert info.value.errno == errno.EINVAL, flags
        else:
            raw_pidfd_send(fd, 0, 4)  # PIDFD_SIGNAL_PROCESS_GROUP exists from 6.9
    finally:
        os.close(fd)


# -- C2: zombie-leader reservation ------------------------------------------------------------


def members_of(pgid: int) -> list[tuple[int, str]]:
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit() and (f := stat_fields(int(name))) is not None and int(f[2]) == pgid:
            found.append((int(name), f[0]))
    return found


def test_c2_a_held_zombie_leader_keeps_its_group_signalable_and_the_scan_finds_members(
    spike_procs: Procs,
) -> None:
    code = """
        import subprocess, time
        kids = [subprocess.Popen(["sleep", "60"]) for _ in range(3)]
        print(*[k.pid for k in kids], flush=True)
        time.sleep(600)
    """
    leader = spike_procs.popen(py(code), stdout=subprocess.PIPE, process_group=0)
    kids = [spike_procs.adopt(int(x)) for x in read_line(leader).split()]
    os.kill(leader.pid, signal.SIGKILL)
    assert wait_for(lambda: (f := stat_fields(leader.pid)) is not None and f[0] == "Z")
    info = os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    assert info is not None and info.si_status == signal.SIGKILL  # exited: ready at once
    fields = stat_fields(leader.pid)
    assert fields is not None and fields[0] == "Z"  # still a zombie: held, not reaped
    live = [pid for pid, state in members_of(leader.pid) if state != "Z" and pid != leader.pid]
    assert sorted(live) == sorted(kids)  # the /proc scan by pgrp finds the 3 members
    os.killpg(leader.pid, signal.SIGKILL)  # reaches members reparented away from the leader
    assert wait_for(lambda: not any(alive(k) for k in kids))
    assert os.waitpid(leader.pid, os.WNOHANG)[0] == leader.pid  # the zombie is ready
    assert leader.returncode is None  # Popen does not see a manual reap


def test_c2_the_group_id_is_reserved_until_the_leader_is_reaped(spike_procs: Procs) -> None:
    leader = spike_procs.popen(["sleep", "60"], process_group=0)
    os.kill(leader.pid, signal.SIGKILL)
    assert wait_for(lambda: (f := stat_fields(leader.pid)) is not None and f[0] == "Z")
    held = os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    assert held is not None  # exited, left unreaped
    os.killpg(leader.pid, 0)  # the zombie leader still reserves the group
    assert os.waitpid(leader.pid, os.WNOHANG)[0] == leader.pid
    with pytest.raises(ProcessLookupError):  # gone only after the reap
        os.killpg(leader.pid, 0)


# -- C3: group SIGTERM / SIGKILL with SIGTERM-ignoring members ---------------------------------

C3_SPAWN = "sleep 60 & echo $!; sleep 60 & echo $!; sleep 60 & echo $!; sleep 60 & echo $!; "
C3_SCRIPT = "trap '' TERM; " + C3_SPAWN + "echo ready; wait"
C3_PLAIN_SCRIPT = C3_SPAWN + "echo ready; wait"


def test_c3_a_term_ignoring_shell_passes_the_ignore_to_its_children_so_only_sigkill_ends_them(
    spike_procs: Procs,
) -> None:
    leader = spike_procs.popen(
        ["/bin/sh", "-c", C3_SCRIPT], stdout=subprocess.PIPE, process_group=0
    )
    kids = [spike_procs.adopt(int(read_line(leader))) for _ in range(4)]
    assert read_line(leader) == "ready"
    for pid in kids:  # SigIgn has the SIGTERM bit (bit 14) set: inherited across exec
        status = Path(f"/proc/{pid}/status").read_text()
        mask = int(next(ln for ln in status.splitlines() if ln.startswith("SigIgn")).split()[1], 16)
        assert mask & (1 << (signal.SIGTERM - 1)), pid
    os.killpg(leader.pid, signal.SIGTERM)
    assert not wait_for(lambda: not all(alive(p) for p in [leader.pid, *kids]), timeout=0.6)
    os.killpg(leader.pid, signal.SIGKILL)  # leader unreaped: the group id is reserved
    assert wait_for(lambda: not any(alive(p) for p in kids))
    assert leader.wait(timeout=15) == -signal.SIGKILL


def test_c3_without_the_ignore_sigterm_to_the_group_ends_every_member(
    spike_procs: Procs,
) -> None:
    leader = spike_procs.popen(
        ["/bin/sh", "-c", C3_PLAIN_SCRIPT], stdout=subprocess.PIPE, process_group=0
    )
    kids = [spike_procs.adopt(int(read_line(leader))) for _ in range(4)]
    assert read_line(leader) == "ready"
    os.killpg(leader.pid, signal.SIGTERM)  # the leader stays unreaped: the group id is reserved
    assert wait_for(lambda: not any(alive(p) for p in [leader.pid, *kids]))
    assert [pid for pid, state in members_of(leader.pid) if state != "Z"] == []  # group gone
    assert leader.wait(timeout=15) == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.killpg(leader.pid, 0)


# -- C4: PR_SET_DUMPABLE -------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the dumpable check")
def test_c4_undumpable_denies_environ_to_a_same_user_process_until_exec(spike_procs: Procs) -> None:
    code = """
        import ctypes, os, subprocess, time
        assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
        forked = os.fork()
        if forked == 0:
            time.sleep(600)
            os._exit(0)
        execd = subprocess.Popen(["sleep", "60"])
        print(os.getpid(), forked, execd.pid, flush=True)
        time.sleep(600)
    """
    p = spike_procs.popen(py(code), stdout=subprocess.PIPE)
    me, forked, execd = (
        spike_procs.adopt(int(x)) if int(x) != p.pid else p.pid for x in read_line(p).split()
    )
    for pid in (me, forked):  # the process itself and a fork-only child
        with pytest.raises(PermissionError):
            Path(f"/proc/{pid}/environ").read_bytes()
    Path(f"/proc/{execd}/environ").read_bytes()  # reset on exec: readable again
    assert Path(f"/proc/{me}/cmdline").read_bytes()  # cmdline stays readable
