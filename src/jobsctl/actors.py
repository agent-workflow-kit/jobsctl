"""Actors and the human-only gate (interface.md, Human-only flags)."""

from __future__ import annotations

import re

from jobsctl.errors import JobsctlError, usage_error

ACTOR_RE = re.compile(r"^(human|auto):[A-Za-z0-9._@-]{1,64}\Z")
RESERVED_ACTOR = "auto:jobsctl"


def parse_actor(value: str) -> str:
    """Validate an `--actor` value; `auto:jobsctl` is reserved for jobsctl's own transitions."""
    if not ACTOR_RE.match(value):
        raise usage_error(
            "actor must look like human:<name> or auto:<name> (name: 1 to 64 of A-Za-z0-9._@-)"
        )
    if value == RESERVED_ACTOR:
        raise usage_error("the actor auto:jobsctl is reserved for jobsctl's own transitions")
    return value


def sanitize_user_name(name: str) -> str:
    """Fit an OS or configured user name to the actor name grammar."""
    fitted = re.sub(r"[^A-Za-z0-9._@-]", "_", name)[:64]
    return fitted or "unknown"


def default_actor(*, me_name: str, stdin_isatty: bool) -> str:
    """`human:<me.name>` when stdin is a TTY, else `auto:unknown`."""
    return f"human:{sanitize_user_name(me_name)}" if stdin_isatty else "auto:unknown"


def require_human(flag: str, *, stdin_isatty: bool, explicit_actor: str | None) -> None:
    """The human-only gate: stdin is a TTY, or `--actor human:<user>` was given.

    Call it only when `flag` would take effect, and last in the check order. `--yes` never
    reaches this function: it cannot satisfy the gate.
    """
    if stdin_isatty or (explicit_actor is not None and explicit_actor.startswith("human:")):
        return
    raise JobsctlError(
        "needs_human",
        f"{flag} needs a human: run it from a terminal or pass --actor human:<user>",
    )
