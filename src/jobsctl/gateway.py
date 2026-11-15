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
import subprocess
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
