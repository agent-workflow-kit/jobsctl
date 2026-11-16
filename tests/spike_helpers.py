"""Helpers for the platform probes in test_spike_*.py (no jobsctl code: they probe the host).

Every process a probe starts is recorded at launch and killed at teardown by that exact PID,
only while its recorded start time still matches (so a reused PID is never signaled). Nothing is
killed by pattern.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator

import pytest

from tests.kill_guard import allow

PY = [sys.executable, "-I"]

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux-only platform behavior (/proc, pidfd, prctl); the macOS checks are #28",
)


def stat_fields(pid: int) -> list[str] | None:
    """Fields after the command name of /proc/<pid>/stat: [0] is the state, [2] the pgrp and
    [19] the start time. Split after the *last* ')' because a name may contain ') ('."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read(4096)
    except OSError:
        return None
    return raw[raw.rindex(b")") + 2 :].decode("ascii").split()


def start_time(pid: int) -> str | None:
    fields = stat_fields(pid)
    return fields[19] if fields else None


def alive(pid: int) -> bool:
    """Exists and is not a zombie."""
    fields = stat_fields(pid)
    return fields is not None and fields[0] not in ("Z", "X")


def wait_for(cond: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def read_line(popen: subprocess.Popen[bytes], timeout: float = 15.0) -> str:
    """One line of a child's stdout, bounded; an exited child or a timeout fails the test."""
    assert popen.stdout is not None
    fd = popen.stdout.fileno()
    deadline = time.monotonic() + timeout
    out = b""
    while not out.endswith(b"\n"):  # byte-wise on the fd: no stdio buffer to hide a ready line
        left = deadline - time.monotonic()
        ready = select.select([fd], [], [], max(left, 0))[0]
        assert ready, "timed out waiting for a line from the child"
        chunk = os.read(fd, 1)
        assert chunk, f"the child closed its stdout (exit {popen.poll()})"
        out += chunk
    return out.decode().strip()


class Procs:
    """Records processes at launch; ``close`` kills exactly those."""

    def __init__(self) -> None:
        self.popens: list[subprocess.Popen[bytes]] = []
        self.adopted: list[tuple[int, str]] = []

    def popen(self, args: list[str], **kw: object) -> subprocess.Popen[bytes]:
        p = subprocess.Popen(args, **kw)  # type: ignore[call-overload]
        self.popens.append(p)
        allow(p.pid)
        return p

    def adopt(self, pid: int) -> int:
        """Record a process we did not spawn directly (a grandchild a child announced)."""
        allow(pid)
        started = start_time(pid)
        assert started is not None, f"{pid} is not running"
        self.adopted.append((pid, started))
        return pid

    def close(self) -> None:
        for pid, started in self.adopted:
            if start_time(pid) == started:  # still the process we recorded
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for p in self.popens:
            if p.returncode is None:
                try:
                    p.kill()
                    p.wait(timeout=15)
                except (ProcessLookupError, ChildProcessError, subprocess.TimeoutExpired):
                    pass
            for f in (p.stdin, p.stdout, p.stderr):
                if f is not None:
                    f.close()


@pytest.fixture
def procs() -> Iterator[Procs]:
    p = Procs()
    try:
        yield p
    finally:
        p.close()
