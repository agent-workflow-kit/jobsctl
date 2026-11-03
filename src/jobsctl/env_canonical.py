"""The canonical environment (docs/interface.md, Canonical environment).

Every jobsctl process that starts other processes builds this at its start. It holds the
inherited names, the XDG directories and `TMPDIR`, never a `JOBSCTL_*` variable and never an
`env_file` value (the builder has no `env_file` input). `PATH` is replaced by `env.set.PATH`,
prefixed by `env.path_prepend` and cleaned.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from jobsctl.config import Config
from jobsctl.env_file import EnvWarning
from jobsctl.textsafe import echo

ALWAYS_INHERITED: Final = (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
)
RESERVED_PREFIX: Final = "JOBSCTL_"
_TEMP_ROOTS: Final = ("/tmp", "/run/user")


def matches(name: str, patterns: Iterable[str]) -> bool:
    """`name` is one of `patterns` (exact names, or a prefix ending in `*`)."""
    for pattern in patterns:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


@dataclass(frozen=True)
class CanonicalEnv:
    values: Mapping[str, str]
    warnings: tuple[EnvWarning, ...] = field(default=())

    @property
    def state_key(self) -> tuple[object, ...]:
        """Equal for equal warnings: log `path_entry_dropped` only when this changes."""
        return tuple(self.warnings)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _temp_roots() -> tuple[str, ...]:
    roots: list[str] = []
    for root in _TEMP_ROOTS:
        roots.append(root)
        real = os.path.realpath(root)
        if real not in roots:
            roots.append(real)
    return tuple(roots)


def _drop_reason(entry: str, roots: tuple[str, ...]) -> str | None:
    if entry == "":
        return "empty"
    if not entry.startswith("/"):
        return "relative"
    lexical = posixpath.normpath(entry)
    if lexical.startswith("//") and not lexical.startswith("///"):
        lexical = lexical[1:]  # POSIX keeps a leading "//"; it names the same directory here
    real = os.path.realpath(entry)
    if any(_under(lexical, r) or _under(real, r) for r in roots):
        return "temporary"
    if not os.path.exists(entry):
        return "missing"
    return None


def clean_path(path: str) -> tuple[str, tuple[EnvWarning, ...]]:
    """Drop empty, relative, non-existent and temporary entries of a `:`-separated PATH."""
    roots = _temp_roots()
    kept: list[str] = []
    dropped: list[EnvWarning] = []
    for entry in path.split(":"):
        reason = _drop_reason(entry, roots)
        if reason is None:
            kept.append(entry)
        else:
            dropped.append(EnvWarning("path_entry_dropped", reason=reason, entry=echo(entry, 256)))
    return ":".join(kept), tuple(dropped)


def path_entry_usable(entry: str) -> bool:
    """The entry survives PATH cleaning."""
    return _drop_reason(entry, _temp_roots()) is None


def prepend_path(prefix: Iterable[str], path: str | None) -> str:
    """`prefix` entries in front of `path`; an entry already in `path` moves to the front, so the
    operation is idempotent (the canonical and job environments both apply it)."""
    front = list(prefix)
    rest = [e for e in (path.split(":") if path else []) if e not in front]
    return ":".join(front + rest)


def build_canonical_env(config: Config, environ: Mapping[str, str]) -> CanonicalEnv:
    """Build the canonical environment from the process's own `environ` and `config`."""
    patterns = [*config.env.inherit, *ALWAYS_INHERITED]
    values = {
        k: v
        for k, v in environ.items()
        if matches(k, patterns) and not k.startswith(RESERVED_PREFIX)
    }
    if "PATH" in config.env.set:
        values["PATH"] = config.env.set["PATH"]
    if config.env.path_prepend:
        values["PATH"] = prepend_path(config.env.path_prepend, values.get("PATH"))
    warnings: tuple[EnvWarning, ...] = ()
    if "PATH" in values:
        values["PATH"], warnings = clean_path(values["PATH"])
    return CanonicalEnv(values, warnings)
