"""Row builders and a private state dir for the store tests."""

from __future__ import annotations

import subprocess
from typing import Any

NOW = "2026-01-01T00:00:00.000Z"
LATER = "2026-01-01T00:01:00.000Z"


def job_values(job_id: str = "jb-aaaaaaaa", **over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": job_id,
        "queue": "default",
        "argv": '["true"]',
        "cwd": "/",
        "position": "m",
        "status": "queued",
        "created_at": NOW,
        "enqueued_at": NOW,
        "status_changed_at": NOW,
        "actor": "human:test",
    }
    values.update(over)
    return values


def attempt_values(job_id: str, n: int, claim_id: str, **over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "job_id": job_id,
        "n": n,
        "claim_id": claim_id,
        "status": "starting",
        "claimed_at": NOW,
        "claimed_by": "pass:1",
        "jobsctl_version": "0.0.1",
    }
    values.update(over)
    return values


def insert_row(tx: Any, table: str, values: dict[str, Any]) -> None:
    cols = ", ".join(f'"{k}"' for k in values)
    marks = ", ".join("?" for _ in values)
    tx.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))


def popen(
    argv: list[str], *, env: dict[str, str], cwd: str, start_new_session: bool = True
) -> subprocess.Popen[str]:
    """Start a test worker without a shell, with text pipes and an explicit env.

    The caller records the PID (with ``start_new_session``, also the group id) at launch and
    signals only that exact process or group.
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
