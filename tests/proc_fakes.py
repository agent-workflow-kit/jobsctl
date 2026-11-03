"""A scriptable fake ``Host`` for unit-testing the process layer without real processes."""

from __future__ import annotations

import errno
from dataclasses import dataclass, field

from jobsctl.gateway_proc import validate_pid, validate_signal
from jobsctl.proc_host import BootIdUnavailable, GroupScan, ProcInfo


def oserror(code: int) -> OSError:
    return OSError(code, "fake")


@dataclass
class FakeHost:
    system: str = "linux"
    boot: str | None = "boot-1"
    procs: dict[int, ProcInfo] = field(default_factory=dict)
    # pid -> exception raised by process_info (PermissionError, ProcParseError, ...)
    info_errors: dict[int, Exception] = field(default_factory=dict)
    # pid -> Tgid reported for it (default: the pid itself, as for a thread-group leader)
    tgids: dict[int, int] = field(default_factory=dict)
    hidden: bool = False  # /proc hides other users' processes (hidepid=2)
    pidfd_ok: bool = True
    pidfd_open_error: OSError | None = None
    pidfd_send_error: dict[int, OSError] = field(default_factory=dict)  # by flags
    send_error_all: OSError | None = None
    pidfd_pid_value: int | None = None  # None: use the pid given to pidfd_open
    kill_error: OSError | None = None
    killpg_error: OSError | None = None
    calls: list[tuple] = field(default_factory=list)
    # hook run right after pidfd_open (to simulate a PID reused between steps)
    after_open: object = None
    _fds: dict[int, int] = field(default_factory=dict)
    _next_fd: int = 100

    def boot_id(self) -> str:
        if self.boot is None:
            raise BootIdUnavailable("fake: no boot id")
        return self.boot

    def process_info(self, pid: int) -> ProcInfo | None:
        self.calls.append(("process_info", pid))
        if pid in self.info_errors:
            raise self.info_errors[pid]
        return self.procs.get(pid)

    def thread_group_id(self, pid: int) -> int | None:
        if pid not in self.procs:
            return None
        return self.tgids.get(pid, pid)

    def procs_hidden(self) -> bool:
        return self.hidden

    def scan_group(self, pgid: int) -> GroupScan:
        members = tuple(p for p in self.procs.values() if p.pgid == pgid)
        unreadable = sum(1 for pid in self.info_errors if pid not in self.procs)
        unreadable += 1 if self.hidden else 0
        return GroupScan(members, unreadable)

    def pidfd_supported(self) -> bool:
        return self.pidfd_ok

    def pidfd_open(self, pid: int) -> int:
        self.calls.append(("pidfd_open", pid))
        if self.pidfd_open_error is not None:
            raise self.pidfd_open_error
        if pid not in self.procs:
            raise oserror(errno.ESRCH)
        fd = self._next_fd
        self._next_fd += 1
        self._fds[fd] = pid
        if callable(self.after_open):
            self.after_open()
        return fd

    def pidfd_pid(self, fd: int) -> int | None:
        return self.pidfd_pid_value if self.pidfd_pid_value is not None else self._fds[fd]

    def pidfd_send_signal(self, fd: int, sig: int, flags: int = 0) -> None:
        validate_signal(sig)
        self.calls.append(("pidfd_send_signal", self._fds[fd], sig, flags))
        if self._fds[fd] not in self.procs:
            raise oserror(errno.ESRCH)  # like the kernel: a pidfd to a reaped process
        if self.send_error_all is not None:
            raise self.send_error_all
        if flags in self.pidfd_send_error:
            raise self.pidfd_send_error[flags]

    def close_fd(self, fd: int) -> None:
        self.calls.append(("close", self._fds.pop(fd)))

    def kill(self, pid: int, sig: int) -> None:
        validate_pid(pid)
        validate_signal(sig)
        self.calls.append(("kill", pid, sig))
        if self.kill_error is not None:
            raise self.kill_error

    def killpg(self, pgid: int, sig: int) -> None:
        validate_pid(pgid)
        validate_signal(sig)
        self.calls.append(("killpg", pgid, sig))
        if self.killpg_error is not None:
            raise self.killpg_error

    def sent(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("kill", "killpg", "pidfd_send_signal")]


def proc(
    pid: int, start: int = 1000, *, pgid: int | None = None, ppid: int = 1, zombie: bool = False
) -> ProcInfo:
    return ProcInfo(
        pid=pid,
        ppid=ppid,
        pgid=pid if pgid is None else pgid,
        start=start,
        state="Z" if zombie else "S",
        zombie=zombie,
    )
