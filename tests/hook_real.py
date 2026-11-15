"""Real hook processes: every one is recorded at launch and killed by that exact handle.

``HookTracker`` wraps ``gateway.spawn``: each hook leader is recorded the moment it exists
(with a pidfd while it is still an unreaped child of the test process). Teardown kills a still
unreaped leader's group (its pgid is reserved while it is unreaped). Background processes a hook
starts are announced by PID in a file; ``adopt`` records their identity, checked against the
leader's group, and teardown signals exactly those through their pidfds. Never a pattern kill.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway, gateway_proc
from jobsctl.proc_host import LinuxHost
from jobsctl.proc_identity import Identity, Liveness, check_identity, identify

HOST = LinuxHost()


@dataclass
class Spawned:
    popen: subprocess.Popen[bytes]
    pidfd: int | None
    pgid: int
    adopted: list[tuple[Identity, int | None]] = field(default_factory=list)


class HookTracker:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.spawned: list[Spawned] = []
        real = gateway.spawn

        def recording(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            popen = real(*args, **kwargs)
            try:
                fd: int | None = gateway_proc.pidfd_open(popen.pid)
            except OSError:
                fd = None
            self.spawned.append(Spawned(popen, fd, popen.pid))
            return popen

        monkeypatch.setattr(gateway, "spawn", recording)

    @property
    def last(self) -> Spawned:
        return self.spawned[-1]

    def adopt(
        self, pidfile: Path, owner: Spawned | None = None, timeout: float = 5.0
    ) -> list[Identity]:
        """Record the PIDs a hook wrote to ``pidfile`` (one per line) for teardown."""
        owner = owner or self.last
        end = time.monotonic() + timeout
        pids: list[int] = []
        while time.monotonic() < end:
            try:
                pids = [int(x) for x in pidfile.read_text().split()]
            except (OSError, ValueError):
                pids = []
            if pids:
                break
            time.sleep(0.01)
        found: list[Identity] = []
        for pid in pids:
            try:
                info = HOST.process_info(pid)
                if info is None or info.pgid != owner.pgid:
                    continue  # gone, or not in the hook's group: never touch it
                ident = identify(HOST, pid)
                fd = gateway_proc.pidfd_open(pid)
            except (OSError, LookupError, ValueError):
                continue
            owner.adopted.append((ident, fd))
            found.append(ident)
        return found

    def teardown(self) -> None:
        for sp in self.spawned:
            if sp.popen.returncode is None:
                try:
                    gateway_proc.killpg(sp.pgid, signal.SIGKILL)  # leader unreaped: pgid ours
                except OSError:
                    pass
            for _ident, fd in sp.adopted:
                if fd is not None:
                    try:
                        gateway_proc.pidfd_send_signal(fd, signal.SIGKILL)
                    except OSError:
                        pass
                    os.close(fd)
                else:  # pragma: no cover
                    pass
            if sp.popen.returncode is None:
                try:
                    sp.popen.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pytest.fail(f"hook leader {sp.pgid} survived SIGKILL")
            for stream in (sp.popen.stdin, sp.popen.stdout, sp.popen.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if sp.pidfd is not None:
                os.close(sp.pidfd)


def recorded_pids(pidfile: Path) -> list[int]:
    return [int(x) for x in pidfile.read_text().split()]


def running_pids(pids: list[int]) -> list[int]:
    """Those of ``pids`` that still run (absent or zombie processes do not)."""
    out = []
    for pid in pids:
        info = HOST.process_info(pid)
        if info is not None and not info.zombie:
            out.append(pid)
    return out


def alive(ident: Identity) -> bool:
    """Whether the recorded process still runs (a zombie waiting for init is dead)."""
    return check_identity(HOST, ident).state is Liveness.ALIVE


def wait_dead(ident: Identity, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(ident):
            return True
        time.sleep(0.01)
    return not alive(ident)


def tracker_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[HookTracker]:
    tracker = HookTracker(monkeypatch)
    try:
        yield tracker
    finally:
        tracker.teardown()
