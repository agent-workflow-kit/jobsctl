"""Process-level system calls: the process half of the gateway.

``gateway.py`` is the single boundary for subprocess calls. Raw process syscalls (``kill``,
pidfds, ``prctl``, ``waitid``, libproc, ``sysctl``) live here, in a separate module so that the
two halves of the gateway can change independently. Nothing outside ``gateway.py`` and this
module touches ``ctypes`` or signals a process. Unit tests replace the ``Host`` built on top of
these functions (``proc_host.py``) with fakes.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import signal
import sys
from typing import Any

from . import gateway

SYS_PIDFD_SEND_SIGNAL = 424
SYS_PIDFD_OPEN = 434
PIDFD_SIGNAL_PROCESS_GROUP = 4  # Linux 6.9 or later

MAX_PID = 2**31 - 1
MAX_SIGNAL = 64

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37

# Architectures where the unified syscall numbers 434 and 424 hold. ``platform.machine()`` is
# the kernel's name; a 32-bit interpreter on x86_64 (i386 ABI or x32) is refused below.
_SYSCALL_ARCHES = frozenset(
    {
        "x86_64",
        "i386",
        "i686",
        "aarch64",
        "arm",
        "armv6l",
        "armv7l",
        "armv8l",
        "riscv64",
        "ppc64le",
        "s390x",
    }
)


def syscall_fallback_allowed(system: str, machine: str, pointer_size: int) -> bool:
    """Whether raw ``syscall()`` numbers 434/424 may be used (Linux, allow-listed arch).

    x86_64 with a 4-byte pointer is an x32 or i386-on-x86_64 process: refused, because the
    number space cannot be told apart from here.
    """
    if system != "Linux" or machine not in _SYSCALL_ARCHES:
        return False
    return not (machine == "x86_64" and pointer_size != 8)


def _fallback_allowed_here() -> bool:
    return syscall_fallback_allowed(
        platform.system(), platform.machine(), ctypes.sizeof(ctypes.c_void_p)
    )


_libc: Any = None


def _get_libc() -> Any:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    return _libc


def _raw_syscall(number: int, *args: int) -> int:
    libc = _get_libc()
    ctypes.set_errno(0)
    res = libc.syscall(ctypes.c_long(number), *[ctypes.c_long(a) for a in args])
    if res == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(res)


def pidfd_supported() -> bool:
    """Whether a pidfd can be opened here (interpreter support or allow-listed fallback)."""
    if sys.platform != "linux":
        return False
    has_py = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
    return has_py or _fallback_allowed_here()


def pidfd_open(pid: int) -> int:
    """``pidfd_open(pid, 0)``; raises ``OSError`` (``ENOSYS`` when unavailable)."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid, 0)
    if not _fallback_allowed_here():
        raise OSError(errno.ENOSYS, "pidfd_open unavailable on this interpreter/architecture")
    return _raw_syscall(SYS_PIDFD_OPEN, pid, 0)


def validate_pid(pid: object) -> int:
    """A PID jobsctl may ever signal: 2..2**31-1. 0, 1 and negatives mean groups or init."""
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 < pid <= MAX_PID:
        raise ValueError(f"unsafe pid: {pid!r}")
    return pid


def validate_signal(sig: object) -> int:
    """A real signal number, 1..64; 0 (an existence probe) is not a signal jobsctl sends."""
    if isinstance(sig, bool) or not isinstance(sig, int) or not 1 <= sig <= MAX_SIGNAL:
        raise ValueError(f"unsafe signal number: {sig!r}")
    return sig


def pidfd_send_signal(fd: int, sig: int, flags: int = 0) -> None:
    validate_signal(sig)
    if flags == 0 and hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
        return
    if not _fallback_allowed_here():
        raise OSError(errno.ENOSYS, "pidfd_send_signal unavailable on this architecture")
    _raw_syscall(SYS_PIDFD_SEND_SIGNAL, fd, sig, 0, flags)


def pidfd_pid(fd: int) -> int | None:
    """The PID a pidfd refers to (``-1`` once it exited), from fdinfo; ``None`` if unreadable."""
    try:
        with open(f"/proc/self/fdinfo/{fd}", "rb") as f:
            text = f.read(4096)
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(b"Pid:"):
            try:
                return int(line[4:].strip())
            except ValueError:
                return None
    return None


def close_fd(fd: int) -> None:
    os.close(fd)


def kill(pid: int, sig: int) -> None:
    validate_pid(pid)
    validate_signal(sig)
    os.kill(pid, sig)


def killpg(pgid: int, sig: int) -> None:
    validate_pid(pgid)
    validate_signal(sig)
    os.killpg(pgid, sig)


def prctl(option: int, arg2: int = 0) -> int:
    libc = _get_libc()
    ctypes.set_errno(0)
    res = libc.prctl(
        ctypes.c_int(option),
        ctypes.c_ulong(arg2),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if res == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(res)


def prctl_get_int(option: int) -> int:
    """``prctl(option, &out)`` for options that store an int through a pointer."""
    libc = _get_libc()
    out = ctypes.c_int(0)
    ctypes.set_errno(0)
    res = libc.prctl(
        ctypes.c_int(option),
        ctypes.byref(out),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if res == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return out.value


def waitid_nowait(pid: int, nohang: bool = False) -> os.waitid_result | None:
    """Wait for child ``pid`` to exit, leaving it unreaped (``WEXITED | WNOWAIT``).

    Blocks until it exits; with ``nohang`` returns ``None`` at once if it has not exited.
    """
    if not hasattr(os, "waitid"):
        raise OSError(errno.ENOSYS, "os.waitid is unavailable on this platform")
    flags = os.WEXITED | os.WNOWAIT | (os.WNOHANG if nohang else 0)
    return os.waitid(os.P_PID, pid, flags)


def waitpid(pid: int) -> int:
    return os.waitpid(pid, 0)[1]


# --- macOS: libproc and sysctl (unverified on a real Mac; see issue #28) ---------------------

PROC_PIDTBSDINFO = 3
PROC_PGRP_ONLY = 2
SZOMB = 5


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_libproc: Any = None


def _get_libproc() -> Any:
    global _libproc
    if _libproc is None:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    return _libproc


SYSCTL = "/usr/sbin/sysctl"  # absolute: cron's PATH may lack /usr/sbin


class DarwinLibproc:
    """The ``libproc``/``sysctl`` calls ``DarwinHost`` needs."""

    def bsdinfo(self, pid: int) -> tuple[int, int, int, int, bool] | None:
        """``(ppid, pgid, start_sec, start_usec, is_zombie)``; ``None`` if no such process."""
        lib = _get_libproc()
        info = _ProcBsdInfo()
        size = ctypes.sizeof(info)
        ctypes.set_errno(0)
        got = lib.proc_pidinfo(
            ctypes.c_int(pid),
            ctypes.c_int(PROC_PIDTBSDINFO),
            ctypes.c_uint64(0),
            ctypes.byref(info),
            ctypes.c_int(size),
        )
        if got <= 0:
            err = ctypes.get_errno()
            if err in (0, errno.ESRCH):
                return None
            raise OSError(err, os.strerror(err))
        if got != size or info.pbi_pid != pid:
            raise OSError(errno.EIO, "short or mismatched proc_pidinfo result")
        return (
            int(info.pbi_ppid),
            int(info.pbi_pgid),
            int(info.pbi_start_tvsec),
            int(info.pbi_start_tvusec),
            info.pbi_status == SZOMB,
        )

    def group_pids(self, pgid: int) -> list[int]:
        lib = _get_libproc()
        need = lib.proc_listpids(
            ctypes.c_uint32(PROC_PGRP_ONLY), ctypes.c_uint32(pgid), None, ctypes.c_int(0)
        )
        if need <= 0:
            return []
        count = need // 4 + 16
        buf = (ctypes.c_int32 * count)()
        got = lib.proc_listpids(
            ctypes.c_uint32(PROC_PGRP_ONLY),
            ctypes.c_uint32(pgid),
            buf,
            ctypes.c_int(ctypes.sizeof(buf)),
        )
        if got <= 0:
            return []
        return [int(p) for p in buf[: got // 4] if p > 0]

    def boot_uuid(self) -> str:
        """The boot session UUID; ``""`` if ``sysctl`` fails. May raise ``OSError`` (missing
        binary) or ``subprocess.TimeoutExpired``: ``DarwinHost.boot_id`` maps both to unavailable.
        """
        res = gateway.run([SYSCTL, "-n", "kern.bootsessionuuid"], timeout=5)
        if res.returncode != 0:
            return ""
        return res.stdout.strip()
