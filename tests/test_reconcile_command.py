"""`jobsctl reconcile`, pass step 2 and the reconcile of `cancel --force` (interface.md, Reconcile,
reconcile, Pass, cancel).

Identities of dead processes come from short-lived children this test started and reaped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import cancel_force, gateway
from jobsctl.leases import expiry, identity_text
from jobsctl.proc_host import default_host
from jobsctl.runner import Runner
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_ops import counter_incr, take_lease
from jobsctl.timestamps import format_timestamp
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.manual_helpers import (
    HUMAN,
    add_job,
    attempt_row,
    audit_records,
    db,
    log_records,
    make_done,
    make_running,
    row,
    sql,
    table_dump,
)

pytestmark = pytest.mark.usefixtures("validated_records")


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for popen in started:
        if popen.poll() is None:
            popen.kill()
        popen.wait(timeout=30)


def start(children: list[subprocess.Popen[bytes]]) -> tuple[subprocess.Popen[bytes], str]:
    popen = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(300)"], stdin=subprocess.DEVNULL
    )
    children.append(popen)
    return popen, identity_text(default_host(), popen.pid)


def dead_identity(children: list[subprocess.Popen[bytes]]) -> str:
    popen, ident = start(children)
    popen.kill()
    popen.wait(timeout=30)
    return ident


def reconcile(capsys: pytest.CaptureFixture[str], *flags: str) -> dict[str, Any]:
    res = run_cli(capsys, "--json", "reconcile", *flags)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/reconcile.json", doc)
    return doc


def running_job(
    capsys: pytest.CaptureFixture[str], shim: str, leader: str | None = None, **attempt: Any
) -> str:
    job_id = add_job(capsys)
    make_running(job_id)
    sets = {
        "shim_identity": shim,
        "job_identity": leader,
        "resolved_path": "/bin/x",
        "shim_confirmed_at": "2026-01-01T00:00:01.000Z",
    }
    sets.update(attempt)
    sql(
        "UPDATE attempts SET " + ", ".join(f"{k} = ?" for k in sets) + " WHERE job_id = ?",
        *sets.values(),
        job_id,
    )
    return job_id


def findings(doc: dict[str, Any]) -> list[tuple[str, str]]:
    return [(f["finding"], f["action"]) for f in doc["findings"]]


# -- the command -------------------------------------------------------------------------------


def test_reconcile_on_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    doc = reconcile(capsys)
    assert doc == {"already_running": False, "holder": None, "dry_run": False, "findings": []}


def test_dry_run_without_a_database_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = reconcile(capsys, "--dry-run")
    assert doc["dry_run"] is True and doc["findings"] == []
    from jobsctl.store_dir import resolve_state_dir

    assert not (resolve_state_dir() / "state.db").exists()


def test_a_shim_lost_job_is_marked_and_not_signaled(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    job, job_ident = start(children)
    job_id = running_job(capsys, dead_identity(children), job_ident, pgid=job.pid)
    doc = reconcile(capsys)
    assert findings(doc) == [("shim_lost", "marked_shim_lost")]
    assert doc["findings"][0]["job_id"] == job_id
    assert row(job_id)["shim_lost"] == 1 and row(job_id)["status"] == "running"
    assert job.poll() is None  # never signaled
    assert findings(reconcile(capsys)) == []  # applied exactly once
    assert len(log_records("shim_lost")) == 1


def test_a_job_gone_is_failed_lost(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    job_id = running_job(capsys, dead_identity(children), dead_identity(children))
    assert findings(reconcile(capsys)) == [("job_gone", "failed_lost")]
    assert (row(job_id)["status"], row(job_id)["reason"]) == ("failed", "lost")
    assert any(
        r["finding"] == "job_gone" and r["action"] == "failed_lost"
        for r in log_records("reconcile")
    )


def test_dry_run_reports_and_changes_nothing(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    running_job(capsys, dead_identity(children), dead_identity(children))
    before = table_dump()
    doc = reconcile(capsys, "--dry-run")
    assert doc["dry_run"] is True and findings(doc) == [("job_gone", "failed_lost")]
    assert table_dump() == before  # no lease row, no counter, nothing
    assert findings(reconcile(capsys)) == [("job_gone", "failed_lost")]


def test_a_live_runner_lease_holder_means_already_running(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    _, holder = start(children)
    running_job(capsys, dead_identity(children), dead_identity(children))
    with Store.open(mode=OpenMode.CLI) as store:
        now = gateway.utc_now()
        assert take_lease(
            store,
            "runner",
            holder,
            now=format_timestamp(now),
            expires_at=expiry(now),
            is_alive=lambda h: False,
        )
    for flags in ((), ("--dry-run",)):
        doc = reconcile(capsys, *flags)
        assert doc["already_running"] is True and doc["findings"] == []
        assert doc["holder"]["pid"] == int(holder.split(":")[1])
    assert row(db().execute("SELECT id FROM jobs").fetchone()[0])["status"] == "running"


def test_a_dead_runner_lease_holder_is_taken_over(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    holder = dead_identity(children)
    with Store.open(mode=OpenMode.CLI) as store:
        now = gateway.utc_now()
        take_lease(
            store, "runner", holder, now=format_timestamp(now), expires_at=expiry(now),
            is_alive=lambda h: False,
        )  # fmt: skip
    doc = reconcile(capsys)
    assert doc["already_running"] is False
    assert db().execute("SELECT count(*) FROM leases WHERE name = 'runner'").fetchone()[0] == 0


def test_the_lease_is_released_and_a_pass_requested_after_a_change(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    trigger_spawns: list[dict[str, object]],
) -> None:
    running_job(capsys, dead_identity(children), dead_identity(children))
    reconcile(capsys)
    assert len(trigger_spawns) == 1  # a slot was freed: one pass is spawned at release
    assert db().execute("SELECT count(*) FROM leases WHERE name = 'runner'").fetchone()[0] == 0


def test_no_pass_is_spawned_when_nothing_changed_or_triggers_are_off(
    capsys: pytest.CaptureFixture[str], trigger_spawns: list[dict[str, object]]
) -> None:
    reconcile(capsys)
    assert trigger_spawns == []


def test_a_request_that_arrives_meanwhile_spawns_a_pass(
    capsys: pytest.CaptureFixture[str],
    trigger_spawns: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = Runner._retention

    def with_request(self: Runner, result: Any) -> None:
        with self.store.write() as tx:
            counter_incr(tx, "pass_requested")  # a start trigger that saw the lease held
        real(self, result)

    monkeypatch.setattr(Runner, "_retention", with_request)
    reconcile(capsys)
    assert len(trigger_spawns) == 1


def test_auto_run_false_spawns_nothing(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    trigger_spawns: list[dict[str, object]],
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(json.dumps({"auto_run": False}))
    running_job(capsys, dead_identity(children), dead_identity(children))
    res = run_cli(capsys, "--config", str(cfg), "--json", "reconcile")
    assert res.code == 0 and trigger_spawns == []


def test_retention_and_redelivery_findings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    old = add_job(capsys)
    make_done(old)
    keep = add_job(capsys)
    make_done(keep)
    sql("UPDATE jobs SET ended_at = ? WHERE id = ?", format_timestamp(gateway.utc_now()), keep)
    pending = add_job(capsys)
    make_done(pending, "failed")
    sql("UPDATE jobs SET ended_at = ? WHERE id = ?", format_timestamp(gateway.utc_now()), pending)
    sql(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES ('e1', ?, 'post-fail', '{}', 0, '2026-01-01T00:00:00.000Z')",
        pending,
    )
    dry = reconcile(capsys, "--dry-run")
    assert {(f["job_id"], f["finding"]) for f in dry["findings"]} == {
        (old, "pruned"),
        (pending, "hooks_redelivered"),
    }
    doc = reconcile(capsys)
    assert [(f["finding"], f["action"]) for f in doc["findings"]] == [
        ("hooks_redelivered", "redelivered"),
        ("pruned", "deleted"),
    ]
    assert db().execute("SELECT id FROM jobs WHERE id = ?", [old]).fetchone() is None
    assert db().execute("SELECT id FROM jobs WHERE id = ?", [keep]).fetchone() is not None
    assert {r["finding"] for r in log_records("reconcile")} >= {"pruned", "hooks_redelivered"}


def test_a_job_with_a_live_lease_holder_is_not_redelivered(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    sql("UPDATE jobs SET ended_at = ? WHERE id = ?", format_timestamp(gateway.utc_now()), job_id)
    sql(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES ('e1', ?, 'post-fail', '{}', 0, '2026-01-01T00:00:00.000Z')",
        job_id,
    )
    _, live = start(children)
    with Store.open(mode=OpenMode.CLI) as store:
        now = gateway.utc_now()
        take_lease(
            store, f"job:{job_id}", live, now=format_timestamp(now), expires_at=expiry(now),
            is_alive=lambda h: False,
        )  # fmt: skip
    assert findings(reconcile(capsys)) == []


# -- the pass ----------------------------------------------------------------------------------


def test_a_pass_reconciles_before_it_claims(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    """A lost job frees the only slot in the same pass that then claims the queued one."""
    cfg = {"queues": {"default": {"cap": 1}}, "auto_run": False}
    lost = running_job(capsys, dead_identity(children), dead_identity(children))
    waiting = add_job(capsys)
    with Store.open(mode=OpenMode.CLI) as store:
        from jobsctl.config import Config

        runner = Runner(store, Config.model_validate(cfg), None, python="/bin/true")
        result = runner.run_pass()
        for child in runner.children:
            child.popen.wait(timeout=30)
    assert {"job_id": lost, "finding": "job_gone", "action": "failed_lost"} in result.reconciled
    assert result.started == [waiting]
    validate_against("cli/run.json", result.as_json())


def test_one_failing_job_does_not_abort_the_pass(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobsctl.config import Config
    from jobsctl.reconcile import Reconciler

    bad = running_job(capsys, dead_identity(children), dead_identity(children))
    good = running_job(capsys, dead_identity(children), dead_identity(children))
    real = Reconciler.apply

    def flaky(self: Reconciler, tx: Any, d: Any, now: str) -> Any:
        if d.subject.job_id == bad:
            raise OSError("boom")
        return real(self, tx, d, now)

    monkeypatch.setattr(Reconciler, "apply", flaky)
    with Store.open(mode=OpenMode.CLI) as store:
        runner = Runner(store, Config.model_validate({"auto_run": False}), None)
        result = runner.run_pass()
    assert [f["job_id"] for f in result.reconciled] == [good]
    assert row(bad)["status"] == "running"
    assert db().execute("SELECT count(*) FROM leases WHERE name = 'runner'").fetchone()[0] == 0


def test_a_pass_that_cannot_reconcile_still_claims(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.config import Config
    from jobsctl.errors import JobsctlError

    waiting = add_job(capsys)

    def broken(runner: Any, result: Any) -> None:
        raise JobsctlError("busy", "state.db is busy")

    with Store.open(mode=OpenMode.CLI) as store:
        runner = Runner(
            store,
            Config.model_validate({"auto_run": False}),
            None,
            python="/bin/true",
            reconcile=broken,
        )
        result = runner.run_pass()
        for child in runner.children:
            child.popen.wait(timeout=30)
    assert result.started == [waiting]


def test_a_step_that_fails_with_any_error_does_not_stop_the_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jobsctl.config import Config

    waiting = add_job(capsys)

    def broken(runner: Any, result: Any) -> None:
        raise RecursionError("deep")

    with Store.open(mode=OpenMode.CLI) as store:
        runner = Runner(
            store,
            Config.model_validate({"auto_run": False}),
            None,
            python="/bin/true",
            reconcile=broken,
        )
        result = runner.run_pass()
        for child in runner.children:
            child.popen.wait(timeout=30)
    assert result.started == [waiting]


def test_a_live_deliver_lease_holder_means_no_redelivery_finding_and_no_pass_record(
    capsys: pytest.CaptureFixture[str], children: list[subprocess.Popen[bytes]]
) -> None:
    from jobsctl.config import Config

    job_id = add_job(capsys)
    make_done(job_id, "failed")
    sql("UPDATE jobs SET ended_at = ? WHERE id = ?", format_timestamp(gateway.utc_now()), job_id)
    sql(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES ('e1', ?, 'post-fail', '{}', 0, '2026-01-01T00:00:00.000Z')",
        job_id,
    )
    _, live = start(children)
    with Store.open(mode=OpenMode.CLI) as store:
        now = gateway.utc_now()
        take_lease(
            store, "deliver", live, now=format_timestamp(now), expires_at=expiry(now),
            is_alive=lambda h: False,
        )  # fmt: skip
        runner = Runner(store, Config.model_validate({"auto_run": False}), None)
        result = runner.run_pass()
    assert result.reconciled == [] and runner.children == []
    assert log_records("pass") == [] and log_records("reconcile") == []


# -- cancel --force ----------------------------------------------------------------------------


@pytest.fixture
def spawned_kills(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(cancel_force, "spawn_kill", lambda ctx, job_id: calls.append(job_id))
    return calls


def force(capsys: pytest.CaptureFixture[str], job_id: str, *flags: str) -> Result:
    return run_cli(
        capsys, "--json", "cancel", job_id, "--force", "--wait-timeout", "0", *flags, *HUMAN
    )


def test_force_cancel_reports_a_job_reconcile_finalized_and_requests_nothing(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys, dead_identity(children), dead_identity(children))
    res = force(capsys, job_id)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert doc["changed"] is False
    assert (doc["jobs"][0]["status"], doc["jobs"][0]["reason"]) == ("failed", "lost")
    assert attempt_row(job_id)["force_requested_by"] is None
    assert audit_records() == [] and spawned_kills == []
    assert row(job_id)["status"] == "failed"
    assert any(r["finding"] == "job_gone" for r in log_records("reconcile"))


def test_force_cancel_dry_run_shows_the_finalized_job_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys, dead_identity(children), dead_identity(children))
    before = table_dump()
    res = force(capsys, job_id, "--dry-run")
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert doc["dry_run"] is True and doc["changed"] is False
    assert (doc["jobs"][0]["status"], doc["jobs"][0]["reason"]) == ("failed", "lost")
    assert table_dump() == before and spawned_kills == []


def test_force_cancel_of_a_shim_lost_job_marks_it_then_requests_and_starts_one_kill(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    job, job_ident = start(children)
    job_id = running_job(capsys, dead_identity(children), job_ident, pgid=job.pid)
    res = force(capsys, job_id)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert doc["changed"] is True and doc["jobs"][0]["shim_lost"] is True
    assert attempt_row(job_id)["force_requested_by"] == "human:kim"
    assert spawned_kills == [job_id]  # one `_kill`, from the request's delivery
    assert job.poll() is None


def test_force_cancel_ends_a_lost_job_with_a_request_as_forced(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    job_id = running_job(
        capsys,
        dead_identity(children),
        dead_identity(children),
        force_requested_by="human:lee",
    )
    res = force(capsys, job_id)
    doc = res.envelope()
    assert (doc["jobs"][0]["status"], doc["jobs"][0]["reason"]) == ("canceled", "forced")
    assert doc["changed"] is False and spawned_kills == []


def test_force_cancel_leaves_a_slow_live_shim_alone(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    shim, shim_ident = start(children)
    job, job_ident = start(children)
    job_id = running_job(capsys, shim_ident, job_ident, pgid=job.pid)
    force(capsys, job_id)
    assert row(job_id)["shim_lost"] == 0 and row(job_id)["status"] == "running"


# -- cancel --force dry run projections --------------------------------------------------------


def put_result(job_id: str, **over: Any) -> Path:
    from jobsctl.result_file import ResultRecord, ResultTail, result_name, write_result
    from jobsctl.shim_exit import ExitRecord
    from jobsctl.store_dir import resolve_state_dir

    shims = resolve_state_dir() / "shims"
    values: dict[str, Any] = {
        "job_id": job_id,
        "attempt": 1,
        "claim_id": attempt_row(job_id)["claim_id"],
        "status": "done",
        "reason": "exit",
        "hold_reason": None,
        "exit": ExitRecord("exited", code=0),
        "leftovers": 0,
        "tail": ResultTail("", 0, False),
        "ended_at": "2026-01-01T00:00:05.000Z",
    }
    values.update(over)
    write_result(shims, ResultRecord(**values))
    return shims / result_name(job_id, 1)


RETRY = json.dumps({"timeout": None, "max_retries": 2, "kill_grace": 30})


def test_force_cancel_dry_run_projects_the_requeue_of_a_retryable_loss(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys, dead_identity(children), dead_identity(children), effective=RETRY)
    before = table_dump()
    dry = force(capsys, job_id, "--dry-run").envelope()
    assert table_dump() == before
    projected = dry["jobs"][0]
    assert projected["status"] == "queued" and projected["not_before"] is not None
    real = force(capsys, job_id).envelope()["jobs"][0]
    assert real["status"] == "queued" and real["not_before"] is not None


def test_force_cancel_dry_run_projects_a_forced_end_for_a_result_file(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
) -> None:
    job_id = running_job(
        capsys, dead_identity(children), dead_identity(children), force_requested_by="human:lee"
    )
    put_result(job_id)
    dry = force(capsys, job_id, "--dry-run").envelope()["jobs"][0]
    assert (dry["status"], dry["reason"]) == ("canceled", "forced")
    real = force(capsys, job_id).envelope()["jobs"][0]
    assert (real["status"], real["reason"]) == ("canceled", "forced")


def test_force_cancel_dry_run_survives_a_result_file_that_vanishes(
    capsys: pytest.CaptureFixture[str],
    children: list[subprocess.Popen[bytes]],
    spawned_kills: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobsctl import reconcile as reconcile_module

    job_id = running_job(capsys, dead_identity(children), dead_identity(children))
    path = put_result(job_id, status="failed", reason="exit", exit=_exit(3))
    real_read = reconcile_module.read_result

    def read_then_vanish(*args: Any) -> Any:
        record = real_read(*args)
        path.unlink()
        return record

    monkeypatch.setattr(reconcile_module, "read_result", read_then_vanish)
    res = force(capsys, job_id, "--dry-run")
    assert res.code == 0, res.out + res.err
    assert res.envelope()["jobs"][0]["status"] == "failed"


def _exit(code: int) -> Any:
    from jobsctl.shim_exit import ExitRecord

    return ExitRecord("exited", code=code)
