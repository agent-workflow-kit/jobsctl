"""Exact-PID and verified process-group signals (docs/interface.md, Signal sequence).

Nothing here signals a PID or group without first verifying the recorded identity.

Linux, pidfds available: ``pidfd_open``, identity check on the pidfd's process, then
``pidfd_send_signal``. Once the pidfd is open it refers to one process for good, and the check
runs afterwards, so a PID reused before the open is caught by the start-time comparison and a
PID reused after it cannot redirect the signal. No race remains on this path.

Fallback (macOS, an architecture outside the syscall allow list, ``ENOSYS``, ``EPERM`` from a
seccomp filter, WSL1): the identity is re-checked immediately before ``kill``/``killpg``.
**Residual race:** the target can exit, be reaped and its PID reused between that check and
the call. For a process the caller has not reaped itself (its own child, or a group leader it
holds unreaped), the PID cannot be reused and the window does not exist; for other processes
(a shim signaled by ``cancel``, a ``shim_lost`` job's leader) it is accepted
(docs/interface.md, Residual risks).
"""

from __future__ import annotations

import enum
import errno
from dataclasses import dataclass

from .gateway_proc import PIDFD_SIGNAL_PROCESS_GROUP
from .proc_host import Host
from .proc_identity import Identity, Liveness, LivenessResult, check_identity, validate_pid

__all__ = ["Result", "SignalOutcome", "signal_group", "signal_process"]

_FALLBACK_ERRNOS = frozenset({errno.ENOSYS, errno.EPERM})
_MAX_SIGNAL = 64


class Result(enum.Enum):
    SENT = "sent"
    NOT_ALIVE = "not_alive"  # gone, reused PID, new boot or zombie: nothing was signaled
    UNKNOWN = "unknown"  # identity could not be established: nothing was signaled
    DENIED = "denied"  # the kernel refused the signal (EPERM)
    NOT_LEADER = "not_leader"  # group signal: the process is not leader of its own group
    ERROR = "error"  # an unexpected OS error; nothing is known to have been signaled


@dataclass(frozen=True)
class SignalOutcome:
    result: Result
    method: str | None = None  # pidfd, pidfd-group, kill, killpg
    detail: str = ""

    @property
    def sent(self) -> bool:
        return self.result is Result.SENT


def _check_signal(sig: int) -> int:
    if isinstance(sig, bool) or not isinstance(sig, int) or not 1 <= sig <= _MAX_SIGNAL:
        raise ValueError(f"unsafe signal number: {sig!r}")
    return sig


def _refuse(res: LivenessResult, *, method: str | None) -> SignalOutcome:
    kind = Result.UNKNOWN if res.state is Liveness.UNKNOWN else Result.NOT_ALIVE
    return SignalOutcome(kind, method, res.reason)


def _verify(host: Host, ident: Identity, allow_zombie: bool, group: bool) -> SignalOutcome | None:
    """``None`` if the identity (and, for a group, leadership) holds; else the refusal."""
    res = check_identity(host, ident, allow_zombie=allow_zombie)
    if not res.alive:
        return _refuse(res, method=None)
    if group and (res.info is None or res.info.pgid != ident.pid):
        return SignalOutcome(Result.NOT_LEADER, None, "process is not its own group leader")
    return None


def _pidfd_path(
    host: Host, ident: Identity, sig: int, flags: int, allow_zombie: bool, group: bool
) -> SignalOutcome | None:
    """Try the pidfd path. ``None`` means: use the fallback (unavailable here)."""
    if not host.pidfd_supported():
        return None
    method = "pidfd-group" if flags else "pidfd"
    try:
        fd = host.pidfd_open(ident.pid)
    except ProcessLookupError:
        return SignalOutcome(Result.NOT_ALIVE, method, "gone")
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return SignalOutcome(Result.NOT_ALIVE, method, "gone")
        if exc.errno in _FALLBACK_ERRNOS:
            return None
        return SignalOutcome(Result.ERROR, method, f"pidfd_open: {exc.strerror}")
    try:
        refusal = _verify(host, ident, allow_zombie, group)
        if refusal is not None:
            return SignalOutcome(refusal.result, method, refusal.detail)
        held = host.pidfd_pid(fd)
        if held is not None and held != ident.pid:
            # -1 means the process behind the pidfd already exited; anything else, a mismatch
            return SignalOutcome(Result.NOT_ALIVE, method, "pidfd_process_mismatch")
        try:
            host.pidfd_send_signal(fd, sig, flags)
        except ProcessLookupError:
            return SignalOutcome(Result.NOT_ALIVE, method, "gone")
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return SignalOutcome(Result.NOT_ALIVE, method, "gone")
            if exc.errno in _FALLBACK_ERRNOS or (flags and exc.errno == errno.EINVAL):
                return None  # EINVAL with the group flag: kernel older than 6.9
            return SignalOutcome(Result.ERROR, method, f"pidfd_send_signal: {exc.strerror}")
        return SignalOutcome(Result.SENT, method)
    finally:
        host.close_fd(fd)


def _fallback(
    host: Host, ident: Identity, sig: int, allow_zombie: bool, group: bool
) -> SignalOutcome:
    method = "killpg" if group else "kill"
    refusal = _verify(host, ident, allow_zombie, group)
    if refusal is not None:
        return SignalOutcome(refusal.result, method, refusal.detail)
    try:
        if group:
            host.killpg(ident.pid, sig)
        else:
            host.kill(ident.pid, sig)
    except ProcessLookupError:
        return SignalOutcome(Result.NOT_ALIVE, method, "gone")
    except PermissionError:
        return SignalOutcome(Result.DENIED, method, "permission denied")
    except OSError as exc:
        return SignalOutcome(Result.ERROR, method, f"{method}: {exc.strerror}")
    return SignalOutcome(Result.SENT, method)


def signal_process(
    host: Host, ident: Identity, sig: int, *, allow_zombie: bool = False
) -> SignalOutcome:
    """Send ``sig`` to the exact process ``ident``, only while its identity holds."""
    validate_pid(ident.pid)
    _check_signal(sig)
    out = _pidfd_path(host, ident, sig, 0, allow_zombie, group=False)
    if out is not None:
        return out
    return _fallback(host, ident, sig, allow_zombie, group=False)


def signal_group(
    host: Host, leader: Identity, sig: int, *, allow_zombie: bool = True, use_pidfd: bool = False
) -> SignalOutcome:
    """Send ``sig`` to the process group whose leader is ``leader`` (pgid == leader PID).

    The group is signaled only while ``leader`` verifiably is that group's leader. The caller
    that spawned the leader keeps it unreaped (``allow_zombie``), which reserves the pgid.
    ``use_pidfd`` selects ``PIDFD_SIGNAL_PROCESS_GROUP`` (Linux 6.9+, for a leader that is not
    the caller's child); older kernels (``EINVAL``) fall back to ``killpg`` with a re-check.
    """
    validate_pid(leader.pid)
    _check_signal(sig)
    if use_pidfd:
        out = _pidfd_path(host, leader, sig, PIDFD_SIGNAL_PROCESS_GROUP, allow_zombie, group=True)
        if out is not None:
            return out
    return _fallback(host, leader, sig, allow_zombie, group=True)
