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


def spawn_job(
    argv: list[str], *, executable: str, env: dict[str, str], cwd: str
) -> subprocess.Popen[bytes]:
    """Start a job's process (fork and exec only): `executable` is the resolved absolute path and
    `argv` keeps the job's own argv[0]; a **new process group** whose id is the job's PID (the
    group the shim may later signal, and only that one), exactly `env`, `cwd`, stdin
    ``/dev/null``, stdout and stderr on **one** pipe the caller reads, every other descriptor
    closed. No new session: a job is not a daemon. Signal dispositions: SIGPIPE is restored to its
    default by `Popen`; a handled signal is reset by exec, an ignored one would be inherited, so
    the shim handles (rather than ignores) SIGHUP and SIGINT. `OSError` if the exec fails."""
    return subprocess.Popen(
        argv,
        executable=executable,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
        process_group=0,
        restore_signals=True,
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
