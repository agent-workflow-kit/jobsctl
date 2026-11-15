"""Seeding helpers for the `list`, `show` and `label` tests: rows written straight to state.db."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from jobsctl import gateway
from jobsctl.store_db import OpenMode, Store
from jobsctl.timestamps import format_timestamp
from tests.store_helpers import attempt_values, insert_row, job_values

NOW = dt.datetime(2026, 3, 10, 12, 0, 0, tzinfo=dt.UTC)


def freeze_clock(monkeypatch: pytest.MonkeyPatch, now: dt.datetime = NOW) -> None:
    monkeypatch.setattr(gateway, "utc_now", lambda: now)


def ago(delta: dt.timedelta, now: dt.datetime = NOW) -> str:
    return format_timestamp(now - delta)


def seed_job(
    job_id: str,
    *,
    labels: list[tuple[str, str]] = (),  # type: ignore[assignment]
    attempts: list[dict[str, Any]] = (),  # type: ignore[assignment]
    output: dict[str, Any] | None = None,
    output_attempt: int | None = None,
    **over: Any,
) -> None:
    """Insert a job (defaults: queued, queue `q`), its labels, attempts and output tail."""
    over.setdefault("queue", "q")
    over.setdefault("position", f"m{job_id[-4:]}")
    over = {k: json.dumps(v) if isinstance(v, list | dict) else v for k, v in over.items()}
    with Store.open(mode=OpenMode.CLI) as store, store.write() as tx:
        insert_row(tx, "jobs", job_values(job_id, **over))
        for key, value in labels:
            tx.execute(
                "INSERT INTO job_labels (job_id, key, value) VALUES (?, ?, ?)",
                [job_id, key, value],
            )
        for n, extra in enumerate(attempts, start=1):
            extra = {
                k: json.dumps(v) if isinstance(v, list | dict) else v for k, v in extra.items()
            }
            insert_row(tx, "attempts", attempt_values(job_id, n, f"claim-{job_id}-{n}", **extra))
        if output is not None:
            row = {
                "job_id": job_id,
                "n": output_attempt or len(attempts) or 1,
                "tail": b"",
                "updated_at": "2026-03-10T11:00:00.000Z",
            }
            row.update(output)
            insert_row(tx, "outputs", row)


GOLDEN = __import__("pathlib").Path(__file__).resolve().parent / "golden"


def check_golden(name: str, document: object) -> None:
    """Compare `document` with `tests/golden/<name>`; `UPDATE_GOLDEN=1` rewrites the file."""
    import os

    path = GOLDEN / name
    text = json.dumps(document, indent=2, sort_keys=False) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text


# -- attempt rows consistent with schemas/defs.json status_rules ------------------------------

CLAIMED = "2026-03-10T10:00:00.000Z"
RUNNING = "2026-03-10T10:00:01.000Z"
ENDED = "2026-03-10T11:00:00.000Z"
EFFECTIVE = {"timeout": None, "max_retries": 0, "kill_grace": 10}


def exit_record(code: int) -> dict[str, Any]:
    return {"kind": "exited", "code": code, "signal": None, "core": False}


def starting_attempt(**over: Any) -> dict[str, Any]:
    return {"status": "starting", "claimed_at": CLAIMED, **over}


def running_attempt(**over: Any) -> dict[str, Any]:
    return {
        "status": "running",
        "claimed_at": CLAIMED,
        "running_at": RUNNING,
        "resolved_path": "/bin/true",
        "effective": EFFECTIVE,
        **over,
    }


def ran(
    status: str = "failed", reason: str | None = "exit", code: int = 1, **over: Any
) -> dict[str, Any]:
    """An attempt that ran and ended (or, with `status="held"`, ended in a hold)."""
    base = running_attempt(
        status=status, reason=reason, ended_at=ENDED, exit=exit_record(code), leftovers=0
    )
    base.update(over)
    return base


def seed_finished(
    job_id: str, status: str = "done", reason: str = "exit", ended_at: str = ENDED, **over: Any
) -> None:
    """A terminal job with its one attempt, consistent with the schema's status rules."""
    code = 0 if status == "done" else 1
    seed_job(
        job_id,
        status=status,
        reason=reason,
        attempt=1,
        ended_at=ended_at,
        attempts=[ran(status, reason, code, ended_at=ended_at)],
        **over,
    )
