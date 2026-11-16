"""Detecting an upgrade underneath a long-lived process (docs/interface.md, Upgrades).

`run --loop` re-execs itself between passes when the database schema is newer than this code
knows or when the installed jobsctl version differs from the one it started with. A re-exec keeps
the PID, the argument vector and the environment, so the process keeps its identity (a pass holds
the ``runner`` lease only while it runs; the lease identity ``boot:pid:start`` does not change
across an exec either). To rule out a loop of re-execs that change nothing, the new image is told
(through one environment variable, removed again at once) why it was started; the same reason does
not re-exec again.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from importlib import metadata

from jobsctl import gateway
from jobsctl.store_db import Store

REEXEC_ENV = "JOBSCTL_LOOP_REEXEC"
_MAX_REASON = 200


def installed_version() -> str | None:
    """The version of the jobsctl distribution installed now (read from disk on every call), or
    `None` when it cannot be determined."""
    try:
        return metadata.version("jobsctl")
    except Exception:  # not installed as a distribution, or an unreadable install
        return None


def reexec_argv(
    orig_argv: list[str] | None = None, executable: str | None = None, argv: list[str] | None = None
) -> list[str]:
    """The argument vector of the new image: the original one (`sys.orig_argv`, so interpreter
    flags such as ``-I`` and ``-m jobsctl`` survive) with the interpreter path made absolute."""
    exe = executable if executable is not None else sys.executable
    original = orig_argv if orig_argv is not None else getattr(sys, "orig_argv", None)
    if original:
        return [exe, *original[1:]]
    rest = list(argv if argv is not None else sys.argv[1:])
    return [exe, "-I", "-m", "jobsctl", *rest]


class UpgradeWatch:
    """Checks, before each iteration, whether this process should be replaced."""

    def __init__(self, environ: MutableMapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        previous = env.pop(REEXEC_ENV, None)  # never inherited by anything this process spawns
        self.previous = previous[:_MAX_REASON] if previous else None
        self.started_version: str | None = installed_version()
        self._failed: set[str] = set()

    def check(self, store: Store) -> str | None:
        """The reason to re-exec now, or `None`. A reason that already started this image, or
        whose re-exec failed, is not reported again (and does not hide another reason)."""
        for reason in self._reasons(store):
            if reason != self.previous and reason not in self._failed:
                return reason
        return None

    def _reasons(self, store: Store) -> list[str]:
        reasons: list[str] = []
        found = store.current_version()
        if found > store.known_version:
            reasons.append(f"schema v{found} is newer than v{store.known_version}")
        now = installed_version()
        if now is not None and self.started_version is None:
            self.started_version = now  # unreadable at start: the first readable value is the base
        elif now is not None and now != self.started_version:
            reasons.append(f"installed version {self.started_version} -> {now}")
        return reasons

    def failed(self, reason: str) -> None:
        self._failed.add(reason)

    def reexec(self, reason: str) -> None:
        """Replace this process (returns only when that failed, with `OSError`)."""
        exe = sys.executable
        if not exe or not os.access(exe, os.X_OK):
            raise OSError(f"interpreter {exe!r} is not executable")
        env = dict(os.environ)
        env[REEXEC_ENV] = reason[:_MAX_REASON]
        sys.stdout.flush()
        sys.stderr.flush()
        gateway.exec_replace(reexec_argv(), env)
