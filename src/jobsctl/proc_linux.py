"""Linux-only process helpers: dumpability, child subreaper, holding and reaping a leader."""

from __future__ import annotations

import errno
import os
import subprocess
import sys

from . import gateway_proc
from .proc_identity import validate_pid

__all__ = [
    "get_dumpable",
    "reap_held_leader",
    "set_child_subreaper",
    "set_undumpable",
    "wait_exited_keep",
]


def set_undumpable() -> bool:
    """``prctl(PR_SET_DUMPABLE, 0)``: other same-user processes cannot read this process's
    memory or ``/proc/<pid>/environ``. Resets on exec. Returns False where it does not exist
    (macOS); raises ``OSError`` if the call fails on Linux.
    """
    if sys.platform != "linux":
        return False
    gateway_proc.prctl(gateway_proc.PR_SET_DUMPABLE, 0)
    return True


def get_dumpable() -> int | None:
    if sys.platform != "linux":
        return None
    return gateway_proc.prctl(gateway_proc.PR_GET_DUMPABLE)


def set_child_subreaper(enabled: bool = True) -> bool:
    """``PR_SET_CHILD_SUBREAPER``: orphaned descendants reparent to this process (Linux only)."""
    if sys.platform != "linux":
        return False
    gateway_proc.prctl(gateway_proc.PR_SET_CHILD_SUBREAPER, 1 if enabled else 0)
    return True


def is_child_subreaper() -> bool | None:
    if sys.platform != "linux":
        return None
    return gateway_proc.prctl_get_int(gateway_proc.PR_GET_CHILD_SUBREAPER) == 1


def wait_exited_keep(pid: int) -> None:
    """Block until child ``pid`` has exited without reaping it (``WEXITED | WNOWAIT``), so the
    PID and process group id stay reserved. Raises ``OSError(ENOSYS)`` where ``os.waitid`` is
    missing (macOS CPython; the shim needs another wait there).
    """
    validate_pid(pid)
    gateway_proc.waitid_nowait(pid)


def reap_held_leader(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> int | None:
    """Reap a leader held with ``WNOWAIT`` by its exact PID and set ``proc.returncode``.

    ``Popen`` does not see a manual reap: without this, a later ``wait()``/``poll()`` fails
    with ``ECHILD`` or reports a wrong status. Returns the exit code, or ``None`` if the
    child was already reaped elsewhere (``returncode`` is then left as it was).
    """
    if proc.returncode is not None:
        return proc.returncode
    validate_pid(proc.pid)
    try:
        status = gateway_proc.waitpid(proc.pid)
    except ChildProcessError as exc:
        if exc.errno == errno.ECHILD:
            return None
        raise
    code = os.waitstatus_to_exitcode(status)
    proc.returncode = code
    return code
