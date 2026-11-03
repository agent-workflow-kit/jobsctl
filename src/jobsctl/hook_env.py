"""The environment of a hook process (interface.md, Hooks, Environment).

A hook gets the canonical environment of the process that fires it, plus the `env_file` values
(except the ignored names), minus the variables in `hooks_strip_env`, plus `JOBSCTL_HOOK`,
`JOBSCTL_ID` and `JOBSCTL_OBJECT_TYPE=job`. The environment is built from scratch as a plain
dict and handed to the child as is; nothing is inherited implicitly.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Collection, Mapping

from .config import ENV_NAME_PATTERN, Config

__all__ = [
    "ENV_FILE_IGNORED",
    "build_hook_env",
    "env_file_name_ignored",
    "provisional_canonical_environment",
]

# `env_file` lines naming these are not added to hook environments (interface.md, Canonical
# environment): they would change a hook's PATH, loader or shell startup.
ENV_FILE_IGNORED = (
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "JOBSCTL_*",
    "LD_*",
    "DYLD_*",
    "PYTHON*",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "IFS",
)

_NAME = re.compile(ENV_NAME_PATTERN)
_ALWAYS_INHERITED = (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
)


def env_file_name_ignored(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in ENV_FILE_IGNORED)


def _clean(source: Mapping[str, str]) -> dict[str, str]:
    """Entries with a valid name and a value without NUL; never a `JOBSCTL_*` name."""
    return {
        name: value
        for name, value in source.items()
        if _NAME.fullmatch(name) and "\x00" not in value and not name.startswith("JOBSCTL_")
    }


def build_hook_env(
    canonical: Mapping[str, str],
    *,
    hook: str,
    job_id: str,
    env_file: Mapping[str, str] | None = None,
    strip: Collection[str] = (),
) -> dict[str, str]:
    """The environment of one hook run.

    `canonical` is the firing process's canonical environment; `env_file` the values of the
    configured `env_file` when it is available (or `None`). `strip` is `hooks_strip_env`. The
    strip applies to both sources and never to the three `JOBSCTL_*` variables set last.
    """
    env = _clean(canonical)
    if env_file:
        env.update(
            (name, value)
            for name, value in _clean(env_file).items()
            if not env_file_name_ignored(name)
        )
    for name in strip:
        env.pop(name, None)
    env["JOBSCTL_HOOK"] = hook
    env["JOBSCTL_ID"] = job_id
    env["JOBSCTL_OBJECT_TYPE"] = "job"
    return env


def provisional_canonical_environment(
    config: Config, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Steps 1 and 2 of the canonical environment only: the `env.inherit` names (patterns end
    in `*`) plus the always-inherited names, without `JOBSCTL_*`.

    TODO(#8): replace by the canonical environment builder (steps 3 and 4: `env.set.PATH`,
    `env.path_prepend`, PATH cleaning) and the `env_file` reader; callers pass its result to
    `build_hook_env` and do not change.
    """
    source = os.environ if environ is None else environ
    patterns = [*config.env.inherit, *_ALWAYS_INHERITED]
    picked = {
        name: value
        for name, value in source.items()
        if any(
            (name.startswith(p[:-1]) if p.endswith("*") else name == p) for p in patterns
        )
    }
    return _clean(picked)


