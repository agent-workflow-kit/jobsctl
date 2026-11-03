"""Error codes, the exit-code table and the error envelope (interface.md, Exit codes, Errors)."""

from __future__ import annotations

import enum
import re
from typing import Any

REFUSAL_REASONS = frozenset(
    {
        "live",
        "not_queued",
        "not_held",
        "not_terminal",
        "not_releasable",
        "not_escalated",
        "override_required",
        "duplicate_key",
        "other_queue",
        "attempt_mismatch",
    }
)

_JOB_ID_RE = re.compile(r"^jb-[a-z2-7]{8}\Z")


class ExitCode(enum.IntEnum):
    """Every exit code jobsctl uses. It never exits 11 (a veto ends a job inside its shim)."""

    OK = 0
    INTERNAL = 1
    USAGE = 2
    NOT_FOUND = 10
    FAILED = 12
    NEEDS_HUMAN = 13
    REFUSED = 20
    AMBIGUOUS = 21
    WAIT_TIMEOUT = 22
    INTERRUPTED = 130


# Error code (schemas/defs.json error_code) -> exit code.
ERROR_EXIT: dict[str, int] = {
    "usage": ExitCode.USAGE,
    "config_invalid": ExitCode.USAGE,
    "tui_missing": ExitCode.USAGE,
    "not_found": ExitCode.NOT_FOUND,
    "unknown_queue": ExitCode.NOT_FOUND,
    "cancel_pending": ExitCode.FAILED,
    "version_skew": ExitCode.FAILED,
    "interrupted": ExitCode.INTERRUPTED,
    "needs_human": ExitCode.NEEDS_HUMAN,
    "refused": ExitCode.REFUSED,
    "ambiguous": ExitCode.AMBIGUOUS,
    "busy": ExitCode.INTERNAL,
    "internal": ExitCode.INTERNAL,
}

_WAIT_EXIT = {
    "done": ExitCode.OK,
    "failed": ExitCode.FAILED,
    "held": ExitCode.NEEDS_HUMAN,
    "canceled": ExitCode.REFUSED,
}


def wait_exit_code(status: str) -> int:
    """Exit code of `wait` for a job that is terminal or held (22 and 130 are the caller's)."""
    try:
        return int(_WAIT_EXIT[status])
    except KeyError:
        raise ValueError(
            f"wait ends only on done, failed, held or canceled, not {status!r}"
        ) from None


class JobsctlError(Exception):
    """A failure with a stable code; `envelope()` is `schemas/cli/error.json`'s document."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        candidates: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_EXIT:
            raise ValueError(f"unknown error code {code!r}")
        if (code == "refused") != (reason is not None):
            raise ValueError("reason is required with, and only with, code 'refused'")
        if reason is not None and reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason {reason!r}")
        if (code == "ambiguous") != (candidates is not None):
            raise ValueError("candidates are required with, and only with, code 'ambiguous'")
        if candidates is not None and (
            len(candidates) < 2 or not all(_JOB_ID_RE.match(c) for c in candidates)
        ):
            raise ValueError("candidates are at least two full job ids (jb- and 8 of a-z2-7)")
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.candidates = candidates
        self.details = details

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT[self.code]

    def envelope(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.candidates is not None:
            body["candidates"] = self.candidates
        if self.details:
            body["details"] = self.details
        return {"error": body}


def usage_error(message: str, **details: Any) -> JobsctlError:
    return JobsctlError("usage", message, details=details or None)


def refused(reason: str, message: str, *, details: dict[str, Any] | None = None) -> JobsctlError:
    return JobsctlError("refused", message, reason=reason, details=details)
