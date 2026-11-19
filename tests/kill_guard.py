"""A test may signal only processes it started: anything else fails the test before the syscall.

Allowed targets of a real signal: the test process itself, every PID of a `Popen` created during
the test, PIDs a fixture registered (`allow`: what `JobGuard` and `RealShims` track; a PID read
back from the database is registered only after its recorded identity is confirmed against the
live process, so the guard can catch a stale or reused PID) and descendants of the test process.
Signal 0 passes. A test that really must signal something else carries
`@pytest.mark.allow_any_kill`.
"""

from __future__ import annotations

import functools
import os
import signal
import subprocess
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from jobsctl import gateway_proc
from jobsctl.proc_host import Host, default_host

_allowed: set[int] = set()
violations: list[str] = []


def allow(pid: int) -> None:
    """Register a PID (or the group id equal to a leader's PID) the test may signal."""
    _allowed.add(pid)


@functools.cache
def _host() -> Host:
    """The platform host, built on first use: every test module imports this one (autouse via
    conftest), so a host that fails to construct (a libproc load error) must not break the import.
    """
    return default_host()


def _parent_of(pid: int) -> int | None:
    """The parent PID through the platform's own host (``/proc`` on Linux, libproc on macOS).

    Anything unreadable, or a host that cannot be built, is "unknown", which the guard treats as
    not a descendant: it refuses (the guard fails safe).
    """
    try:
        info = _host().process_info(pid)
    except Exception:  # noqa: BLE001 - any failure means "unknown", never "allowed"
        return None
    return None if info is None else info.ppid


def _descendant_of_us(pid: int) -> bool:
    me = os.getpid()
    seen = 0
    while pid > 1 and seen < 64:
        if pid == me:
            return True
        parent = _parent_of(pid)
        if parent is None:
            return False
        pid = parent
        seen += 1
    return False


def _permitted(pid: int) -> bool:
    return pid == os.getpid() or pid in _allowed or (pid > 1 and _descendant_of_us(pid))


def _refuse(what: str, target: int, sig: int) -> None:
    message = (
        f"{what}({target}, {sig}): the test did not start that process "
        "(record it, or mark the test allow_any_kill)"
    )
    violations.append(message)  # also reported at teardown: a thread or a handler may swallow
    pytest.fail(message, pytrace=False)


def guarded_kill(real: Callable[..., Any]) -> Callable[[int, int], None]:
    def kill(pid: int, sig: int) -> None:
        if sig != 0 and not (pid > 0 and _permitted(pid)):
            _refuse("os.kill", pid, sig)
        real(pid, sig)

    return kill


def guarded_killpg(real: Callable[..., Any]) -> Callable[[int, int], None]:
    def killpg(pgid: int, sig: int) -> None:
        if sig != 0 and not (pgid > 1 and pgid != os.getpgrp() and _permitted(pgid)):
            _refuse("os.killpg", pgid, sig)
        real(pgid, sig)

    return killpg


def _fd_target(fd: int) -> int | None:
    pid = gateway_proc.pidfd_pid(fd)
    return pid


def guarded_pidfd_send(real: Callable[..., Any]) -> Callable[..., None]:
    def send(fd: int, sig: int, *args: Any, **kwargs: Any) -> None:
        if sig != 0:
            pid = _fd_target(fd)
            if pid is None or (pid > 0 and not _permitted(pid)):  # -1: already exited
                _refuse("pidfd_send_signal", -2 if pid is None else pid, sig)
        real(fd, sig, *args, **kwargs)

    return send


def guarded_raw(real: Callable[..., int]) -> Callable[..., int]:
    def raw(number: int, *args: int) -> int:
        if number == gateway_proc.SYS_PIDFD_SEND_SIGNAL and len(args) >= 2 and args[1] != 0:
            send = guarded_pidfd_send(lambda *a, **k: None)
            send(args[0], args[1])
        return real(number, *args)

    return raw


@pytest.fixture(autouse=True)
def kill_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _allowed.clear()
    violations.clear()
    if request.node.get_closest_marker("allow_any_kill") is not None:
        yield
        return
    real_init = subprocess.Popen.__init__

    def recording_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        allow(self.pid)

    monkeypatch.setattr(subprocess.Popen, "__init__", recording_init)
    monkeypatch.setattr(os, "kill", guarded_kill(os.kill))
    monkeypatch.setattr(os, "killpg", guarded_killpg(os.killpg))
    if hasattr(signal, "pidfd_send_signal"):
        monkeypatch.setattr(
            signal, "pidfd_send_signal", guarded_pidfd_send(signal.pidfd_send_signal)
        )
    monkeypatch.setattr(gateway_proc, "_raw_syscall", guarded_raw(gateway_proc._raw_syscall))
    yield
    _allowed.clear()
    if violations:
        message = "; ".join(violations)
        violations.clear()
        pytest.fail(message, pytrace=False)
