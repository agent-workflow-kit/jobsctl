"""The check order every command follows (interface.md, Commands).

usage (exit 2), then not found or ambiguous (10, 21), then state refusals (20), then the
human-only check (13), which applies only when the flag would take effect.
"""

from __future__ import annotations

from collections.abc import Callable

Check = Callable[[], None]


def run_checks(
    *,
    usage: Check | None = None,
    lookup: Check | None = None,
    state: Check | None = None,
    human: Check | None = None,
) -> None:
    """Run the stages in the documented order; the first check that raises decides the exit."""
    for stage in (usage, lookup, state, human):
        if stage is not None:
            stage()
