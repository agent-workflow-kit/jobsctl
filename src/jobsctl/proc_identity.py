"""Process identity ``(boot_id, pid, start)`` and liveness (docs/interface.md, Process identity).

Wall-clock time is never used. A process is alive only if its PID exists in the current boot
with the same start value. Readings that cannot decide (permission denied, malformed record,
boot id unavailable) are ``UNKNOWN``, never ``DEAD``: callers must not treat an unknown
process as gone, and must not signal it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .gateway_proc import validate_pid
from .proc_host import BootIdUnavailable, Host, ProcInfo, ProcParseError

__all__ = [
    "Identity",
    "Liveness",
    "LivenessResult",
    "ProcessGone",
    "check_identity",
    "identify",
    "validate_pid",
]


class ProcessGone(Exception):
    """No process with that PID exists."""


@dataclass(frozen=True)
class Identity:
    boot_id: str
    pid: int
    start: int

    def __post_init__(self) -> None:
        validate_pid(self.pid)
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise ValueError("boot_id must be a non-empty string")
        if isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 0:
            raise ValueError("start must be a non-negative integer")


class Liveness(enum.Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LivenessResult:
    state: Liveness
    reason: str  # ok, gone, reused, zombie, boot_changed, boot_id_unavailable, unreadable, ...
    info: ProcInfo | None = None

    @property
    def alive(self) -> bool:
        return self.state is Liveness.ALIVE


def identify(host: Host, pid: int) -> Identity:
    """The identity of the process now holding ``pid``.

    Raises ``ProcessGone``, ``BootIdUnavailable`` (never record an identity without a boot id),
    ``PermissionError`` or ``ProcParseError`` (also for a thread ID, which is not a process).
    """
    validate_pid(pid)
    boot = host.boot_id()
    info = host.process_info(pid)
    if info is None:
        raise ProcessGone(pid)
    tgid = host.thread_group_id(pid)
    if tgid is None:
        raise ProcessGone(pid)
    if tgid != pid:
        raise ProcParseError(f"pid {pid} is a thread of process {tgid}, not a process")
    return Identity(boot, pid, info.start)


def check_identity(host: Host, ident: Identity, *, allow_zombie: bool = False) -> LivenessResult:
    """Is the process recorded in ``ident`` alive?

    ``allow_zombie`` is for a job leader the caller keeps unreaped on purpose: it still
    reserves its PID and process group id, so it counts as alive.
    """
    try:
        boot = host.boot_id()
    except BootIdUnavailable:
        return LivenessResult(Liveness.UNKNOWN, "boot_id_unavailable")
    if boot != ident.boot_id:
        return LivenessResult(Liveness.DEAD, "boot_changed")
    try:
        info = host.process_info(ident.pid)
    except PermissionError:
        return LivenessResult(Liveness.UNKNOWN, "permission_denied")
    except (ProcParseError, OSError):
        return LivenessResult(Liveness.UNKNOWN, "unreadable")
    if info is None:
        if host.procs_hidden():
            return LivenessResult(Liveness.UNKNOWN, "hidepid")
        return LivenessResult(Liveness.DEAD, "gone")
    if info.start != ident.start:
        return LivenessResult(Liveness.DEAD, "reused")
    if info.zombie and not allow_zombie:
        return LivenessResult(Liveness.DEAD, "zombie", info)
    return LivenessResult(Liveness.ALIVE, "ok", info)
