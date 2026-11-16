"""`hold` and `release` (interface.md, hold / release, Hold reasons, Status transitions)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsctl.commands import hold_release as hr_module
from jobsctl.env_file import load_env_file
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import run_cli
from tests.manual_helpers import (
    HUMAN,
    add_job,
    attempt_row,
    audit_records,
    change,
    config_file,
    hold_cli,
    log_records,
    make_done,
    make_held,
    make_running,
    make_starting,
    outbox,
    refusal,
    release_cli,
    row,
    sql,
    table_dump,
)

pytestmark = pytest.mark.usefixtures("validated_records")

HOOKS = {"hooks": {"post-held": [{"cmd": "true"}]}}


@pytest.fixture(autouse=True)
def trusted_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = load_env_file
    monkeypatch.setattr(
        hr_module, "load_env_file", lambda path: real(path, check_ancestors_from=tmp_path)
    )


# -- hold ------------------------------------------------------------------------------------


def test_hold_makes_a_queued_job_held_manual(capsys: pytest.CaptureFixture[str]) -> None:
    first = add_job(capsys)
    second = add_job(capsys)
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z' WHERE id = ?", first)
    before = row(first)
    doc = change(hold_cli(capsys, first, "--actor", "human:kim", "--decision-id", "d1"))
    job = doc["job"]
    assert doc["changed"] is True and doc["dry_run"] is False
    assert job["status"] == "held" and job["hold_reason"] == "manual" and job["rank"] is None
    assert job["reason"] is None and job["blocked_by"] is None
    assert job["not_before"] == "2999-01-01T00:00:00.000Z"  # a hold keeps not_before
    assert job["exit"] is None and job["claimed_at"] is None  # a manual hold has no attempt
    after = row(first)
    assert after["position"] == before["position"] and after["priority"] == before["priority"]
    assert after["enqueued_at"] == before["enqueued_at"]
    assert after["status_changed_at"] >= before["status_changed_at"]
    assert row(second)["status"] == "queued"
    (record,) = log_records("transition")
    assert record["job_id"] == first and record["from"] == "queued" and record["to"] == "held"
    assert record["hold_reason"] == "manual" and record["reason"] is None
    assert record["actor"] == "human:kim" and record["decision_ids"] == ["d1"]
    assert record["attempt"] == 0 and "attempt_status" not in record
    assert audit_records() == []  # hold is not audited


def test_hold_is_idempotent_and_a_noop_on_terminal_jobs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    held = add_job(capsys)
    done = add_job(capsys)
    assert change(hold_cli(capsys, held))["changed"] is True
    again = change(hold_cli(capsys, held))
    assert again["changed"] is False and again["job"]["status"] == "held"
    make_done(done)
    noop = change(hold_cli(capsys, done))
    assert noop["changed"] is False and noop["job"]["status"] == "done"
    assert len(log_records("transition")) == 1


@pytest.mark.parametrize("make", [make_starting, make_running])
def test_hold_of_a_live_job_is_refused(capsys: pytest.CaptureFixture[str], make) -> None:  # type: ignore[no-untyped-def]
    job_id = add_job(capsys)
    make(job_id)
    refusal(hold_cli(capsys, job_id), "live")
    assert row(job_id)["status"] in ("starting", "running")
    assert outbox() == []


def test_hold_resolves_prefixes_and_reports_lookup_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    assert change(hold_cli(capsys, job_id[:8]))["job"]["id"] == job_id  # jb- plus 5
    assert change(hold_cli(capsys, job_id[3:8]))["changed"] is False  # jb- is optional
    unknown = hold_cli(capsys, "jb-zzzzzz22")
    assert unknown.code == 10 and unknown.envelope()["error"]["code"] == "not_found"
    assert hold_cli(capsys, "jb-ab").code == 2  # too short
    assert hold_cli(capsys, "JB-ABCDEFGH").code == 2


def test_hold_of_an_ambiguous_prefix_exits_21(capsys: pytest.CaptureFixture[str]) -> None:
    ids = [add_job(capsys) for _ in range(3)]
    for index, job_id in enumerate(ids):  # force a shared prefix
        sql("UPDATE jobs SET id = ? WHERE id = ?", f"jb-aaaab{'cde'[index]}xy", job_id)
    res = hold_cli(capsys, "jb-aaaab")
    assert res.code == 21
    error = res.envelope()["error"]
    assert error["code"] == "ambiguous" and len(error["candidates"]) == 3


def test_hold_with_a_missing_database_is_not_found_and_creates_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = hold_cli(capsys, "jb-abcd2345", "--dry-run")
    assert res.code == 10
    assert not resolve_state_dir().exists()


def test_hold_dry_run_projects_and_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    before = table_dump()
    doc = change(hold_cli(capsys, job_id, "--dry-run"))
    assert doc["dry_run"] is True and doc["changed"] is True
    assert doc["job"]["status"] == "held" and doc["job"]["hold_reason"] == "manual"
    assert table_dump() == before
    assert log_records("transition") == [] and outbox() == []
    held = change(hold_cli(capsys, job_id))["job"]
    assert held["status"] == "held"
    noop = change(hold_cli(capsys, job_id, "--dry-run"))
    assert noop["changed"] is False and noop["dry_run"] is True


def test_hold_writes_a_post_held_event_in_the_same_transaction(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, **HOOKS)
    job_id = add_job(capsys, "--label", "team=a", "--env", "K=v", "--env", "A=b", config=cfg)
    res = run_cli(capsys, "--config", cfg, "--json", "hold", job_id)
    assert res.code == 0
    (event,) = outbox()
    assert event["job_id"] == job_id and event["hook"] == "post-held"
    assert event["delivered_at"] is None and event["async"] == 0
    payload = event["payload"]
    validate_against("hooks/post-held.json", payload)
    assert payload["event_id"] == event["event_id"]
    assert payload["previous_status"] == "queued" and payload["attempt"] is None
    assert payload["labels"] == ["team=a"] and payload["job"]["status"] == "held"
    assert payload["job"]["env_names"] == ["A", "K"] and "env" not in payload["job"]
    assert payload["output_available"] is False and payload["output_tail"] is None


def test_hook_selectors_decide_whether_an_event_is_written(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(
        tmp_path,
        hooks={
            "post-held": [
                {"name": "a", "cmd": "true", "queues": ["other"]},
                {"name": "b", "cmd": "true", "exclude_queues": ["default"]},
                {"name": "c", "cmd": "true", "labels": ["team=b"]},
            ]
        },
    )
    job_id = add_job(capsys, "--label", "team=a", config=cfg)
    run_cli(capsys, "--config", cfg, "--json", "hold", job_id)
    assert outbox() == []  # no entry selects this job
    other = add_job(capsys, "--label", "team=b", config=cfg)
    run_cli(capsys, "--config", cfg, "--json", "hold", other)
    assert [e["job_id"] for e in outbox()] == [other]


def test_async_hooks_mark_the_event_async(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, hooks={"post-held": [{"cmd": "true", "async": True}]})
    job_id = add_job(capsys, config=cfg)
    run_cli(capsys, "--config", cfg, "--json", "hold", job_id)
    assert outbox()[0]["async"] == 1


def test_a_mixed_sync_and_async_list_writes_one_event_per_kind(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(
        tmp_path,
        hooks={
            "post-held": [
                {"name": "a", "cmd": "true", "async": True},
                {"name": "b", "cmd": "true"},
                {"name": "c", "cmd": "true", "async": True, "queues": ["other"]},
            ]
        },
    )
    job_id = add_job(capsys, config=cfg)
    assert run_cli(capsys, "--config", cfg, "--json", "hold", job_id).code == 0
    sync_event, async_event = outbox()
    assert (sync_event["async"], async_event["async"]) == (0, 1)
    assert sync_event["event_id"] != async_event["event_id"]
    assert sync_event["event_id"].startswith(f"{job_id}:post-held:")
    for event in (sync_event, async_event):
        assert event["payload"]["event_id"] == event["event_id"]
        validate_against("hooks/post-held.json", event["payload"])


# -- release ---------------------------------------------------------------------------------


def test_release_of_a_manual_hold_restores_the_previous_rank(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = [add_job(capsys) for _ in range(3)]
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z', auto_retries = 2, "
        "spawn_failures = 2 WHERE id = ?", ids[0])  # fmt: skip
    hold_cli(capsys, ids[0])
    before = row(ids[0])
    doc = change(release_cli(capsys, ids[0]))
    job = doc["job"]
    assert job["status"] == "queued" and job["rank"] == 1 and job["hold_reason"] is None
    assert job["not_before"] is None and job["blocked_by"] is None
    after = row(ids[0])
    assert after["position"] == before["position"]
    assert after["enqueued_at"] >= before["enqueued_at"] and after["enqueued_at"] != ""
    assert after["auto_retries"] == 0 and after["spawn_failures"] == 0
    records = log_records("transition")
    assert records[-1]["from"] == "held" and records[-1]["to"] == "queued"
    assert records[-1]["hold_reason"] is None and "attempt_status" not in records[-1]
    assert audit_records() == []  # a plain release is not audited
    assert outbox() == []


def test_release_resets_enqueued_at_and_status_changed_at(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    hold_cli(capsys, job_id)
    sql("UPDATE jobs SET enqueued_at = '2020-01-01T00:00:00.000Z' WHERE id = ?", job_id)
    release_cli(capsys, job_id)
    assert row(job_id)["enqueued_at"] > "2020-01-01T00:00:00.000Z"
    assert row(job_id)["status_changed_at"] == row(job_id)["enqueued_at"]


@pytest.mark.parametrize("flags", [[], ["--override-escalation"]])
def test_release_of_a_queued_job_is_a_noop_without_a_human_check(
    capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    job_id = add_job(capsys)
    doc = change(release_cli(capsys, job_id, *flags))  # no TTY, no human actor: still exit 0
    assert doc["changed"] is False and doc["job"]["status"] == "queued"
    assert log_records("transition") == [] and audit_records() == []


@pytest.mark.parametrize("make", [make_starting, make_running, make_done])
def test_release_of_a_job_that_is_not_held_is_refused(
    capsys: pytest.CaptureFixture[str],
    make,  # type: ignore[no-untyped-def]
) -> None:
    job_id = add_job(capsys)
    make(job_id)
    refusal(release_cli(capsys, job_id), "not_held")
    refusal(release_cli(capsys, job_id, "--override-escalation", *HUMAN), "not_held")


def test_exit_13_hold_is_not_releasable_and_not_escalated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    refusal(release_cli(capsys, job_id), "not_releasable")
    refusal(release_cli(capsys, job_id, "--override-escalation", *HUMAN), "not_escalated")
    assert row(job_id)["status"] == "held"


def test_manual_hold_with_the_override_flag_is_not_escalated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    hold_cli(capsys, job_id)
    # the refusal comes before the human check: no human actor is needed to be refused
    refusal(release_cli(capsys, job_id, "--override-escalation"), "not_escalated")


def test_escalated_hold_needs_the_override_flag_and_a_human(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated", ("gate", "lint"))
    refusal(release_cli(capsys, job_id), "override_required")
    refusal(release_cli(capsys, job_id, *HUMAN), "override_required")
    needs_human = release_cli(capsys, job_id, "--override-escalation")
    assert needs_human.code == 13 and needs_human.envelope()["error"]["code"] == "needs_human"
    yes = release_cli(capsys, job_id, "--override-escalation", "--yes")
    assert yes.code == 13  # --yes never satisfies the gate
    auto = release_cli(capsys, job_id, "--override-escalation", "--actor", "auto:bot")
    assert auto.code == 13
    assert row(job_id)["status"] == "held" and audit_records() == []


def test_override_release_requeues_and_records_the_escalating_hooks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    other = add_job(capsys)
    make_held(job_id, "escalated", ("gate", "lint"))
    doc = change(
        release_cli(capsys, job_id, "--override-escalation", *HUMAN, "--decision-id", "d9")
    )
    job = doc["job"]
    assert job["status"] == "queued" and job["rank"] == 1 and job["attempt"] == 1
    assert job["override_hooks"] == ["gate", "lint"] and job["hold_reason"] is None
    assert job["exit"] is None and job["claimed_at"] is None  # queued: no current attempt
    assert row(other)["status"] == "queued"
    held_attempt = attempt_row(job_id)
    assert held_attempt["status"] == "held" and held_attempt["hold_reason"] == "escalated"
    (audit,) = audit_records()
    assert audit["action"] == "release_override" and audit["target"] == job_id
    assert audit["actor"] == "human:kim" and audit["decision_id"] == ["d9"]
    assert audit["result"] == "queued"
    assert audit["inputs"]["attempt"] == 1 and audit["inputs"]["queue"] == "default"
    assert audit["inputs"]["unique_key"] is None and len(audit["inputs"]["argv_sha256"]) == 64
    transition = log_records("transition")[-1]
    assert transition["from"] == "held" and transition["to"] == "queued"
    assert transition["attempt"] == 1 and "attempt_status" not in transition
    # a repeated release is a no-op and writes no second audit record
    again = change(release_cli(capsys, job_id, "--override-escalation"))
    assert again["changed"] is False and len(audit_records()) == 1


def test_a_tty_satisfies_the_human_gate(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated", ("gate",))
    monkeypatch.setattr("jobsctl.cli._stdin_isatty", lambda: True)
    doc = change(release_cli(capsys, job_id, "--override-escalation"))
    assert doc["job"]["status"] == "queued"
    assert audit_records()[0]["actor"].startswith("human:")


def test_release_dry_run_keeps_the_checks_and_changes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated", ("gate",))
    before = table_dump()
    assert release_cli(capsys, job_id, "--override-escalation", "--dry-run").code == 13
    refusal(release_cli(capsys, job_id, "--dry-run"), "override_required")
    doc = change(release_cli(capsys, job_id, "--override-escalation", *HUMAN, "--dry-run"))
    assert doc["dry_run"] is True and doc["changed"] is True
    assert doc["job"]["status"] == "queued" and doc["job"]["override_hooks"] == ["gate"]
    assert table_dump() == before
    assert audit_records() == [] and log_records("transition") == []


def test_release_reports_blocked_by_for_a_job_that_cannot_start(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, queues={"default": {"cap": 0}})
    job_id = add_job(capsys, config=cfg)
    run_cli(capsys, "--config", cfg, "--json", "hold", job_id)
    res = run_cli(capsys, "--config", cfg, "--json", "release", job_id)
    assert change(res)["job"]["blocked_by"] == "queue_paused"


def test_release_of_a_job_with_pass_env_reports_an_unavailable_env_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env_path = tmp_path / "missing.env"
    cfg = config_file(tmp_path, env_file=str(env_path))
    job_id = add_job(capsys, "--pass-env", "TOK", config=cfg)
    run_cli(capsys, "--config", cfg, "--json", "hold", job_id)
    doc = change(run_cli(capsys, "--config", cfg, "--json", "release", job_id))
    assert doc["job"]["blocked_by"] == "env_file_unavailable"


def test_release_keeps_the_event_log_valid_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    hold_cli(capsys, job_id)
    release_cli(capsys, job_id)
    for line in (resolve_state_dir() / "log.jsonl").read_text().splitlines():
        json.loads(line)


def test_unusable_audit_file_fails_an_override_release_before_it_commits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated", ("gate",))
    (resolve_state_dir() / "audit.jsonl").mkdir()
    res = release_cli(capsys, job_id, "--override-escalation", *HUMAN)
    assert res.code != 0
    assert row(job_id)["status"] == "held" and log_records("transition") == []
