"""`wait` and `add --wait` (interface.md, wait, add): exit codes, timeout, interrupts, pruning."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.manual_helpers import (
    add_job,
    config_file,
    make_done,
    make_held,
    row,
    sql,
)

pytestmark = pytest.mark.usefixtures("validated_records")


class Clock:
    """A fake monotonic clock; `sleep` advances it and runs the test's hook for that tick."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.hook: Callable[[int], None] = lambda tick: None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.hook(len(self.sleeps))

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(gateway, "sleep", c.sleep)
    monkeypatch.setattr(gateway, "monotonic", c.monotonic)
    return c


def wait(capsys: pytest.CaptureFixture[str], *args: str) -> Result:
    return run_cli(capsys, "--json", "wait", *args)


def shown(res: Result, code: int, *, validate: bool = True) -> dict[str, Any]:
    """The printed job. `validate=False` for rows whose hand-built attempts are not realistic
    enough for the schema's cross-field rules (only the exit code and status are under test)."""
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    if validate:
        validate_against("cli/show.json", doc)
    return doc


@pytest.mark.parametrize(("status", "code"), [("done", 0), ("failed", 12), ("canceled", 20)])
def test_a_terminal_job_returns_at_once_with_its_exit_code(
    capsys: pytest.CaptureFixture[str], clock: Clock, status: str, code: int
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, status)
    if status == "canceled":
        sql("UPDATE jobs SET reason = 'canceled' WHERE id = ?", job_id)
        sql("UPDATE attempts SET reason = 'canceled' WHERE job_id = ?", job_id)
    doc = shown(wait(capsys, job_id), code, validate=status != "canceled")
    assert doc["status"] == status and doc["id"] == job_id
    assert doc["attempts"][0]["status"] == status
    assert clock.sleeps == []


@pytest.mark.parametrize("reason", ["canceled", "forced", "vetoed", "resolved"])
def test_canceled_reasons_all_exit_20_and_resolved_follows_its_status(
    capsys: pytest.CaptureFixture[str], clock: Clock, reason: str
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "canceled")
    sql("UPDATE jobs SET reason = ? WHERE id = ?", reason, job_id)
    sql("UPDATE attempts SET reason = ? WHERE job_id = ?", reason, job_id)
    doc = shown(wait(capsys, job_id), 20, validate=False)
    assert doc["reason"] == reason


@pytest.mark.parametrize("status", ["done", "failed"])
def test_a_resolved_job_exits_by_the_status_it_was_resolved_as(
    capsys: pytest.CaptureFixture[str], clock: Clock, status: str
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, status)
    sql("UPDATE jobs SET reason = 'resolved', resolved_by = 'human:x' WHERE id = ?", job_id)
    shown(wait(capsys, job_id), {"done": 0, "failed": 12}[status], validate=False)


@pytest.mark.parametrize("hold", ["manual", "exit_13", "escalated"])
def test_a_held_job_exits_13_at_once(
    capsys: pytest.CaptureFixture[str], clock: Clock, hold: str
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, hold)
    doc = shown(wait(capsys, job_id), 13, validate=hold != "escalated")
    assert doc["hold_reason"] == hold and clock.sleeps == []


def test_a_job_that_ends_while_waiting_ends_the_wait(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    clock.hook = lambda tick: make_done(job_id, "failed") if tick == 3 else None
    shown(wait(capsys, job_id), 12)
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_running_jobs_with_output_are_printed_with_the_output_when_they_end(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    doc = shown(wait(capsys, job_id), 13)
    assert doc["output"]["tail"] == "out"


def test_a_job_waiting_for_its_automatic_retry_is_not_terminal(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    sql(
        "UPDATE jobs SET status = 'queued', reason = NULL, ended_at = NULL, "
        "not_before = '2099-01-01T00:00:00.000Z' WHERE id = ?",
        job_id,
    )

    def finish(tick: int) -> None:
        if tick == 5:
            sql(
                "UPDATE jobs SET status = 'done', reason = 'exit', not_before = NULL, "
                "ended_at = '2026-01-01T00:00:09.000Z', attempt = 2 WHERE id = ?",
                job_id,
            )

    clock.hook = finish
    doc = shown(wait(capsys, job_id), 0, validate=False)
    assert len(clock.sleeps) == 5 and doc["status"] == "done"


def test_the_timeout_prints_the_current_job_and_exits_22(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    doc = shown(wait(capsys, job_id, "--timeout", "3s"), 22)
    assert doc["status"] == "queued"
    assert sum(clock.sleeps) == 3.0 and max(clock.sleeps) <= 1.0
    assert "error" not in doc


def test_a_job_that_ends_exactly_at_the_deadline_is_reported_not_timed_out(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    clock.hook = lambda tick: make_done(job_id) if tick == 2 else None
    shown(wait(capsys, job_id, "--timeout", "2s"), 0)


def test_an_unknown_job_is_not_found_and_creates_no_state(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = wait(capsys, "jb-aaaaaaaa")
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"
    assert not resolve_state_dir().exists()


def test_a_job_pruned_while_waiting_is_not_found(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    clock.hook = lambda tick: sql("DELETE FROM jobs WHERE id = ?", job_id)
    res = wait(capsys, job_id)
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"


def test_a_malformed_id_is_a_usage_error(capsys: pytest.CaptureFixture[str], clock: Clock) -> None:
    assert wait(capsys, "jb-1").code == 2


def test_a_schema_upgrade_underneath_a_wait_is_version_skew(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)

    def upgrade(tick: int) -> None:
        conn = sqlite3.connect(resolve_state_dir() / "state.db")
        conn.execute("PRAGMA user_version = 99")
        conn.close()

    clock.hook = upgrade
    res = wait(capsys, job_id)
    assert res.code == 12 and res.envelope()["error"]["code"] == "version_skew"


def test_ctrl_c_prints_the_job_exits_130_and_leaves_it_queued(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)

    def interrupt(tick: int) -> None:
        raise KeyboardInterrupt

    clock.hook = interrupt
    doc = shown(wait(capsys, job_id), 130)
    assert doc["status"] == "queued" and row(job_id)["status"] == "queued"


def test_a_corrupt_stored_tail_still_gives_the_exit_code(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = x'ff', compressed = 1 WHERE job_id = ?", job_id)
    res = wait(capsys, job_id)
    doc = shown(res, 13)
    assert doc["output"] is None and "cannot be read" in res.err


def test_human_output_names_the_job_and_status_and_sanitizes_the_tail(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = ? WHERE job_id = ?", b"hi \x1b[31mred\n", job_id)
    res = run_cli(capsys, "wait", job_id)
    assert res.code == 13
    assert job_id in res.out and "held" in res.out and "\x1b" not in res.out


# -- add --wait ---------------------------------------------------------------------------


def add_wait(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(
        capsys, "--json", "add", "--no-start", "--wait", *flags, "--cwd", "/work", "--", "x"
    )


def test_add_wait_creates_the_job_first_then_waits_and_embeds_the_detail(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    def finish(tick: int) -> None:
        job_id = sql_one("SELECT id FROM jobs")
        if tick == 2:
            make_done(job_id, "failed")

    clock.hook = finish
    res = add_wait(capsys)
    assert res.code == 12, res.out + res.err
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["created"] is True and doc["waited"] is True
    assert doc["job"]["status"] == "failed" and doc["job"]["attempts"]


def sql_one(statement: str) -> str:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    try:
        return str(conn.execute(statement).fetchone()[0])
    finally:
        conn.close()


def test_add_wait_ctrl_c_leaves_the_job_queued_and_reports_its_id(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    def interrupt(tick: int) -> None:
        raise KeyboardInterrupt

    clock.hook = interrupt
    res = add_wait(capsys)
    assert res.code == 130
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["job"]["status"] == "queued" and doc["waited"] is True
    assert row(doc["job"]["id"])["status"] == "queued"


def test_add_wait_timeout_exits_22_with_the_job(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = add_wait(capsys, "--wait-timeout", "2s")
    assert res.code == 22
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["job"]["status"] == "queued"


def test_add_wait_on_a_held_unique_key_exits_13_at_once(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys, "--unique-key", "k")
    make_held(job_id, "manual")
    res = add_wait(capsys, "--unique-key", "k")
    assert res.code == 13
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["created"] is False and doc["job"]["id"] == job_id
    assert doc["warnings"][0]["code"] == "existing_job_held"


def test_add_wait_usage_errors_still_come_first_and_write_nothing(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = run_cli(capsys, "--json", "add", "--wait", "--dry-run", "--", "x")
    assert res.code == 2 and not resolve_state_dir().exists()
    res = run_cli(capsys, "--json", "add", "--wait-timeout", "1s", "--", "x")
    assert res.code == 2 and not resolve_state_dir().exists()


def test_add_wait_human_prints_the_final_status(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = run_cli(
        capsys, "add", "--no-start", "--wait", "--wait-timeout", "1s", "--cwd", "/w", "--", "x"
    )
    assert res.code == 22 and "queued" in res.out and "jb-" in res.out


# -- real processes -----------------------------------------------------------------------


def _spawn_wait(job_id: str, tmp_path: Path, *extra: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    cfg = config_file(tmp_path, auto_run=False)
    return subprocess.Popen(
        [sys.executable, "-I", "-m", "jobsctl", "--config", cfg, "--json", "wait", job_id, *extra],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_waiting_process_that_gets_a_signal_prints_the_job_and_exits_130(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sig: signal.Signals
) -> None:
    job_id = add_job(capsys)
    proc = _spawn_wait(job_id, tmp_path)  # tests/kill_guard allows signals to a Popen's pid
    try:
        time.sleep(1.5)  # let it enter the poll loop
        assert proc.poll() is None
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == 130, out + err
    doc = json.loads(out)
    validate_against("cli/show.json", doc)
    assert doc["id"] == job_id and doc["status"] == "queued"
    assert "Traceback" not in err
    assert row(job_id)["status"] == "queued"


def test_a_real_wait_sees_the_job_end(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    job_id = add_job(capsys)
    proc = _spawn_wait(job_id, tmp_path)
    try:
        time.sleep(1.0)
        make_done(job_id)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == 0, out + err
    assert json.loads(out)["status"] == "done"
