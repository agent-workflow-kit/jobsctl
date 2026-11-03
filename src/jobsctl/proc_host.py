"""The platform seam of the process layer: a ``Host`` reads and signals processes.

``LinuxHost`` reads ``/proc`` (the root is injectable, so parsing is tested against fixture
trees); ``DarwinHost`` sits on a libproc object (fakeable). Everything above this module
(``proc_identity``, ``proc_signal``, ``proc_group``, ``proc_linux``) is platform-agnostic and
takes a ``Host``, so unit tests use fakes and never signal anything.
"""

from __future__ import annotations

import errno
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import gateway_proc

_STAT_LIMIT = 64 * 1024
_MAX_START = 2**63 - 1
_STATES = frozenset("RSDZTtXxKWPI")


class BootIdUnavailable(Exception):
    """The platform boot id cannot be read; liveness is then inconclusive."""


class ProcParseError(ValueError):
    """A process record is malformed. Callers treat the process as unknown, never as dead."""


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    start: int  # Linux: clock ticks since boot; macOS: microseconds since the epoch
    state: str
    zombie: bool


@dataclass(frozen=True)
class GroupScan:
    members: tuple[ProcInfo, ...]
    unreadable: int  # entries seen but not readable (permission, parse): the scan is incomplete


class Libproc(Protocol):
    def bsdinfo(self, pid: int) -> tuple[int, int, int, int, bool] | None: ...
    def group_pids(self, pgid: int) -> list[int]: ...
    def boot_uuid(self) -> str: ...


class Host(Protocol):
    system: str

    def boot_id(self) -> str: ...
    def process_info(self, pid: int) -> ProcInfo | None: ...
    def thread_group_id(self, pid: int) -> int | None: ...
    def procs_hidden(self) -> bool: ...
    def scan_group(self, pgid: int) -> GroupScan: ...
    def pidfd_supported(self) -> bool: ...
    def pidfd_open(self, pid: int) -> int: ...
    def pidfd_pid(self, fd: int) -> int | None: ...
    def pidfd_send_signal(self, fd: int, sig: int, flags: int = 0) -> None: ...
    def close_fd(self, fd: int) -> None: ...
    def kill(self, pid: int, sig: int) -> None: ...
    def killpg(self, pgid: int, sig: int) -> None: ...


def parse_proc_stat(raw: bytes, pid: int) -> ProcInfo:
    """Parse ``/proc/<pid>/stat``.

    The command name (field 2) may hold spaces, parentheses and newlines, so fields after it
    are taken after the **last** ``)``. Field 22 is the start time in clock ticks since boot;
    ticks are only compared for equality, so ``CLK_TCK`` is not needed.
    """
    lparen = raw.find(b"(")
    rparen = raw.rfind(b")")
    if lparen < 0 or rparen < lparen:
        raise ProcParseError("no command name in stat")
    if raw[:lparen].strip() != str(pid).encode():
        raise ProcParseError("stat pid does not match")
    fields = raw[rparen + 1 :].split()
    # fields[0] is field 3 (state); field 20 (num_threads) is fields[17], field 22 (starttime)
    # is fields[19]
    if len(fields) < 20:
        raise ProcParseError("too few stat fields")
    state = fields[0].decode("ascii", "replace")
    if len(state) != 1 or state not in _STATES:
        raise ProcParseError("bad state")
    try:
        ppid = int(fields[1])
        pgid = int(fields[2])
        threads = int(fields[17])
        start = int(fields[19])
    except ValueError as exc:
        raise ProcParseError("non-numeric stat field") from exc
    if ppid < 0 or pgid < 0 or not 0 <= start <= _MAX_START:
        raise ProcParseError("stat field out of range")
    # A thread-group leader whose main thread exited reads Z while other threads still run:
    # it is alive. Only a Z with no other thread is a zombie.
    zombie = state == "X" or (state == "Z" and threads <= 1)
    return ProcInfo(pid, ppid, pgid, start, state, zombie)


_MOUNTINFO_LIMIT = 1024 * 1024
_HIDING = frozenset({"2", "4", "invisible", "ptraceable"})


def _mountinfo_hides_procs(text: str) -> bool:
    """Whether the (last) mount at ``/proc`` has ``hidepid`` hiding other users' processes.

    No ``/proc`` mount found counts as hidden (unknown).
    """
    found = False
    hides = False
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 6 or fields[4] != "/proc":
            continue
        found = True
        tail = right.split()  # fstype, source, super options
        opts = [*fields[5].split(","), *(tail[2].split(",") if len(tail) > 2 else [])]
        hides = any(o.startswith("hidepid=") and o[8:] in _HIDING for o in opts)
    return hides or not found


def _read_limited(path: Path, limit: int) -> bytes:
    with open(path, "rb") as f:
        return f.read(limit)


class _SignalOps:
    """Signal operations shared by every platform (macOS has no pidfds)."""

    def kill(self, pid: int, sig: int) -> None:
        gateway_proc.kill(pid, sig)

    def killpg(self, pgid: int, sig: int) -> None:
        gateway_proc.killpg(pgid, sig)

    def close_fd(self, fd: int) -> None:
        gateway_proc.close_fd(fd)


class LinuxHost(_SignalOps):
    system = "linux"

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self._root = proc_root

    def procs_hidden(self) -> bool:
        """Whether ``/proc`` hides other users' processes (``hidepid=2``/``invisible``).

        Read from the ``/proc`` mount in ``<root>/self/mountinfo``. If that cannot be read or
        no ``/proc`` mount is found, assume hidden: a conservative answer only makes callers
        report "unknown" where they would have said "gone".
        """
        try:
            raw = _read_limited(self._root / "self/mountinfo", _MOUNTINFO_LIMIT)
        except OSError:
            return True
        return _mountinfo_hides_procs(raw.decode("utf-8", "replace"))

    def thread_group_id(self, pid: int) -> int | None:
        """The ``Tgid`` of ``pid`` (its own PID unless ``pid`` is a thread ID); ``None`` if
        the process is gone. ``ProcParseError`` if ``status`` has no valid ``Tgid`` line.
        """
        try:
            raw = _read_limited(self._root / str(pid) / "status", _STAT_LIMIT)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return None
        for line in raw.split(b"\n"):
            if line.startswith(b"Tgid:"):
                try:
                    return int(line[5:].strip())
                except ValueError:
                    break
        raise ProcParseError("no Tgid in status")

    def boot_id(self) -> str:
        try:
            raw = _read_limited(self._root / "sys/kernel/random/boot_id", 256)
        except OSError as exc:
            raise BootIdUnavailable(str(exc)) from exc
        text = raw.decode("ascii", "replace").strip()
        if not text or len(text) > 64 or not all(c.isalnum() or c == "-" for c in text):
            raise BootIdUnavailable("boot id is empty or malformed")
        return text

    def process_info(self, pid: int) -> ProcInfo | None:
        """The process's record; ``None`` if it does not exist (or is hidden by ``hidepid=2``).

        ``PermissionError`` (e.g. ``hidepid=1``) is not "gone": it propagates.
        """
        try:
            raw = _read_limited(self._root / str(pid) / "stat", _STAT_LIMIT)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return None
        return parse_proc_stat(raw, pid)

    def scan_group(self, pgid: int) -> GroupScan:
        members: list[ProcInfo] = []
        unreadable = 0
        try:
            names = [n.name for n in self._root.iterdir()]
        except OSError:
            return GroupScan((), 1)
        if self.procs_hidden():
            unreadable += 1  # other users' processes are invisible: the scan is incomplete
        for name in names:
            if not (name.isascii() and name.isdecimal()):
                continue
            try:
                info = self.process_info(int(name))
            except (OSError, ProcParseError):
                unreadable += 1
                continue
            if info is not None and info.pgid == pgid:
                members.append(info)
        return GroupScan(tuple(members), unreadable)

    def pidfd_supported(self) -> bool:
        return gateway_proc.pidfd_supported()

    def pidfd_open(self, pid: int) -> int:
        return gateway_proc.pidfd_open(pid)

    def pidfd_pid(self, fd: int) -> int | None:
        return gateway_proc.pidfd_pid(fd)

    def pidfd_send_signal(self, fd: int, sig: int, flags: int = 0) -> None:
        gateway_proc.pidfd_send_signal(fd, sig, flags)


class DarwinHost(_SignalOps):
    """macOS host. Identity through libproc and ``sysctl``; no pidfds. Unverified on a Mac."""

    system = "darwin"

    def __init__(self, libproc: Libproc | None = None) -> None:
        self._lib = libproc if libproc is not None else gateway_proc.DarwinLibproc()

    def boot_id(self) -> str:
        try:
            text = self._lib.boot_uuid().strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BootIdUnavailable(f"kern.bootsessionuuid: {exc}") from exc
        if not text or len(text) > 64:
            raise BootIdUnavailable("kern.bootsessionuuid is empty or malformed")
        return text

    def process_info(self, pid: int) -> ProcInfo | None:
        got = self._lib.bsdinfo(pid)
        if got is None:
            return None
        ppid, pgid, sec, usec, zombie = got
        return ProcInfo(pid, ppid, pgid, sec * 1_000_000 + usec, "Z" if zombie else "R", zombie)

    def thread_group_id(self, pid: int) -> int | None:
        return pid if self._lib.bsdinfo(pid) is not None else None

    def procs_hidden(self) -> bool:
        return False

    def scan_group(self, pgid: int) -> GroupScan:
        members: list[ProcInfo] = []
        unreadable = 0
        for pid in self._lib.group_pids(pgid):
            try:
                info = self.process_info(pid)
            except OSError:
                unreadable += 1
                continue
            if info is not None and info.pgid == pgid:
                members.append(info)
        return GroupScan(tuple(members), unreadable)

    def pidfd_supported(self) -> bool:
        return False

    def pidfd_open(self, pid: int) -> int:
        raise OSError(errno.ENOSYS, "no pidfds on macOS")

    def pidfd_pid(self, fd: int) -> int | None:
        return None

    def pidfd_send_signal(self, fd: int, sig: int, flags: int = 0) -> None:
        raise OSError(errno.ENOSYS, "no pidfds on macOS")


def default_host() -> Host:
    if sys.platform == "darwin":
        return DarwinHost()
    return LinuxHost()
