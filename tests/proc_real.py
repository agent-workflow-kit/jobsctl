"""Real-process fixture: every child is recorded at launch and killed by that exact handle.

Never kills by pattern. Each child holds a pidfd opened right after the spawn (while the
child is still our unreaped child, so its PID cannot have been reused). Teardown signals
SIGKILL through that pidfd, and to the recorded process group only while the leader is still
unreaped (so the pgid is reserved), then reaps by exact PID.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from jobsctl import gateway_proc

# A child that ignores SIGTERM, optionally renames itself (prctl PR_SET_NAME) and optionally
# starts N grandchildren that also ignore SIGTERM. Prints "ready <grandchild pids...>".
CHILD = textwrap.dedent(
    """
    import ctypes, os, signal, subprocess, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    name = sys.argv[1].encode("utf-8", "surrogateescape") if sys.argv[1] else b""
    if name:
        ctypes.CDLL(None).prctl(15, name, 0, 0, 0)
    kids = []
    for _ in range(int(sys.argv[2])):
        k = subprocess.Popen([sys.executable, "-I", "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('up', flush=True); time.sleep(600)"], stdout=subprocess.PIPE)
        k.stdout.readline()
        kids.append(k.pid)
    print("ready", *kids, flush=True)
    time.sleep(600)
    """
)


@dataclass
class Child:
    popen: subprocess.Popen[bytes]
    pidfd: int | None
    pgid: int | None
    grandchildren: list[int] = field(default_factory=list)
    grand_fds: list[int] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.popen.pid


class Spawner:
    def __init__(self) -> None:
        self.children: list[Child] = []

    def spawn(self, *, name: str = "", grandchildren: int = 0, group: bool = False) -> Child:
        popen = subprocess.Popen(
            [sys.executable, "-I", "-c", CHILD, name, str(grandchildren)],
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            process_group=0 if group else None,
            env={"HOME": os.environ["HOME"], "PATH": os.environ.get("PATH", "")},
        )
        fd = _open_pidfd(popen.pid)
        child = Child(popen, fd, popen.pid if group else None)
        self.children.append(child)
        assert popen.stdout is not None
        line = popen.stdout.readline().split()
        assert line and line[0] == b"ready", line
        child.grandchildren = [int(p) for p in line[1:]]
        # grandchildren are not our children: open pidfds now, while we hold the leader
        for gpid in child.grandchildren:
            child.grand_fds.append(_open_pidfd(gpid))
        return child

    def teardown(self) -> None:
        for child in self.children:
            _kill_child(child)


def _open_pidfd(pid: int) -> int:
    return gateway_proc.pidfd_open(pid)


def _kill_child(child: Child) -> None:
    for fd in child.grand_fds:
        try:
            gateway_proc.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass  # already gone
        os.close(fd)
    if child.popen.returncode is None and child.pidfd is not None:
        try:
            gateway_proc.pidfd_send_signal(child.pidfd, signal.SIGKILL)
        except OSError:
            pass
    if child.popen.returncode is None:
        try:
            child.popen.wait(timeout=10)  # reap by exact PID via the handle
        except subprocess.TimeoutExpired:  # pragma: no cover
            pytest.fail(f"child {child.pid} survived SIGKILL")
    if child.popen.stdout is not None:
        child.popen.stdout.close()
    if child.pidfd is not None:
        os.close(child.pidfd)


def spawner_fixture() -> Iterator[Spawner]:
    sp = Spawner()
    try:
        yield sp
    finally:
        sp.teardown()
