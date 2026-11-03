"""Gateway: the single boundary for subprocess and HTTP calls.

Every call this tool makes to an external program (and, later, over HTTP) goes through this
module so that tests can substitute fakes (fake binaries in ``tests/fakebin/`` or a patched
function). Commands are always run as argv lists, never through a shell, and untrusted text is
passed via stdin or files, never in argv or the environment.
"""

from __future__ import annotations

import subprocess


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


def popen(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str | None = None,
    start_new_session: bool = False,
) -> subprocess.Popen[str]:
    """Start ``argv`` without a shell, with text pipes for stdout and stderr and an explicit env.

    The caller owns the child: it records the PID (and, with ``start_new_session``, the group id,
    which equals the PID) at launch and signals only that exact process or group.
    """
    return subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=start_new_session,
        close_fds=True,
    )
