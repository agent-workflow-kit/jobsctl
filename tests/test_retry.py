"""`retry` (interface.md, `retry`, Status transitions, Automatic retry's reset)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsctl.commands import retry_resolve as rr_module
from jobsctl.store_db import OpenMode, Store
from tests.helpers import run_cli
from tests.manual_helpers import (
    add_attempt,
    add_job,
    attempt_row,
    audit_records,
    change,
    config_file,
    log_records,
    make_done,
    make_held,
    make_running,
    make_starting,
    outbox,
    refusal,
    row,
    sql,
    table_dump,
)

pytestmark = pytest.mark.usefixtures("validated_records")

HUMAN = ("--actor", "human:kim")
NOWISH = "2026-01-01T00:00:05.000Z"


@pytest.fixture(autouse=True)
def no_passes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(rr_module, "request_pass_from_command", lambda *a, **k: calls.append(1))
    return calls


def retry_cli(capsys: pytest.CaptureFixture[str], *flags: str, config: str | None = None):  # type: ignore[no-untyped-def]
    cfg = ("--config", config) if config else ()
    return run_cli(capsys, *cfg, "--json", "retry", *flags)


@pytest.mark.parametrize("status", ["done", "failed", "canceled"])
def test_a_terminal_job_returns_to_queued_keeping_its_attempts(
    capsys: pytest.CaptureFixture[str], status: str, no_passes: list[int]
) -> None:
    other = add_job(capsys)
    job_id = add_job(capsys)
    make_done(job_id, status)
    sql(
        "UPDATE jobs SET resolved_by = 'human:x', note = 'n', not_before = NULL WHERE id = ?",
        job_id,
    )
    doc = change(retry_cli(capsys, job_id, *HUMAN, "--decision-id", "d1"))
    job = doc["job"]
    assert doc["changed"] is True and job["status"] == "queued"
    assert job["reason"] is None and job["ended_at"] is None and job["attempt"] == 1
    assert job["retried_from_attempt"] == 1 and job["rank"] == 2  # behind the other queued job
    assert job["resolved_by"] is None and job["note"] is None
    assert row(other)["position"] < row(job_id)["position"]
    assert attempt_row(job_id)["status"] == status  # the previous attempt stays as it was
    (record,) = log_records("transition")
    assert (record["from"], record["to"], record["reason"]) == (status, "queued", None)
    assert "attempt_status" not in record and record["decision_ids"] == ["d1"]
    (audit,) = audit_records()
    assert audit["action"] == "retry" and audit["result"] == "queued" and audit["target"] == job_id
    assert "overrides" not in audit["inputs"] and audit["inputs"]["attempt"] == 1
    assert outbox() == [] and no_passes == [1]


def test_top_puts_the_job_before_every_band_and_resets_enqueued_at(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = add_job(capsys)
    pinned = add_job(capsys)
    run_cli(capsys, "--json", "pin", pinned)
    job_id = add_job(capsys)
    make_done(job_id)
    sql("UPDATE jobs SET enqueued_at = '2020-01-01T00:00:00.000Z' WHERE id = ?", job_id)
    change(retry_cli(capsys, job_id, "--top"))
    assert row(job_id)["position"] < min(row(first)["position"], row(pinned)["position"])
    assert row(job_id)["enqueued_at"] > "2020-01-02"


def test_bottom_is_after_a_tied_older_job(capsys: pytest.CaptureFixture[str]) -> None:
    older = add_job(capsys)
    job_id = add_job(capsys)
    make_done(job_id)
    sql("UPDATE jobs SET position = ? WHERE id = ?", row(older)["position"], job_id)  # a tie
    change(retry_cli(capsys, job_id))
    assert row(job_id)["position"] > row(older)["position"]


def test_the_count_and_hooks_restart(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    sql(
        "UPDATE jobs SET auto_retries = 3, spawn_failures = 2, override_hooks = '[\"h\"]' "
        "WHERE id = ?",
        job_id,
    )
    change(retry_cli(capsys, job_id))
    stored = row(job_id)
    assert (stored["auto_retries"], stored["spawn_failures"], stored["override_hooks"]) == (
        0,
        0,
        "[]",
    )


def test_an_exit_13_hold_is_retried_and_the_held_attempt_stays_held(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    doc = change(retry_cli(capsys, job_id))
    assert doc["job"]["status"] == "queued" and doc["job"]["id"] == job_id
    assert doc["job"]["hold_reason"] is None and doc["job"]["exit"] is None
    assert attempt_row(job_id)["status"] == "held"  # the attempt is not rewritten
    assert attempt_row(job_id)["hold_reason"] == "exit_13"
    assert row(job_id)["attempt"] == 1 and row(job_id)["retried_from_attempt"] == 1
    assert outbox() == []


@pytest.mark.parametrize(
    "setup",
    [
        lambda j: None,
        make_starting,
        make_running,
        lambda j: sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", j),
        lambda j: make_held(j, "escalated"),
    ],
    ids=["queued", "starting", "running", "manual_hold", "escalated_hold"],
)
def test_a_live_job_is_refused_not_terminal(capsys: pytest.CaptureFixture[str], setup) -> None:  # type: ignore[no-untyped-def]
    job_id = add_job(capsys)
    setup(job_id)
    before = table_dump()
    refusal(retry_cli(capsys, job_id), "not_terminal")
    assert table_dump() == before and audit_records() == []


def test_a_job_waiting_for_an_automatic_retry_is_not_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z' WHERE id = ?", job_id)
    refusal(retry_cli(capsys, job_id), "not_terminal")


def test_a_unique_key_held_by_a_live_job_refuses_with_its_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys, "--unique-key", "k1")
    make_done(job_id)
    holder = add_job(capsys, "--unique-key", "k1")
    doc = refusal(retry_cli(capsys, job_id), "duplicate_key")
    assert doc["error"]["details"] == {"job_id": holder}
    refusal(retry_cli(capsys, job_id, "--dry-run"), "duplicate_key")
    assert row(job_id)["status"] == "done"
    sql("UPDATE jobs SET status = 'done' WHERE id = ?", holder)  # the key is free again
    sql("UPDATE jobs SET unique_key = NULL WHERE id = ?", holder)
    assert change(retry_cli(capsys, job_id))["changed"] is True


def test_unknown_and_ambiguous_ids(capsys: pytest.CaptureFixture[str]) -> None:
    assert retry_cli(capsys, "jb-aaaaaaaa").code == 10


# -- --attempt ---------------------------------------------------------------------------------


def test_attempt_equal_to_the_last_attempt_retries_then_is_idempotent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    assert change(retry_cli(capsys, job_id, "--attempt", "1"))["changed"] is True
    again = change(retry_cli(capsys, job_id, "--attempt", "1"))
    assert again["changed"] is False and again["job"]["status"] == "queued"
    assert len(audit_records()) == 1 and len(log_records("transition")) == 1


def test_a_noop_attempt_changes_no_override_whatever_the_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    change(retry_cli(capsys, job_id, "--attempt", "1"))
    make_running_again(job_id)
    before = table_dump()
    doc = change(retry_cli(capsys, job_id, "--attempt", "1", "--timeout", "5m"))
    assert doc["changed"] is False and table_dump() == before


def make_running_again(job_id: str) -> None:
    add_attempt(
        job_id, 2, claim_id="c2", status="running", resolved_path="/bin/x", running_at=NOWISH
    )
    sql("UPDATE jobs SET status = 'running', attempt = 2 WHERE id = ?", job_id)


def test_a_later_attempt_makes_an_older_attempt_number_a_noop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    change(retry_cli(capsys, job_id))
    make_running_again(job_id)
    sql(
        "UPDATE attempts SET status = 'failed', reason = 'exit', leftovers = 0, "
        "ended_at = '2026-01-01T00:00:09.000Z', "
        '"exit" = \'{"kind":"exited","code":1,"signal":null,"core":false}\' '
        "WHERE job_id = ? AND n = 2",
        job_id,
    )
    sql(
        "UPDATE jobs SET status = 'failed', reason = 'exit', "
        "ended_at = '2026-01-01T00:00:09.000Z' WHERE id = ?",
        job_id,
    )
    # the job is at attempt 2 and failed; a caller that read attempt 1 must not retry attempt 2
    doc = change(retry_cli(capsys, job_id, "--attempt", "1"))
    assert doc["changed"] is False and row(job_id)["status"] == "failed"
    assert change(retry_cli(capsys, job_id, "--attempt", "2"))["changed"] is True


def test_attempt_above_the_last_is_a_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_done(job_id)
    refusal(retry_cli(capsys, job_id, "--attempt", "2"), "attempt_mismatch")
    assert row(job_id)["status"] == "done"


def test_attempt_beats_not_terminal_only_for_the_noop_cases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)  # queued, attempt 0
    refusal(retry_cli(capsys, job_id, "--attempt", "1"), "attempt_mismatch")
    refusal(retry_cli(capsys, job_id), "not_terminal")


@pytest.mark.parametrize("bad", ["0", "-1", "x"])
def test_attempt_must_be_a_positive_integer(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    assert retry_cli(capsys, "jb-aaaaaaaa", "--attempt", bad).code == 2


# -- overrides ---------------------------------------------------------------------------------


def test_overrides_replace_and_are_audited_after_the_change(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys, "--max-retries", "2", "--timeout", "1m")
    make_done(job_id, "failed")
    doc = change(
        retry_cli(capsys, job_id, "--timeout", "none", "--kill-grace", "7s", "--max-retries", "0")
    )
    expected = {"timeout": "none", "kill_grace": 7, "max_retries": 0}
    assert doc["job"]["overrides"] == expected
    assert doc["job"]["effective"]["timeout"] is None
    assert json.loads(row(job_id)["overrides"]) == expected
    assert audit_records()[0]["inputs"]["overrides"] == expected


def test_a_flag_equal_to_the_current_override_is_not_audited_as_a_change(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys, "--max-retries", "2")
    make_done(job_id, "failed")
    change(retry_cli(capsys, job_id, "--max-retries", "2"))
    assert "overrides" not in audit_records()[0]["inputs"]


def test_overrides_that_were_not_given_are_kept(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys, "--max-retries", "2")
    make_done(job_id, "failed")
    doc = change(retry_cli(capsys, job_id, "--timeout", "30s"))
    assert doc["job"]["overrides"] == {"max_retries": 2, "timeout": 30}


@pytest.mark.parametrize(
    "flags", [("--timeout", "soon"), ("--max-retries", "101"), ("--kill-grace", "none")]
)
def test_bad_override_values_are_usage_errors(
    capsys: pytest.CaptureFixture[str], flags: tuple[str, str]
) -> None:
    assert retry_cli(capsys, "jb-aaaaaaaa", *flags).code == 2


# -- dry run, ordering of checks, signals --------------------------------------------------------


def test_dry_run_projects_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    add_job(capsys)
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    before = table_dump()
    doc = change(retry_cli(capsys, job_id, "--dry-run", "--timeout", "9s", "--top"))
    assert doc["dry_run"] is True and doc["changed"] is True
    assert doc["job"]["status"] == "queued" and doc["job"]["rank"] == 1
    assert doc["job"]["overrides"] == {"timeout": 9}
    assert table_dump() == before and audit_records() == [] and log_records("transition") == []


def test_dry_run_keeps_the_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    refusal(retry_cli(capsys, job_id, "--dry-run"), "not_terminal")


def test_no_pass_is_requested_for_a_noop_or_a_dry_run(
    capsys: pytest.CaptureFixture[str], no_passes: list[int]
) -> None:
    job_id = add_job(capsys)
    make_done(job_id)
    change(retry_cli(capsys, job_id, "--dry-run"))
    assert no_passes == []
    change(retry_cli(capsys, job_id))
    change(retry_cli(capsys, job_id, "--attempt", "1"))
    assert no_passes == [1]


def test_the_retried_job_blocked_by_reports_a_paused_queue(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, queues={"default": {"cap": 0}})
    job_id = add_job(capsys, config=cfg)
    make_done(job_id)
    assert change(retry_cli(capsys, job_id, config=cfg))["job"]["blocked_by"] == "queue_paused"


def test_a_retried_job_can_be_claimed_with_the_next_attempt_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jobsctl.claims import claim_jobs
    from jobsctl.config import Config

    job_id = add_job(capsys)
    make_done(job_id, "failed")
    change(retry_cli(capsys, job_id))
    with Store.open(mode=OpenMode.CLI) as store, store.write() as tx:
        claims, _ = claim_jobs(
            tx,
            Config(),
            now="2999-01-01T00:00:00.000Z",
            claimed_by="pass:1",
            env_file_unavailable=lambda: False,
            limit=None,
        )
    assert [(c.job_id, c.attempt) for c in claims] == [(job_id, 2)]
