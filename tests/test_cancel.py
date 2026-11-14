"""`cancel` of jobs that are not running, and its selectors (interface.md, cancel, Selectors)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.manual_helpers import (
    HUMAN,
    add_job,
    attempt_row,
    audit_records,
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


def cancel(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(capsys, "--json", "cancel", *flags)


def done(res: Result, *, code: int = 0) -> dict[str, Any]:
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    return doc


def one(res: Result) -> dict[str, Any]:
    doc = done(res)
    assert len(doc["jobs"]) == 1
    return doc


# -- by id -----------------------------------------------------------------------------------


def test_cancel_of_a_queued_job(capsys: pytest.CaptureFixture[str], tmp_path: Any) -> None:
    cfg = config_file(tmp_path, hooks={"post-cancel": [{"cmd": "true"}]})
    job_id = add_job(capsys, "--label", "a=1", config=cfg)
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z', override_hooks = '[\"h\"]'")
    res = run_cli(
        capsys, "--config", cfg, "--json", "cancel", job_id, "--actor", "human:kim",
        "--decision-id", "d1",
    )  # fmt: skip
    doc = one(res)
    job = doc["jobs"][0]
    assert doc["changed"] is True and doc["dry_run"] is False and doc["refused"] == []
    assert job["status"] == "canceled" and job["reason"] == "canceled" and job["rank"] is None
    assert job["ended_at"] is not None and job["not_before"] is None
    assert job["override_hooks"] == [] and job["hold_reason"] is None
    stored = row(job_id)
    assert stored["ended_at"] == stored["status_changed_at"]
    (record,) = log_records("transition")
    assert record["from"] == "queued" and record["to"] == "canceled"
    assert record["reason"] == "canceled" and record["hold_reason"] is None
    assert record["actor"] == "human:kim" and record["decision_ids"] == ["d1"]
    assert audit_records() == []  # a queued cancel is not audited
    (event,) = outbox()
    validate_against("hooks/post-cancel.json", event["payload"])
    assert event["hook"] == "post-cancel" and event["payload"]["previous_status"] == "queued"
    assert event["payload"]["attempt"] is None and event["payload"]["job"]["status"] == "canceled"


def test_cancel_of_a_queued_job_that_failed_before_leaves_the_old_attempt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    sql("UPDATE jobs SET status = 'queued', hold_reason = NULL WHERE id = ?", job_id)
    sql("UPDATE attempts SET status = 'failed', reason = 'exit', hold_reason = NULL")
    doc = one(cancel(capsys, job_id))
    assert doc["jobs"][0]["status"] == "canceled"
    assert attempt_row(job_id)["status"] == "failed"  # a queued cancel involves no attempt
    assert outbox() == [] or outbox()[0]["payload"]["attempt"] is None


def test_cancel_of_a_manual_hold_is_audited(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys, "--unique-key", "K1")
    make_held(job_id, "manual")
    res = cancel(capsys, job_id, *HUMAN, "--decision-id", "d2")
    doc = one(res)
    assert doc["jobs"][0]["status"] == "canceled" and doc["jobs"][0]["hold_reason"] is None
    (audit,) = audit_records()
    assert audit["action"] == "cancel_held" and audit["target"] == job_id
    assert audit["actor"] == "human:kim" and audit["decision_id"] == ["d2"]
    assert audit["result"] == "canceled"
    assert audit["inputs"]["unique_key"] == "K1" and audit["inputs"]["attempt"] == 0
    (record,) = log_records("transition")
    assert record["from"] == "held" and record["to"] == "canceled"
    assert record["reason"] == "canceled" and record["hold_reason"] is None
    # the unique key is free again
    assert row(job_id)["status"] == "canceled"
    new = run_cli(capsys, "--json", "add", "--cwd", "/w", "--unique-key", "K1", "--", "x")
    assert new.envelope()["created"] is True


@pytest.mark.parametrize("hold_reason", ["escalated", "exit_13"])
def test_cancel_of_an_attempt_hold_ends_the_attempt_too(
    capsys: pytest.CaptureFixture[str], hold_reason: str, tmp_path: Any
) -> None:
    cfg = config_file(tmp_path, hooks={"post-cancel": [{"cmd": "true"}]})
    job_id = add_job(capsys, config=cfg)
    make_held(job_id, hold_reason, ("gate",))
    res = run_cli(capsys, "--config", cfg, "--json", "cancel", job_id, *HUMAN)
    doc = one(res)
    job = doc["jobs"][0]
    assert job["status"] == "canceled" and job["reason"] == "canceled"
    attempt = attempt_row(job_id)
    assert attempt["status"] == "canceled" and attempt["reason"] == "canceled"
    assert attempt["hold_reason"] is None
    assert attempt["ended_at"] == "2026-01-01T00:00:02.000Z"  # the hold's end time is kept
    (audit,) = audit_records()
    assert audit["action"] == "cancel_held" and audit["inputs"]["attempt"] == 1
    (record,) = log_records("transition")
    assert record["attempt_status"] == "canceled" and record["attempt_reason"] == "canceled"
    assert record["retry_pending"] is False and record["attempt"] == 1
    payload = outbox()[0]["payload"]
    validate_against("hooks/post-cancel.json", payload)
    assert payload["previous_status"] == "held"
    assert payload["attempt"]["status"] == "canceled" and payload["attempt"]["attempt"] == 1
    assert payload["output_available"] is (hold_reason == "exit_13")
    assert payload["output_tail"] is None


def test_cancel_of_a_starting_job_commits_at_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    cfg = config_file(tmp_path, hooks={"post-cancel": [{"cmd": "true"}]})
    job_id = add_job(capsys, config=cfg)
    make_starting(job_id)
    # --force has no effect on a starting job: a plain cancel, no human check
    res = run_cli(capsys, "--config", cfg, "--json", "cancel", job_id, "--force")
    doc = one(res)
    job = doc["jobs"][0]
    assert doc["changed"] is True and job["status"] == "canceled" and job["reason"] == "canceled"
    assert job["force_requested_by"] is None and job["claimed_at"] is not None
    assert job["exit"] is None
    attempt = attempt_row(job_id)
    assert attempt["status"] == "canceled" and attempt["reason"] == "canceled"
    assert attempt["ended_at"] is not None and attempt["force_requested_by"] is None
    assert audit_records() == []  # not a held job, not forced
    (record,) = log_records("transition")
    assert record["from"] == "starting" and record["attempt_status"] == "canceled"
    assert record["attempt_reason"] == "canceled" and record["retry_pending"] is False
    payload = outbox()[0]["payload"]
    validate_against("hooks/post-cancel.json", payload)
    assert payload["previous_status"] == "starting" and payload["output_available"] is False
    assert payload["attempt"]["status"] == "canceled" and payload["attempt"]["running_at"] is None


def test_cancel_of_a_running_job_is_refused_without_force(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_running(job_id)
    refusal(cancel(capsys, job_id), "live")
    assert row(job_id)["status"] == "running" and outbox() == []


def test_force_on_a_running_job_needs_a_human_after_state_checks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_running(job_id)
    res = cancel(capsys, job_id, "--force")
    assert res.code == 13 and res.envelope()["error"]["code"] == "needs_human"
    assert row(job_id)["status"] == "running"


def test_cancel_of_a_terminal_job_is_a_noop(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    doc = one(cancel(capsys, job_id))
    assert doc["changed"] is False and doc["jobs"][0]["status"] == "failed"
    assert log_records("transition") == [] and outbox() == []
    again = add_job(capsys)
    cancel(capsys, again)
    second = one(cancel(capsys, again))
    assert second["changed"] is False and second["jobs"][0]["status"] == "canceled"
    assert len(log_records("transition")) == 1 and len(outbox()) == 0


def test_cancel_lookup_errors(capsys: pytest.CaptureFixture[str]) -> None:
    add_job(capsys)
    assert cancel(capsys, "jb-zzzzzz22").code == 10
    assert cancel(capsys, "jb-zz").code == 2


def test_cancel_dry_run_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    queued = add_job(capsys)
    starting = add_job(capsys)
    make_starting(starting)
    held = add_job(capsys)
    make_held(held, "exit_13")
    before = table_dump()
    for job_id in (queued, starting, held):
        doc = one(cancel(capsys, job_id, "--dry-run"))
        assert doc["dry_run"] is True and doc["changed"] is True
        assert doc["jobs"][0]["status"] == "canceled" and doc["jobs"][0]["reason"] == "canceled"
    held_doc = one(cancel(capsys, held, "--dry-run"))
    assert held_doc["jobs"][0]["exit"]["code"] == 13  # the projection keeps the attempt's fields
    assert table_dump() == before
    assert audit_records() == [] and log_records("transition") == [] and outbox() == []


def test_cancel_without_a_database_is_not_found_and_creates_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jobsctl.store_dir import resolve_state_dir

    assert cancel(capsys, "jb-abcd2345", "--dry-run").code == 10
    res = cancel(capsys, "--queue", "q", "--all", "--dry-run")
    assert res.code == 0 and done(res)["jobs"] == []
    assert not resolve_state_dir().exists()


# -- selectors -------------------------------------------------------------------------------


def test_selectors_with_no_match_exit_zero_with_an_empty_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_done(job_id)  # selectors match non-terminal jobs only
    for flags in (["--queue", "nope"], ["--label", "a=1"], ["--unique-key", "K"], ["--queue",
                  "default"]):  # fmt: skip
        doc = done(cancel(capsys, *flags))
        assert doc == {"changed": False, "dry_run": False, "jobs": [], "refused": []}


def test_a_single_match_is_canceled_without_all(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys, "--queue", "q1", "--label", "a=1")
    add_job(capsys, "--queue", "q2", "--label", "a=1")
    doc = one(cancel(capsys, "--queue", "q1", "--label", "a=1"))
    assert doc["jobs"][0]["id"] == job_id and doc["changed"] is True


def test_several_matches_are_ambiguous_without_all(capsys: pytest.CaptureFixture[str]) -> None:
    ids = sorted(add_job(capsys, "--label", "a=1") for _ in range(3))
    res = cancel(capsys, "--label", "a=1")
    assert res.code == 21
    validate_against("cli/error.json", res.envelope())
    error = res.envelope()["error"]
    assert error["code"] == "ambiguous" and error["candidates"] == ids
    assert all(row(i)["status"] == "queued" for i in ids)
    assert log_records("transition") == []


def test_all_cancels_every_match_in_one_command(capsys: pytest.CaptureFixture[str]) -> None:
    ids = [add_job(capsys, "--label", "a=1") for _ in range(2)]
    held = add_job(capsys, "--label", "a=1")
    make_held(held, "manual")
    keep = add_job(capsys, "--label", "a=2")
    doc = done(cancel(capsys, "--label", "a=1", "--all", "--actor", "human:kim"))
    assert doc["changed"] is True and doc["refused"] == []
    assert sorted(j["id"] for j in doc["jobs"]) == sorted([*ids, held])
    assert all(j["status"] == "canceled" for j in doc["jobs"])
    assert row(keep)["status"] == "queued"
    assert [a["target"] for a in audit_records()] == [held]  # only the held cancel is audited
    assert len(log_records("transition")) == 3


def test_labels_combine_with_and_and_queue_and_key_narrow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    both = add_job(capsys, "--label", "a=1", "--label", "b=2", "--queue", "q")
    add_job(capsys, "--label", "a=1", "--queue", "q")
    add_job(capsys, "--label", "a=1", "--label", "b=2", "--queue", "other")
    doc = done(cancel(capsys, "--label", "a=1", "--label", "b=2", "--queue", "q", "--all"))
    assert [j["id"] for j in doc["jobs"]] == [both]


def test_unique_key_selector_matches_the_one_live_job(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys, "--unique-key", "K1")
    doc = one(cancel(capsys, "--unique-key", "K1"))
    assert doc["jobs"][0]["id"] == job_id and doc["changed"] is True
    again = add_job(capsys, "--unique-key", "K1")  # the key is free: a new job
    assert again != job_id
    assert one(cancel(capsys, "--unique-key", "K1", "--force"))["jobs"][0]["id"] == again


def test_all_lists_running_jobs_as_refused(capsys: pytest.CaptureFixture[str]) -> None:
    queued = add_job(capsys, "--label", "a=1")
    running = add_job(capsys, "--label", "a=1")
    make_running(running)
    doc = done(cancel(capsys, "--label", "a=1", "--all"))
    assert doc["changed"] is True and [j["id"] for j in doc["jobs"]] == [queued]
    assert doc["refused"] == [{"job_id": running, "reason": "live"}]
    assert row(running)["status"] == "running"


def test_all_exits_20_only_when_nothing_was_canceled_and_prints_the_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    running = add_job(capsys, "--label", "a=1")
    make_running(running)
    res = cancel(capsys, "--label", "a=1", "--all")
    doc = done(res, code=20)
    assert doc["changed"] is False and doc["jobs"] == []
    assert doc["refused"] == [{"job_id": running, "reason": "live"}]


def test_a_single_running_match_without_all_is_an_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    running = add_job(capsys, "--label", "a=1")
    make_running(running)
    refusal(cancel(capsys, "--label", "a=1"), "live")


def test_several_matches_including_running_are_ambiguous(
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_running(add_job(capsys, "--label", "a=1"))
    add_job(capsys, "--label", "a=1")
    assert cancel(capsys, "--label", "a=1").code == 21


def test_selector_dry_run_projects_and_changes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = [add_job(capsys, "--label", "a=1") for _ in range(2)]
    before = table_dump()
    doc = done(cancel(capsys, "--label", "a=1", "--all", "--dry-run"))
    assert doc["dry_run"] is True and doc["changed"] is True
    assert sorted(j["id"] for j in doc["jobs"]) == sorted(ids)
    assert all(j["status"] == "canceled" for j in doc["jobs"])
    assert table_dump() == before


def test_injection_shaped_selector_values_match_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys, "--label", "a=1")
    for flags in (["--queue", "x' OR '1'='1"], ["--unique-key", "'; DROP TABLE jobs;--"]):
        assert cancel(capsys, *flags, "--all").code == 2  # refused by the value grammars
    assert done(cancel(capsys, "--label", "a=2", "--all"))["jobs"] == []
    assert row(job_id)["status"] == "queued"
