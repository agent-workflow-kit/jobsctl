"""Stopping a synchronous hook when the firing command is interrupted (interface.md, Hook
processes).

A CLI command that delivers a hook itself (`hold`, `cancel`, `resolve`) wraps the delivery:

    with InterruptGuard() as guard:
        result = run_hook(..., stop=guard)
        guard.check(result)   # raises the `interrupted` or `internal` error if it was stopped

SIGINT, SIGHUP and SIGTERM then only record themselves; ``run_hook`` sees ``stop`` answer, stops
the hook's group (SIGTERM, 5 seconds, SIGKILL, leader held unreaped) and returns. The command
leaves the event undelivered (``_deliver`` delivers it later) and exits 130 (`interrupted`) for
SIGINT, else 1 (`internal`).
TODO(#24): the delivery code calls this around every synchronous hook it runs in a command.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType, TracebackType

from .errors import JobsctlError
from .hook_run import HookResult

__all__ = ["HANDLED_SIGNALS", "InterruptGuard"]

HANDLED_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)


class InterruptGuard:
    def __init__(self) -> None:
        self.signum: int | None = None
        self._previous: dict[int, object] = {}

    def __enter__(self) -> InterruptGuard:
        if threading.current_thread() is threading.main_thread():
            for sig in HANDLED_SIGNALS:
                self._previous[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]
        self._previous.clear()

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        if self.signum is None:  # the first signal decides the exit code
            self.signum = signum

    def __call__(self) -> str | None:
        """The ``stop`` callback of ``run_hook``: the signal's name once one was received."""
        return None if self.signum is None else signal.Signals(self.signum).name

    def check(self, result: HookResult) -> None:
        """Raise the command's error if ``result`` was stopped by a received signal."""
        if result.stopped is None or self.signum is None:
            return
        name = signal.Signals(self.signum).name
        message = (
            f"interrupted by {name} while running hook {result.hook!r}; its event stays undelivered"
        )
        code = "interrupted" if self.signum == signal.SIGINT else "internal"
        raise JobsctlError(code, message)
