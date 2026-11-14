"""Gateway: the single boundary for subprocess and HTTP calls.

Every call this tool makes to an external program (and, later, over HTTP) goes through this
module so that tests can substitute fakes (fake binaries in ``tests/fakebin/`` or a patched
function). Commands are always run as argv lists, never through a shell, and untrusted text is
passed via stdin or files, never in argv or the environment.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import subprocess
import time


def run(
    argv: list[str], *, stdin: str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output."""
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def spawn_detached(argv: list[str], *, env: dict[str, str], cwd: str) -> subprocess.Popen[bytes]:
    """Start an internal process: no shell, a new session (so it survives its parent's exit and a
    hangup), `cwd`, exactly `env`, stdin and stdout/stderr on ``/dev/null`` and every other file
    descriptor closed. The child opens its own diagnostics file (``diag.py``). The caller keeps
    the handle: it reaps the child by this exact PID and never by "any child"."""
    return subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        shell=False,
    )


def sleep(seconds: float) -> None:
    """Sleep (tests replace this to run back-offs and loop intervals instantly)."""
    time.sleep(seconds)


def flock_exclusive(fd: int) -> None:
    """Block until this process holds an exclusive advisory lock on ``fd`` (released by
    :func:`funlock` or by closing ``fd``; the kernel drops it when the process dies)."""
    fcntl.flock(fd, fcntl.LOCK_EX)


def funlock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


def utc_now() -> dt.datetime:
    """The wall clock, timezone-aware UTC (tests replace this or pass their own clock)."""
    return dt.datetime.now(dt.UTC)


def monotonic() -> float:
    """A monotonic clock in seconds, for budgets (tests replace this or pass their own)."""
    return time.monotonic()
