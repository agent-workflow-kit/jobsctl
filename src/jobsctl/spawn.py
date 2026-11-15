"""Starting internal processes (docs/interface.md, Internal processes).

Each is ``[<python>, "-I", "-m", "jobsctl", [--config PATH,] <command>, ...]``, detached in a new
session, with cwd the state directory, stdin and stdout/stderr on ``/dev/null`` (the child
opens its own diagnostics file, :mod:`jobsctl.diag`), every other descriptor closed and the
canonical environment, which never holds an ``env_file`` value. The caller owns the returned
handle: it reaps that exact PID and nothing else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from jobsctl import gateway
from jobsctl.config import Config
from jobsctl.config_files import handoff_args
from jobsctl.env_canonical import build_canonical_env


def internal_argv(
    command: str, args: Sequence[str], config_path: Path | None, *, python: str | None = None
) -> list[str]:
    """The argv of an internal command; `python` defaults to the running interpreter."""
    return [
        python or sys.executable,
        "-I",
        "-m",
        "jobsctl",
        *handoff_args(config_path),
        command,
        *args,
    ]


def spawn_internal(
    state_dir: Path,
    config: Config,
    config_path: Path | None,
    command: str,
    args: Sequence[str] = (),
    *,
    python: str | None = None,
) -> subprocess.Popen[bytes]:
    env = dict(build_canonical_env(config, dict(os.environ)).values)
    # The child resolves the state directory from XDG_STATE_HOME (canonical, always inherited) or
    # HOME; pass HOME to jobsctl's own processes even when `env.inherit` omits it. Hooks and jobs
    # do not get it from here.
    if "HOME" in os.environ:
        env.setdefault("HOME", os.environ["HOME"])
    argv = internal_argv(command, args, config_path, python=python)
    return gateway.spawn_detached(argv, env=env, cwd=str(state_dir))
