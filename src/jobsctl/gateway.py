"""Gateway: the single boundary for subprocess and HTTP calls.

Every call this tool makes to an external program (and, later, over HTTP) goes through this
module so that tests can substitute fakes (fake binaries in ``tests/fakebin/`` or a patched
function). Commands are always run as argv lists, never through a shell, and untrusted text is
passed via stdin or files, never in argv or the environment.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping


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


def spawn(
    argv: list[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    new_process_group: bool = False,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` without a shell, with pipes for stdin, stdout and stderr and exactly the
    environment ``env`` (never the caller's). With ``new_process_group`` the child leads a new
    process group whose id is its PID. The caller owns the child: it must wait for it and reap
    it by its exact PID.
    """
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        process_group=0 if new_process_group else None,
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


def _noop_handler(signum: int, frame: object) -> None:
    return None


def spawn_job(
    argv: list[str], *, executable: str, env: dict[str, str], cwd: str
) -> subprocess.Popen[bytes]:
    """Start a job's process (fork and exec only): `executable` is the resolved absolute path and
    `argv` keeps the job's own argv[0]; a **new process group** whose id is the job's PID (the
    group the shim may later signal, and only that one), exactly `env`, `cwd`, stdin
    ``/dev/null``, stdout and stderr on **one** pipe the caller reads, every other descriptor
    closed. No new session: a job is not a daemon. Signal dispositions: a handled signal is reset
    by exec but an ignored one would be inherited, so every signal the shim currently ignores is
    *handled* by a function that does nothing while the child is forked, then ignored again; the
    job starts with default dispositions (SIGPIPE is also restored by `Popen`). `OSError` if the
    exec fails."""
    ignored = _ignored_signals()
    for number in ignored:
        signal.signal(number, _noop_handler)
    try:
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
    finally:
        for number in ignored:
            signal.signal(number, signal.SIG_IGN)


def _ignored_signals() -> list[int]:
    """The signals this process ignores that a job must not inherit as ignored (none outside the
    main thread, where dispositions cannot be changed)."""
    if threading.current_thread() is not threading.main_thread():
        return []
    return [
        number
        for number in sorted(signal.valid_signals())
        if number not in (signal.SIGKILL, signal.SIGSTOP)
        and signal.getsignal(number) == signal.SIG_IGN
    ]


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


def exec_replace(argv: list[str], env: Mapping[str, str]) -> None:
    """Replace this process image with ``argv`` (no shell; ``argv[0]`` is an executable path) and
    exactly the environment ``env``. The PID, the process start time and the children stay; it
    returns only by raising ``OSError`` (the process is unchanged then). Tests replace this."""
    os.execve(argv[0], argv, dict(env))


def reap_child(pid: int) -> tuple[bool, int | None]:
    """Poll the child with exactly this PID (never "any child"): ``(True, None)`` while it runs,
    ``(True, status)`` once reaped, ``(False, None)`` when it is not a child of this process."""
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False, None
    return True, (None if done == 0 else status)


class CrontabError(Exception):
    """The ``crontab`` program could not be run or reported a failure (its message, no secrets)."""


_CRONTAB_TIMEOUT = 30.0


def _crontab(args: list[str], data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run ``crontab`` with bytes in and out (a crontab is not necessarily text) and the C locale,
    so its "no crontab" message is the one ``crontab_read`` looks for."""
    try:
        return subprocess.run(
            ["crontab", *args],
            input=data,
            capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=_CRONTAB_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CrontabError("the crontab command is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise CrontabError("the crontab command timed out") from exc
    except OSError as exc:
        raise CrontabError(f"cannot run crontab: {exc.strerror}") from exc


def _crontab_message(proc: subprocess.CompletedProcess[bytes]) -> str:
    text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    return f"exit {proc.returncode}: {text[:300]}" if text else f"exit {proc.returncode}"


def crontab_read() -> bytes | None:
    """The user's crontab exactly as stored, or ``None`` when the user has none."""
    proc = _crontab(["-l"])
    if proc.returncode == 0:
        return proc.stdout
    if proc.returncode == 1 and b"no crontab for" in proc.stderr:
        return None
    raise CrontabError(_crontab_message(proc))


def crontab_install(data: bytes) -> None:
    """Replace the user's crontab with ``data``, sent on stdin (``crontab -``). A file argument
    would be limited by the program's own path buffer (Debian's cron truncates long paths)."""
    proc = _crontab(["-"], data)
    if proc.returncode != 0:
        raise CrontabError(_crontab_message(proc))


def crontab_remove() -> None:
    """Remove the user's crontab."""
    proc = _crontab(["-r"])
    if proc.returncode != 0 and b"no crontab for" not in proc.stderr:
        raise CrontabError(_crontab_message(proc))
