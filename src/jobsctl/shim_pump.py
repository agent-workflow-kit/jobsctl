"""The shim's output pump (docs/interface.md, Start sequence steps 8 and 9).

Reads the job's combined stdout/stderr pipe into an :class:`OutputCapture`, calls `periodic` every
`interval` seconds (the tail flush and the force-request check) and `tick` on every iteration (the
lease renewal), and watches for the leader's exit with `waitid(WEXITED | WNOWAIT | WNOHANG)`,
leaving the leader unreaped. After the exit `drain` reads what is left for at most 2 seconds and
never until end of file: a leftover process can hold the pipe open forever.
"""

from __future__ import annotations

import os
import select
from collections.abc import Callable

from jobsctl import gateway, gateway_proc
from jobsctl.output_capture import OutputCapture

READ_SIZE = 64 * 1024
READS_PER_WAKE = 16  # at most 1 MiB between two looks at the clock and the leader
POLL_SECONDS = 0.1
DRAIN_SECONDS = 2.0

WaitExit = Callable[[int], os.waitid_result | None]


def _wait_nohang(pid: int) -> os.waitid_result | None:
    return gateway_proc.waitid_nowait(pid, nohang=True)


class Pump:
    def __init__(
        self,
        fd: int,
        leader_pid: int,
        capture: OutputCapture,
        *,
        interval: float,
        periodic: Callable[[], None],
        tick: Callable[[], None] = lambda: None,
        wait_exit: WaitExit = _wait_nohang,
    ) -> None:
        self._fd = fd
        self._pid = leader_pid
        self._capture = capture
        self._interval = interval
        self._periodic = periodic
        self._tick = tick
        self._wait_exit = wait_exit
        self.eof = False
        os.set_blocking(fd, False)

    def _read_some(self) -> int:
        """Read what is available now (bounded); returns the number of bytes fed."""
        total = 0
        for _ in range(READS_PER_WAKE):
            try:
                data = os.read(self._fd, READ_SIZE)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            if not data:
                self.eof = True
                break
            self._capture.feed(data)
            total += len(data)
        return total

    def run(self) -> os.waitid_result:
        """Pump until the leader exits; returns its (unreaped) wait result."""
        next_periodic = gateway.monotonic() + self._interval
        while True:
            if self.eof:
                gateway.sleep(POLL_SECONDS)
            else:
                ready, _, _ = select.select([self._fd], [], [], POLL_SECONDS)
                if ready:
                    self._read_some()
            self._tick()
            if gateway.monotonic() >= next_periodic:
                self._periodic()
                next_periodic = gateway.monotonic() + self._interval
            exited = self._wait_exit(self._pid)
            if exited is not None:
                return exited

    def drain(self, seconds: float = DRAIN_SECONDS) -> None:
        """Read what is left, for at most `seconds` and not necessarily to the end of file."""
        deadline = gateway.monotonic() + seconds
        while not self.eof:
            remaining = deadline - gateway.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([self._fd], [], [], min(remaining, POLL_SECONDS))
            if ready:
                self._read_some()
