"""hold, release and cancel racing each other and a runner's claim, in real processes.

Every process a test starts is recorded at launch (it leads its own session, so its group id is
its PID) and killed exactly, by that group, on teardown. Each runs with the test's private HOME
and XDG directories.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.manual_helpers import (
    add_job,
    attempt_row,
    audit_records,
    log_records,
    outbox,
    row,
)
from tests.store_helpers import popen

WORKER = Path(__file__).with_name("manual_worker.py")
TIMEOUT = 90
HOOKS = {"hooks": {"post-cancel": [{"cmd": "true"}], "post-held": [{"cmd": "true"}]}}


@dataclass
class Worker:
    proc: subprocess.Popen[str]
    pgid: int

    def result(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read() if self.proc.stderr else ""
        out = json.loads(line)
        assert self.proc.wait(timeout=TIMEOUT) == 0
        return out  # type: ignore[no-any-return]


class Pool:
    def __init__(self, tmp: Path, env: dict[str, str]) -> None:
        self.tmp, self.env, self.workers = tmp, env, []  # type: ignore[var-annotated]
        self.go_file = tmp / "go"

    def spawn(self, kind: str, *args: str) -> Worker:
        proc = popen(
            [sys.executable, "-I", str(WORKER), kind, str(self.go_file), *args],
            env=self.env,
            cwd=str(self.tmp),
        )
        worker = Worker(proc, pgid=proc.pid)
        self.workers.append(worker)
        assert proc.stdout is not None
        assert json.loads(proc.stdout.readline()) == {"ready": True}
        return worker

    def go(self) -> None:
        self.go_file.touch()

    def close(self) -> None:
        for w in self.workers:
            if w.proc.poll() is None:
                os.killpg(w.pgid, signal.SIGKILL)
            w.proc.wait(timeout=TIMEOUT)
            for stream in (w.proc.stdout, w.proc.stderr):
                if stream is not None:
                    stream.close()


@pytest.fixture
def pool(tmp_path: Path, private_home: Path) -> Iterator[Pool]:
    env = {
        "HOME": str(private_home),
        "XDG_CONFIG_HOME": str(private_home / ".config"),
        "XDG_STATE_HOME": str(private_home / ".local/state"),
        "XDG_DATA_HOME": str(private_home / ".local/share"),
        "XDG_CACHE_HOME": str(private_home / ".cache"),
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    work = tmp_path / "pool"
    work.mkdir()
    p = Pool(work, env)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def config(tmp_path: Path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(HOOKS), encoding="utf-8")
    return str(path)


def cli(pool: Pool, config: str, *argv: str) -> Worker:
    return pool.spawn("cli", "--config", config, "--json", *argv)


def changed(result: dict[str, Any]) -> bool:
    doc = json.loads(result["out"])
    return bool(doc["changed"])


@pytest.mark.parametrize("round_", range(5))
def test_cancel_racing_a_claim_has_one_consistent_outcome(
    capsys: pytest.CaptureFixture[str], pool: Pool, config: str, round_: int
) -> None:
    job_id = add_job(capsys, config=config)
    canceler = cli(pool, config, "cancel", job_id)
    claimer = pool.spawn("claim", job_id)
    pool.go()
    cancel_result, claim_result = canceler.result(), claimer.result()
    assert cancel_result["exit"] == 0 and changed(cancel_result)
    stored = row(job_id)
    assert stored["status"] == "canceled" and stored["reason"] == "canceled"
    if claim_result["claimed"]:  # the claim won: cancel then saw `starting`
        attempt = attempt_row(job_id)
        assert attempt["status"] == "canceled" and attempt["reason"] == "canceled"
        assert attempt["ended_at"] is not None
        assert log_records("transition")[-1]["from"] == "starting"
    else:  # cancel won: the claim found the job canceled
        assert claim_result["status"] == "canceled" and stored["attempt"] == 0
        assert log_records("transition")[-1]["from"] == "queued"
    assert len(outbox()) == 1 and outbox()[0]["hook"] == "post-cancel"
    assert len(log_records("transition")) == 1


def test_many_cancels_of_one_job_commit_exactly_once(
    capsys: pytest.CaptureFixture[str], pool: Pool, config: str
) -> None:
    job_id = add_job(capsys, config=config)
    workers = [cli(pool, config, "cancel", job_id) for _ in range(6)]
    pool.go()
    results = [w.result() for w in workers]
    assert [r["exit"] for r in results] == [0] * 6
    assert sum(changed(r) for r in results) == 1
    assert len(outbox()) == 1 and len(log_records("transition")) == 1


def test_hold_and_cancel_racing_end_in_a_state_the_table_allows(
    capsys: pytest.CaptureFixture[str], pool: Pool, config: str
) -> None:
    job_id = add_job(capsys, config=config)
    holder = cli(pool, config, "hold", job_id)
    canceler = cli(pool, config, "cancel", job_id)
    pool.go()
    hold_result, cancel_result = holder.result(), canceler.result()
    assert cancel_result["exit"] == 0 and changed(cancel_result)
    assert row(job_id)["status"] == "canceled"
    transitions = [(r["from"], r["to"]) for r in log_records("transition")]
    if hold_result["exit"] == 0 and changed(hold_result):  # hold first, then cancel of held
        assert transitions == [("queued", "held"), ("held", "canceled")]
        assert [a["action"] for a in audit_records()] == ["cancel_held"]
    else:  # cancel first: hold is a no-op on a terminal job
        assert transitions == [("queued", "canceled")] and audit_records() == []


def test_hold_and_release_racing_leave_a_consistent_job(
    capsys: pytest.CaptureFixture[str], pool: Pool, config: str
) -> None:
    ids = [add_job(capsys, config=config) for _ in range(4)]
    workers = []
    for job_id in ids:
        workers.append(cli(pool, config, "hold", job_id))
        workers.append(cli(pool, config, "release", job_id))
    pool.go()
    for w in workers:
        assert w.result()["exit"] in (0, 20)  # release of a not yet held job is a queued no-op
    for job_id in ids:
        assert row(job_id)["status"] in ("queued", "held")
    # every logged transition chains: each starts where the previous one ended
    last: dict[str, str] = {}
    for record in log_records("transition"):
        assert record["from"] == last.get(record["job_id"], "queued")
        last[record["job_id"]] = record["to"]
    for job_id in ids:
        assert last.get(job_id, "queued") == row(job_id)["status"]


def test_cancel_all_racing_a_new_claim_never_skips_or_double_cancels(
    capsys: pytest.CaptureFixture[str], pool: Pool, config: str
) -> None:
    ids = [add_job(capsys, "--label", "a=1", config=config) for _ in range(5)]
    canceler = cli(pool, config, "cancel", "--label", "a=1", "--all")
    claimers = [pool.spawn("claim", job_id) for job_id in ids]
    pool.go()
    result = canceler.result()
    for c in claimers:
        c.result()
    assert result["exit"] in (0, 20)
    for job_id in ids:
        assert row(job_id)["status"] in ("canceled", "starting")
    # a claim that won before the cancel selected the job was canceled as `starting`; a claim
    # that came after found `canceled` and did nothing. Nothing is left running or doubled.
    assert len(outbox()) == len({e["job_id"] for e in outbox()})
