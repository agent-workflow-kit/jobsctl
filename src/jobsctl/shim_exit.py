"""Exit mapping: what a job's wait status means (docs/interface.md, Exit record and End reasons).

Pure functions over the ``waitid`` result of the job leader. A shell that reports a killed child as
exit 143 is `exited` with code 143: it cannot be told from a real `exit 143` (spike A6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

HELD_EXIT_CODE: Final = 13

# `waitid` si_code values (CLD_*); the names exist in `os` on every platform with `waitid`.
_EXITED: Final = getattr(os, "CLD_EXITED", 1)
_KILLED: Final = getattr(os, "CLD_KILLED", 2)
_DUMPED: Final = getattr(os, "CLD_DUMPED", 3)


class UnknownWaitStatus(ValueError):
    """The wait result is neither an exit nor a death by signal (stopped, continued, ...)."""


@dataclass(frozen=True)
class ExitRecord:
    kind: str  # exited, signaled, spawn_failed, unknown
    code: int | None = None
    signal: int | None = None
    core: bool = False

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "code": self.code, "signal": self.signal, "core": self.core}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ExitRecord:
        return cls(data["kind"], data["code"], data["signal"], bool(data["core"]))


SPAWN_FAILED_EXIT: Final = ExitRecord("spawn_failed")


@dataclass(frozen=True)
class Outcome:
    """How an attempt ends: the job's status, its end `reason` (terminal) or `hold_reason`."""

    status: str  # done, failed, canceled, held
    reason: str | None
    hold_reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status != "held"


def exit_record(si_code: int, si_status: int) -> ExitRecord:
    """The exit record of a `waitid` result of the job leader."""
    if si_code == _EXITED:
        return ExitRecord("exited", code=si_status)
    if si_code in (_KILLED, _DUMPED):
        return ExitRecord("signaled", signal=si_status, core=si_code == _DUMPED)
    raise UnknownWaitStatus(f"unexpected waitid code {si_code}")


def natural_outcome(record: ExitRecord) -> Outcome:
    """The end of a job that exited or died on its own (no timeout, no force request)."""
    if record.kind == "signaled":
        return Outcome("failed", "signaled")
    if record.code == 0:
        return Outcome("done", "exit")
    if record.code == HELD_EXIT_CODE:
        return Outcome("held", None, "exit_13")
    return Outcome("failed", "exit")


def forced_outcome() -> Outcome:
    """A committed `cancel --force` ends the job whatever its exit status (interface.md, Status
    transitions)."""
    return Outcome("canceled", "forced")


POST_HOOK: Final = {"done": "post-finish", "failed": "post-fail", "canceled": "post-cancel"}


def post_hook(outcome: Outcome) -> str:
    return "post-held" if outcome.status == "held" else POST_HOOK[outcome.status]
