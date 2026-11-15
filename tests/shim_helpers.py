"""Helpers of the shim tests: a claimed job, an in-process `Shim`, and readers of its results.

In-process shims run in the test process (its own identity is the shim's), so no signal handler,
subreaper flag or other process-wide setting is touched; the jobs they start are real processes
(each in the group the shim creates). A test that starts a long-running job kills exactly the
recorded group/PID it started.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jobsctl.config import Config
from jobsctl.leases import own_identity
from jobsctl.proc_host import default_host
from jobsctl.shim import Shim
from jobsctl.store_db import Store
from tests.store_helpers import attempt_values, insert_row, job_values

JOB = "jb-aaaaaaaa"
CLAIM = "claim-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SAFE_PATH = "/usr/bin:/bin"


def environ(home: str | None = None, **extra: str) -> dict[str, str]:
    """The environment a shim process would have (a private HOME and a plain PATH)."""
    home = home or os.environ["HOME"]
    base = {
        "HOME": home,
        "PATH": SAFE_PATH,
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "LANG": "C.UTF-8",
    }
    base.update(extra)
    return base


def seed_claimed(
    store: Store,
    argv: list[str],
    *,
    job_id: str = JOB,
    claim_id: str = CLAIM,
    cwd: str = "/",
    env: dict[str, str] | None = None,
    pass_env: list[str] | None = None,
    queue: str = "q",
    overrides: dict[str, Any] | None = None,
    shim_identity: str | None = None,
    claimed_by: str | None = None,
    spawn_failures: int = 0,
) -> None:
    """A job claimed by a pass (`starting`, attempt 1) whose shim is this process."""
    me = own_identity(default_host())
    with store.write() as tx:
        insert_row(
            tx,
            "jobs",
            job_values(
                job_id,
                queue=queue,
                status="starting",
                attempt=1,
                argv=json.dumps(argv),
                cwd=cwd,
                env=json.dumps(env or {}),
                pass_env=json.dumps(pass_env or []),
                overrides=json.dumps(overrides or {}),
                spawn_failures=spawn_failures,
            ),
        )
        insert_row(
            tx,
            "attempts",
            attempt_values(
                job_id,
                1,
                claim_id,
                claimed_by=claimed_by or me,
                shim_identity=me if shim_identity is None else shim_identity or None,
            ),
        )


def make_shim(
    store: Store,
    config: Config | None = None,
    *,
    job_id: str = JOB,
    claim_id: str = CLAIM,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> Shim:
    return Shim(
        store,
        config or Config(),
        kwargs.pop("config_path", None),
        job_id,
        claim_id,
        environ=env if env is not None else environ(),
        **kwargs,
    )


def job(store: Store, job_id: str = JOB) -> Any:
    return store.query_one("SELECT * FROM jobs WHERE id = ?", [job_id])


def attempt(store: Store, job_id: str = JOB, n: int = 1) -> Any:
    return store.query_one("SELECT * FROM attempts WHERE job_id = ? AND n = ?", [job_id, n])


def tail_of(store: Store, job_id: str = JOB, n: int = 1) -> str | None:
    from jobsctl.output_store import load_tail

    with store.read() as tx:
        stored = load_tail(tx, job_id, n)
    return None if stored is None else stored.snapshot.text


def exit_of(store: Store, job_id: str = JOB) -> dict[str, Any] | None:
    row = attempt(store, job_id)
    return None if row["exit"] is None else json.loads(row["exit"])


def log_records(event: str | None = None) -> list[dict[str, Any]]:
    from jobsctl.store_dir import resolve_state_dir

    path = resolve_state_dir() / "log.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in records if event is None or r["event"] == event]


def shims_dir() -> Path:
    from jobsctl.store_dir import resolve_state_dir

    return resolve_state_dir() / "shims"


def one(store: Store, sql: str, params: list[Any] | None = None) -> Any:
    """One row that must exist."""
    row = store.query_one(sql, params or [])
    assert row is not None
    return row
