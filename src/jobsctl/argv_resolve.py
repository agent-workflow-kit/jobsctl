"""Resolving `argv[0]` (docs/interface.md, Start sequence step 6).

Against the job environment's `PATH` unless it contains `/`. The resolved path is recorded on the
attempt and is what gets executed (`argv[0]` itself stays as the job asked for it). A command that
does not resolve to an absolute path of an executable regular file is `spawn_failed`.

Reading choices (the spec is silent): a `PATH` entry that is empty or relative is skipped (an empty
entry means "the current directory" to shells, which jobsctl never wants; cleaned `PATH`s have
none, but `--env PATH=...` can bring them back), a job environment without `PATH` finds nothing,
and a name with a `/` that is relative is taken relative to the job's cwd (as `exec` would).
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def _executable_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def resolve_argv0(argv0: str, env: Mapping[str, str], cwd: str) -> str | None:
    """The absolute path `argv0` resolves to, or `None` (`spawn_failed`)."""
    if argv0 == "" or "\x00" in argv0:
        return None
    if "/" in argv0:
        candidate = argv0 if argv0.startswith("/") else os.path.join(cwd, argv0)
        return candidate if _executable_file(candidate) else None
    for entry in env.get("PATH", "").split(":"):
        if not entry.startswith("/"):
            continue
        candidate = os.path.join(entry, argv0)
        if _executable_file(candidate):
            return candidate
    return None
