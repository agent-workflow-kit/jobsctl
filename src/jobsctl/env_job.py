"""The job environment (docs/interface.md, Job environment), built from scratch at start step 5.

Order: (1) the canonical environment restricted to `env.inherit`, (2) `env.set`,
(3) `env.path_prepend`, (4) the job's `--env` entries, (5) the job's `--pass-env` names read from
`env_file` now, (6) `JOBSCTL_JOB_ID` and `JOBSCTL_ATTEMPT`. Step 6 comes last so nothing earlier
can forge them. Errors name a variable at most, never a value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from jobsctl.config import Config
from jobsctl.env_canonical import (
    RESERVED_PREFIX,
    CanonicalEnv,
    matches,
    path_entry_usable,
    prepend_path,
)
from jobsctl.env_file import EnvFile

_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")

MissingReason = Literal["undefined", "unavailable", "not_configured", "reserved", "invalid"]


class MissingEnvError(Exception):
    """The job ends `failed` (`missing_env`). `variable` is the first offending name (shown only
    when it is a valid name, else a fixed placeholder); no value is ever in the message."""

    def __init__(self, variable: str, reason: MissingReason) -> None:
        shown = variable if _NAME_RE.match(variable) else "<invalid name>"
        super().__init__(f"environment variable {shown} is not available ({reason})")
        self.variable = shown
        self.reason = reason


@dataclass(frozen=True)
class JobEnv:
    values: Mapping[str, str] = field(repr=False)
    # The `--pass-env` values read from `env_file` for this start: the shim adds them to its
    # masking set (`env_mask.masking_values`).
    pass_env_values: Mapping[str, str] = field(repr=False, default_factory=dict)


def _check_name(name: str) -> None:
    if _NAME_RE.match(name) is None:
        raise MissingEnvError(name, "invalid")
    if name.startswith(RESERVED_PREFIX):
        raise MissingEnvError(name, "reserved")


def build_job_env(
    config: Config,
    canonical: CanonicalEnv,
    env_file: EnvFile,
    *,
    job_id: str,
    attempt: int,
    env: Mapping[str, str],
    pass_env: Sequence[str],
) -> JobEnv:
    """Build a job's environment. `env_file` is the file as read at this moment."""
    for name in (*env, *pass_env):
        _check_name(name)

    values = {k: v for k, v in canonical.values.items() if matches(k, config.env.inherit)}
    values.update(config.env.set)
    if config.env.path_prepend:
        # The entries canonical cleaning keeps; the canonical step already warned about the rest.
        kept = [e for e in config.env.path_prepend if path_entry_usable(e)]
        values["PATH"] = prepend_path(kept, values.get("PATH"))
    values.update(env)

    passed: dict[str, str] = {}
    for name in pass_env:
        if not env_file.available:
            reason: MissingReason = (
                "not_configured" if env_file.status == "not_configured" else "unavailable"
            )
            raise MissingEnvError(name, reason)
        if name not in env_file.values:
            raise MissingEnvError(name, "undefined")
        passed[name] = env_file.values[name]
    values.update(passed)

    values["JOBSCTL_JOB_ID"] = job_id
    values["JOBSCTL_ATTEMPT"] = str(attempt)
    return JobEnv(MappingProxyType(values), MappingProxyType(passed))
