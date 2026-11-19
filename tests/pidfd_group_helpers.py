"""A real job-shaped process group for the PIDFD_SIGNAL_PROCESS_GROUP tests (#35).

A leader in its own process group with one member (``sleep``) in that group. Every PID is
recorded with its identity when it is started or announced; teardown signals exactly those,
and only while the recorded identity still holds. Nothing is ever killed by pattern.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Identity, Liveness, check_identity, identify
from tests.kill_guard import allow

SCRIPT = (
    "import subprocess, time\n"
    "m = subprocess.Popen(['sleep', '600'])\n"
    "print(m.pid, flush=True)\n"
    "time.sleep(600)\n"
)


def kernel_at_least(major: int, minor: int) -> bool:
    if sys.platform != "linux":
        return False
    parts = platform.release().split(".")
    try:
        return (int(parts[0]), int("".join(c for c in parts[1] if c.isdigit()))) >= (major, minor)
    except (ValueError, IndexError):
        return False


@dataclass
class Group:
    leader_proc: subprocess.Popen[str]
    leader: Identity
    member: Identity
    _tracked: list[Identity] = field(default_factory=list)

    def alive(self, ident: Identity, *, allow_zombie: bool = False) -> bool:
        res = check_identity(default_host(), ident, allow_zombie=allow_zombie)
        return res.state is Liveness.ALIVE

    def wait_gone(self, ident: Identity, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive(ident):
                return True
            time.sleep(0.02)
        return False

    def wait_zombie(self, timeout: float = 30.0) -> bool:
        host = default_host()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = host.process_info(self.leader.pid)
            if info is not None and info.zombie:
                return True
            time.sleep(0.02)
        return False

    def cleanup(self) -> None:
        for ident in [self.member, self.leader]:
            if self.alive(ident, allow_zombie=True):
                try:
                    os.kill(ident.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            self.leader_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        if self.leader_proc.stdout is not None:
            self.leader_proc.stdout.close()


def start_group() -> Group:
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
        process_group=0,
    )
    allow(proc.pid)
    assert proc.stdout is not None
    host = default_host()
    leader = identify(host, proc.pid)
    try:
        line = proc.stdout.readline()
        member_pid = int(line.split()[0])
    except Exception:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    allow(member_pid)
    return Group(proc, leader, identify(host, member_pid))


@pytest.fixture
def group() -> Iterator[Group]:
    g = start_group()
    try:
        yield g
    finally:
        g.cleanup()
