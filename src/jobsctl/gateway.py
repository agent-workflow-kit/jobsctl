"""Gateway: the single boundary for subprocess and HTTP calls.

Every call this tool makes to an external program (and, later, over HTTP) goes through this
module so that tests can substitute fakes (fake binaries in ``tests/fakebin/`` or a patched
function). Commands are always run as argv lists, never through a shell, and untrusted text is
passed via stdin or files, never in argv or the environment.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping


def run(
    argv: list[str], *, stdin: str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output."""
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def spawn(
    argv: list[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    new_process_group: bool = False,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` without a shell, with pipes for stdin, stdout and stderr and exactly the
    environment ``env`` (never the caller's). With ``new_process_group`` the child leads a new
    process group whose id is its PID. The caller owns the child: it must wait for it and reap
    it by its exact PID.
    """
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        process_group=0 if new_process_group else None,
    )
