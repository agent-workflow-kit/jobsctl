"""Real-process fixture: every child is recorded at launch and killed by that exact handle.

Never kills by pattern. A child is recorded the moment it is spawned, then gets a pidfd (while
it is still our unreaped child, so its PID cannot have been reused); grandchildren are recorded
as the child announces them. Teardown, even after a partial failure, signals SIGKILL to the
group the fixture created (``group=True``) only while the leader is unreaped (so the pgid is
reserved), then through each recorded pidfd (``Popen.kill`` if the child has none), reaps the
child by exact PID and closes every fd.
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
# starts N grandchildren that also ignore SIGTERM. Prints "kid <pid>" per grandchild, then "ready".
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
        print("kid", k.pid, flush=True)
        k.stdout.readline()
        kids.append(k.pid)
    print("ready", flush=True)
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
        child = self.track(popen, group=group)
        assert popen.stdout is not None
        while True:
            line = popen.stdout.readline().split()
            assert line, "child exited before it was ready"
            if line[0] == b"ready":
                return child
            assert line[0] == b"kid", line
            # grandchildren are not our children: record and open a pidfd right away, while
            # we hold the leader
            gpid = int(line[1])
            child.grandchildren.append(gpid)
            child.grand_fds.append(_open_pidfd(gpid))

    def track(self, popen: subprocess.Popen[bytes], *, group: bool = False) -> Child:
        """Record ``popen`` for teardown at once, then open its pidfd."""
        child = Child(popen, None, popen.pid if group else None)
        self.children.append(child)
        child.pidfd = _open_pidfd(popen.pid)
        return child

    def teardown(self) -> None:
        for child in self.children:
            _kill_child(child)


def _open_pidfd(pid: int) -> int:
    return gateway_proc.pidfd_open(pid)


def _kill_child(child: Child) -> None:
    if child.popen.returncode is None and child.pgid is not None:
        try:  # the group this fixture created; the leader is unreaped, so the pgid is ours
            gateway_proc.killpg(child.pgid, signal.SIGKILL)
        except OSError:
            pass
    for fd in child.grand_fds:
        try:
            gateway_proc.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass  # already gone
        os.close(fd)
    if child.popen.returncode is None:
        try:
            if child.pidfd is not None:
                gateway_proc.pidfd_send_signal(child.pidfd, signal.SIGKILL)
            else:
                child.popen.kill()  # unreaped child: its PID cannot have been reused
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
