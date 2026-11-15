"""Schema-valid job and attempt objects for hook payload tests."""

from __future__ import annotations

from typing import Any

TS = "2026-11-03T12:00:00.000Z"
JOB_ID = "jb-abcdefgh"


def job_object(status: str = "starting", **over: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": JOB_ID,
        "queue": "default",
        "status": status,
        "reason": None,
        "hold_reason": None,
        "argv": ["echo", "hi"],
        "cwd": "/work",
        "env": {"B": "2", "A": "1"},
        "pass_env": [],
        "labels": ["k=v"],
        "unique_key": None,
        "priority": 0,
        "pinned": False,
        "rank": None,
        "attempt": 1,
        "retried_from_attempt": None,
        "overrides": {},
        "effective": {"timeout": None, "max_retries": 0, "kill_grace": 30},
        "not_before": None,
        "blocked_by": None,
        "force_requested_by": None,
        "override_hooks": [],
        "shim_lost": False,
        "created_at": TS,
        "enqueued_at": TS,
        "status_changed_at": TS,
        "claimed_at": TS,
        "ended_at": None,
        "actor": "human:me",
        "decision_ids": [],
        "exit": None,
        "leftovers": None,
        "resolved_path": None,
        "resolved_by": None,
        "note": None,
    }
    job.update(over)
    return job


def attempt_object(status: str = "starting", **over: Any) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "attempt": 1,
        "status": status,
        "reason": None,
        "hold_reason": None,
        "claimed_at": TS,
        "running_at": None,
        "ended_at": None,
        "exit": None,
        "leftovers": None,
        "resolved_path": None,
        "effective": None,
        "escalated_hooks": [],
        "vetoed_by": None,
        "hook_messages": [],
        "decision_ids": [],
        "force_requested_by": None,
        "resolved_by": None,
        "note": None,
        "jobsctl_version": "0.0.1",
    }
    attempt.update(over)
    return attempt
