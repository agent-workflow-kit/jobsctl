"""Shared helpers for the hold, release and cancel tests: add jobs through the CLI, put rows into
states only later issues reach (starting, running, escalated and exit-13 holds), read them back."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.helpers import Result, run_cli

HUMAN = ("--actor", "human:kim")
EFFECTIVE = json.dumps({"timeout": None, "max_retries": 0, "kill_grace": 30})


def config_file(tmp_path: Path, **keys: Any) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(keys), encoding="utf-8")  # JSON is YAML
    return str(path)


def add_job(capsys: pytest.CaptureFixture[str], *flags: str, config: str | None = None) -> str:
    cfg = ("--config", config) if config else ()
    res = run_cli(capsys, *cfg, "--json", "add", "--no-start", *flags, "--cwd", "/work", "--", "x")
    assert res.code == 0, res.out + res.err
    return str(res.envelope()["job"]["id"])


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    conn.row_factory = sqlite3.Row
    return conn


def sql(statement: str, *params: Any) -> None:
    with db() as conn:
        conn.execute(statement, params)


def row(job_id: str) -> sqlite3.Row:
    with db() as conn:
        found = conn.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
    assert found is not None
    return found


def attempt_row(job_id: str, n: int = 1) -> sqlite3.Row:
    with db() as conn:
        found = conn.execute(
            "SELECT * FROM attempts WHERE job_id = ? AND n = ?", [job_id, n]
        ).fetchone()
    assert found is not None
    return found


def add_attempt(job_id: str, n: int = 1, **over: Any) -> None:
    values: dict[str, Any] = {
        "job_id": job_id,
        "n": n,
        "claim_id": f"claim-{n}",
        "status": "starting",
        "claimed_at": "2026-01-01T00:00:00.000Z",
        "claimed_by": "pass:1",
        "jobsctl_version": "0.0.1",
    }
    values.update(over)
    cols = ", ".join(f'"{k}"' for k in values)
    marks = ", ".join("?" for _ in values)
    sql(f"INSERT INTO attempts ({cols}) VALUES ({marks})", *values.values())


def make_starting(job_id: str) -> None:
    add_attempt(job_id, 1)
    sql("UPDATE jobs SET status = 'starting', attempt = 1 WHERE id = ?", job_id)


def make_running(job_id: str) -> None:
    add_attempt(
        job_id, 1, status="running", running_at="2026-01-01T00:00:01.000Z", effective=EFFECTIVE
    )
    sql("UPDATE jobs SET status = 'running', attempt = 1 WHERE id = ?", job_id)


def make_held(job_id: str, hold_reason: str, escalated_hooks: tuple[str, ...] = ()) -> None:
    """`escalated` and `exit_13` holds end an attempt with attempt status `held`."""
    if hold_reason != "manual":
        exit_record = (
            json.dumps({"kind": "exited", "code": 13, "signal": None, "core": False})
            if hold_reason == "exit_13"
            else None
        )
        add_attempt(
            job_id,
            1,
            status="held",
            hold_reason=hold_reason,
            ended_at="2026-01-01T00:00:02.000Z",
            escalated_hooks=json.dumps(list(escalated_hooks)),
            exit=exit_record,
            resolved_path="/bin/x" if exit_record else None,
            leftovers=0 if exit_record else None,
            running_at="2026-01-01T00:00:01.000Z" if exit_record else None,
            effective=EFFECTIVE if exit_record else None,
        )
        sql("UPDATE jobs SET attempt = 1 WHERE id = ?", job_id)
        if hold_reason == "exit_13":  # the job process ran, so a tail exists
            sql(
                "INSERT INTO outputs (job_id, n, tail, updated_at) VALUES (?, 1, ?, ?)",
                job_id,
                b"out",
                "2026-01-01T00:00:02.000Z",
            )
    sql("UPDATE jobs SET status = 'held', hold_reason = ? WHERE id = ?", hold_reason, job_id)


def make_done(job_id: str, status: str = "done") -> None:
    code = 0 if status == "done" else 1
    add_attempt(
        job_id,
        1,
        status=status,
        reason="exit",
        resolved_path="/bin/x",
        leftovers=0,
        effective=EFFECTIVE,
        running_at="2026-01-01T00:00:01.000Z",
        ended_at="2026-01-01T00:00:03.000Z",
        exit=json.dumps({"kind": "exited", "code": code, "signal": None, "core": False}),
    )
    sql(
        "UPDATE jobs SET status = ?, reason = 'exit', attempt = 1, "
        "ended_at = '2026-01-01T00:00:03.000Z' WHERE id = ?",
        status,
        job_id,
    )


def log_records(event: str | None = None) -> list[dict[str, Any]]:
    path = resolve_state_dir() / "log.jsonl"
    if not path.exists():
        return []
    records = validate_record_file(path, "log.json")
    return [r for r in records if event is None or r["event"] == event]


def audit_records() -> list[dict[str, Any]]:
    path = resolve_state_dir() / "audit.jsonl"
    if not path.exists():
        return []
    return validate_record_file(path, "audit.json")


def outbox() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM hook_outbox ORDER BY seq").fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def table_dump() -> dict[str, list[tuple[Any, ...]]]:
    with db() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n}")] for n in sorted(names)}


def change(res: Result, *, code: int = 0) -> dict[str, Any]:
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    validate_against("cli/change.json", doc)
    return doc


def refusal(res: Result, reason: str, *, code: int = 20) -> dict[str, Any]:
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    assert doc["error"]["code"] == "refused" and doc["error"]["reason"] == reason
    return doc


def hold_cli(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(capsys, "--json", "hold", *flags)


def release_cli(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(capsys, "--json", "release", *flags)
