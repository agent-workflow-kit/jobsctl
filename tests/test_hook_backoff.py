"""Backoff of an event whose hook leader outlived SIGKILL (docs/interface.md, Delivery).

The unkillable leader is simulated by a fake hook runner (a process that survives SIGKILL cannot
be made safely); the clock is a fake the test moves, so nothing sleeps.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jobsctl.config import Config
from jobsctl.deliver import Delivery
from jobsctl.delivery import Deliverer
from jobsctl.errors import JobsctlError
from jobsctl.hook_retry import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    STUCK_ATTEMPTS,
    backoff_seconds,
    record_unkillable,
)
from jobsctl.hook_run import HookResult
from jobsctl.runner import Runner
from jobsctl.store_db import Store
from jobsctl.store_ops import counter_incr
from jobsctl.store_schema import MIGRATIONS
from jobsctl.timestamps import format_timestamp
from jobsctl.triggers import has_undelivered, redeliverable_jobs
from tests.conftest import validate_against
from tests.delivery_helpers import fake_result, open_store, pending
from tests.helpers import run_cli
from tests.manual_helpers import add_job, config_file, log_records, outbox, sql
from tests.test_delivery import _async_cfg, async_events  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.usefixtures("validated_records")

T0 = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """No detached `_deliver` or pass may hold a lease behind a test's back."""
    monkeypatch.setattr("jobsctl.triggers.spawn_internal", lambda *args, **kwargs: None)


class Hook:
    """A fake hook runner: `bad` job ids outlive SIGKILL, `mode` decides what the others do."""

    def __init__(self, bad: set[str], mode: str = "allow") -> None:
        self.bad = bad
        self.mode = mode
        self.calls: list[str] = []

    def __call__(self, **kw: Any) -> HookResult:
        job = kw["env"]["JOBSCTL_ID"]
        self.calls.append(job)
        result = fake_result(kw["hook"], kw["hook_id"])
        if job in self.bad:
            return _replace(result, unkillable=True)
        if self.mode == "failed":
            return _replace(result, outcome="failed", exit=3)
        if self.mode == "timeout":
            return _replace(result, outcome="timeout", exit=None)
        return result


def _replace(result: HookResult, **changes: Any) -> HookResult:
    import dataclasses

    return dataclasses.replace(result, **changes)


def run_round(cfg: Config, hook: Hook, clock: Clock, *, expect_error: bool = False) -> None:
    with open_store() as store:
        d = Deliverer(store, cfg, runner=hook, clock=clock, diag=lambda m: None)
        delivery = Delivery(store, cfg, None, deliverer=d, wait_seconds=0.01, diag=lambda m: None)
        if expect_error:
            with pytest.raises(JobsctlError) as err:
                delivery.run()
            assert err.value.code == "internal"
        else:
            assert delivery.run() == 0


def event_row(job_id: str) -> dict[str, Any]:
    return next(e for e in outbox() if e["job_id"] == job_id)


def test_the_schedule_doubles_from_30_seconds_up_to_one_hour() -> None:
    assert (BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, STUCK_ATTEMPTS) == (30, 3600, 5)
    assert [backoff_seconds(n) for n in range(1, 10)] == [
        30, 60, 120, 240, 480, 960, 1920, 3600, 3600,
    ]  # fmt: skip
    assert backoff_seconds(10**9) == 3600 and backoff_seconds(0) == 30


def test_an_unkillable_async_hook_defers_its_event_with_a_growing_delay(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (bad,) = async_events(capsys, tmp_path, 1, 1)
    cfg, clock, hook = _async_cfg(), Clock(), Hook({"x"})
    hook.bad = {bad}
    for attempts, delay in ((1, 30), (2, 60), (3, 120)):
        run_round(cfg, hook, clock, expect_error=True)
        row = event_row(bad)
        assert row["attempts"] == attempts and row["delivered_at"] is None
        assert row["retry_after"] == format_timestamp(clock.now + dt.timedelta(seconds=delay))
        clock.advance(delay)  # exactly at retry_after: due again
    assert hook.calls == [bad] * 3


def test_a_deferred_event_is_skipped_until_its_time_and_then_delivered(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (bad,) = async_events(capsys, tmp_path, 1, 1)
    cfg, clock = _async_cfg(), Clock()
    hook = Hook({bad})
    run_round(cfg, hook, clock, expect_error=True)
    clock.advance(29.9)
    run_round(cfg, hook, clock)  # skipped: no hook ran and the round does not fail
    assert hook.calls == [bad]
    with open_store() as store, store.read() as tx:
        assert not has_undelivered(tx, _host(), clock.now)
    clock.advance(0.1)
    hook.bad = set()
    run_round(cfg, hook, clock)
    assert hook.calls == [bad, bad]
    row = event_row(bad)
    assert row["delivered_at"] is not None and row["attempts"] == 1  # kept as history


def _host() -> Any:
    from jobsctl.proc_host import default_host

    return default_host()


def test_a_stuck_job_does_not_delay_another_job_or_its_own_later_event(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad, good = async_events(capsys, tmp_path, 2, 2)
    cfg, clock = _async_cfg(), Clock()
    hook = Hook({bad})
    run_round(cfg, hook, clock, expect_error=True)
    assert hook.calls.count(bad) == 1 and hook.calls.count(good) == 2
    clock.advance(10)
    run_round(cfg, hook, clock)  # deferred: the bad job's two events wait, in order
    assert hook.calls.count(bad) == 1
    with open_store() as store:
        assert {e["job_id"] for e in pending(store)} == {bad}
        assert len(pending(store)) == 2


def test_a_deferred_sync_event_is_skipped_by_redelivery_and_its_job_alone(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad, good = async_events(capsys, tmp_path, 2, 1)
    sql("UPDATE hook_outbox SET async = 0")
    cfg = Config.model_validate({"hooks": {"post-held": [{"name": "w", "cmd": "true"}]}})
    clock, hook = Clock(), Hook({bad})
    run_round(cfg, hook, clock, expect_error=True)
    assert event_row(bad)["attempts"] == 1
    with open_store() as store:
        with store.read() as tx:
            assert redeliverable_jobs(tx, _host(), clock.now) == []
            clock.advance(30)
            assert redeliverable_jobs(tx, _host(), clock.now) == [bad]
    run_round(cfg, hook, clock, expect_error=True)
    assert event_row(bad)["attempts"] == 2
    assert hook.calls.count(good) == 1


def test_a_deferred_sync_event_blocks_only_its_own_jobs_sync_events(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Job A: two sync events (the first deferred) and an async one; job B: one sync event."""
    a, b = async_events(capsys, tmp_path, 2, 1)
    sql("UPDATE hook_outbox SET async = 0")
    first = event_row(a)
    second = {**first["payload"], "event_id": f"{a}:post-held:second"}
    for event_id, is_async in ((second["event_id"], 0), (f"{a}:post-held:async", 1)):
        sql(
            "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
            "VALUES (?, ?, 'post-held', ?, ?, 'x')",
            event_id,
            a,
            json.dumps({**second, "event_id": event_id}),
            is_async,
        )
    sql(
        "UPDATE hook_outbox SET attempts = 1, retry_after = ? WHERE event_id = ?",
        format_timestamp(T0 + dt.timedelta(seconds=30)),
        first["event_id"],
    )
    cfg = Config.model_validate({"hooks": {"post-held": [{"name": "w", "cmd": "true"}]}})
    with open_store() as store, store.read() as tx:
        assert redeliverable_jobs(tx, _host(), T0) == [b]
        d = Deliverer(store, cfg, clock=Clock(), diag=lambda m: None)
        assert d.next_event(b, is_async=False) is not None
        assert d.next_event(a, is_async=True) is not None  # A's async event stays deliverable
        # the deferral is the first sync event of A: its later sync event waits behind it
        deferred = d.next_event(a, is_async=False)
        assert deferred is not None and deferred["event_id"] == first["event_id"]
        assert d.defer_notice(deferred)
        assert not d.defer_notice(d.next_event(b, is_async=False))  # pyright: ignore
        sql("UPDATE hook_outbox SET delivered_at = 'x' WHERE job_id = ? AND async = 0", b)
    with open_store() as store, store.read() as tx:
        assert redeliverable_jobs(tx, _host(), T0) == []  # only A's sync events were left
        assert has_undelivered(tx, _host(), T0)  # A's async event is deliverable
        sql("UPDATE hook_outbox SET delivered_at = 'x' WHERE async = 1")
    with open_store() as store, store.read() as tx:
        assert not has_undelivered(tx, _host(), T0)
        assert redeliverable_jobs(tx, _host(), T0 + dt.timedelta(seconds=30)) == [a]


def test_an_unkillable_failure_of_a_delivered_event_changes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (job,) = async_events(capsys, tmp_path, 1, 1)
    sql("UPDATE hook_outbox SET delivered_at = 'x'")
    before = event_row(job)
    with open_store() as store, store.write() as tx:
        assert record_unkillable(tx, before["event_id"], T0) is None
    assert event_row(job) == before


@pytest.mark.parametrize("mode", ["failed", "timeout"])
def test_ordinary_failures_do_not_count_and_are_never_retried(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, mode: str
) -> None:
    (job,) = async_events(capsys, tmp_path, 1, 1)
    cfg, clock, hook = _async_cfg(), Clock(), Hook(set(), mode)
    run_round(cfg, hook, clock)
    row = event_row(job)
    assert row["delivered_at"] is not None and row["attempts"] == 0 and row["retry_after"] is None


def test_retries_are_never_abandoned_and_continue_at_the_cap(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (bad,) = async_events(capsys, tmp_path, 1, 1)
    cfg, clock = _async_cfg(), Clock()
    hook = Hook({bad})
    for attempts in range(1, 13):
        run_round(cfg, hook, clock, expect_error=True)
        row = event_row(bad)
        assert row["attempts"] == attempts and row["delivered_at"] is None
        wait = backoff_seconds(attempts)
        assert row["retry_after"] == format_timestamp(clock.now + dt.timedelta(seconds=wait))
        clock.advance(wait)
    assert backoff_seconds(12) == BACKOFF_CAP_SECONDS and len(hook.calls) == 12


def test_a_sync_delivery_by_a_command_counts_the_failure_too(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (bad,) = async_events(capsys, tmp_path, 1, 1)
    sql("UPDATE hook_outbox SET async = 0")
    cfg, clock = (
        Config.model_validate({"hooks": {"post-held": [{"name": "w", "cmd": "true"}]}}),
        Clock(),
    )
    with open_store() as store:
        d = Deliverer(store, cfg, runner=Hook({bad}), clock=clock, diag=lambda m: None)
        with pytest.raises(JobsctlError):
            d.deliver_sync(bad)
        assert d.deliver_sync(bad) is False  # deferred: nothing runs, nothing raises
    assert event_row(bad)["attempts"] == 1


def test_cancel_with_a_deferred_earlier_event_runs_no_hook_and_says_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "ran"
    hook = [{"name": "w", "cmd": f"echo ran >> {out}"}]
    cfg = config_file(tmp_path, hooks={"post-held": hook, "post-cancel": hook})
    job = add_job(capsys, config=cfg)
    assert run_cli(capsys, "--config", cfg, "--json", "hold", job).code == 0
    out.unlink()  # the hold ran its hook; now its event backs off
    (first,) = outbox()
    far = format_timestamp(dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    sql(
        "UPDATE hook_outbox SET delivered_at = NULL, attempts = 1, retry_after = ?",
        far,
    )
    res = run_cli(capsys, "--config", cfg, "--json", "cancel", job)
    assert res.code == 0, res.out + res.err
    assert res.err == "" and not out.exists()  # nothing said, no hook run
    events = outbox()
    assert [e["event_id"] for e in events if e["delivered_at"] is None][0] == first["event_id"]
    assert len([e for e in events if e["delivered_at"] is None]) == 2  # left for `_deliver`


# -- the finding -----------------------------------------------------------------------------


def make_stuck(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, attempts: int
) -> tuple[str, str]:
    (job,) = async_events(capsys, tmp_path, 1, 1)
    event = event_row(job)["event_id"]
    sql(
        "UPDATE hook_outbox SET attempts = ?, retry_after = ? WHERE event_id = ?",
        attempts,
        format_timestamp(T0 + dt.timedelta(hours=1)),
        event,
    )
    return job, event


def test_below_the_threshold_there_is_no_finding(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    make_stuck(capsys, tmp_path, STUCK_ATTEMPTS - 1)
    res = run_cli(capsys, "--json", "reconcile")
    assert res.envelope()["findings"] == []


@pytest.mark.parametrize(("attempts", "findings"), [(4, 0), (5, 1)])
def test_the_threshold_is_five_attempts(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, attempts: int, findings: int
) -> None:
    make_stuck(capsys, tmp_path, attempts)
    assert len(run_cli(capsys, "--json", "reconcile").envelope()["findings"]) == findings


def test_a_pass_that_repeats_reports_a_stuck_event_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, event = make_stuck(capsys, tmp_path, STUCK_ATTEMPTS)
    bumped: list[str] = []

    def probe(step: str) -> None:
        if step == "release" and not bumped:  # a request that arrives while the pass runs
            bumped.append(step)
            with open_store() as other, other.write() as tx:
                counter_incr(tx, "pass_requested")

    with open_store() as store:
        runner = Runner(store, Config(), None, clock=Clock())
        runner.probe = probe
        body = runner.run_pass().as_json()
    assert bumped
    assert [f["event_id"] for f in body["reconciled"]] == [event]


@pytest.mark.parametrize("attempts", [STUCK_ATTEMPTS, STUCK_ATTEMPTS + 2])
def test_reconcile_reports_a_stuck_event_and_logs_it_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, attempts: int
) -> None:
    job, event = make_stuck(capsys, tmp_path, attempts)
    expected = {
        "job_id": job,
        "finding": "hook_delivery_stuck",
        "action": "reported",
        "event_id": event,
        "attempts": attempts,
    }
    dry = run_cli(capsys, "--json", "reconcile", "--dry-run")
    validate_against("cli/reconcile.json", dry.envelope())
    assert dry.envelope()["findings"] == [expected]
    assert log_records("reconcile") == []  # a dry run writes nothing
    for _ in range(2):
        res = run_cli(capsys, "--json", "reconcile")
        validate_against("cli/reconcile.json", res.envelope())
        assert res.envelope()["findings"] == [expected]  # reported at every run
    (record,) = log_records("reconcile")  # logged once per event
    assert record["level"] == "warn" and record["job_id"] == job
    assert record["finding"] == "hook_delivery_stuck" and record["action"] == "reported"
    assert record["details"] == {"event_id": event, "attempts": attempts}


def test_a_pass_reports_a_stuck_event_in_run_json_without_a_pass_record(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job, event = make_stuck(capsys, tmp_path, STUCK_ATTEMPTS)
    with open_store() as store:
        result = Runner(store, Config(), None, clock=Clock()).run_pass()
    body = result.as_json()
    validate_against("cli/run.json", body)
    assert [f["event_id"] for f in body["reconciled"]] == [event]
    assert log_records("pass") == []  # a steady state does not make every pass a changed one


def test_a_delivered_event_is_no_longer_reported(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    make_stuck(capsys, tmp_path, STUCK_ATTEMPTS)
    sql("UPDATE hook_outbox SET delivered_at = 'x'")
    assert run_cli(capsys, "--json", "reconcile").envelope()["findings"] == []


# -- the migration ---------------------------------------------------------------------------


def columns(conn: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
    return {r[1]: tuple(r) for r in conn.execute("PRAGMA table_info(hook_outbox)")}


def test_a_fresh_database_has_the_columns_with_defaults(tmp_path: Path) -> None:
    with Store.open() as store:
        cols = {r["name"]: r for r in store.query_all("PRAGMA table_info(hook_outbox)")}
        assert cols["attempts"]["notnull"] == 1 and cols["attempts"]["dflt_value"] == "0"
        assert cols["retry_after"]["notnull"] == 0
        assert store.current_version() == len(MIGRATIONS) >= 2


def test_a_version_1_database_migrates_and_keeps_its_events(tmp_path: Path) -> None:
    with Store.open(migrations=MIGRATIONS[:1]) as old:
        with old.write() as tx:
            tx.execute(
                "INSERT INTO jobs (id, queue, argv, cwd, position, status, created_at, "
                "enqueued_at, status_changed_at, actor) VALUES ('jb-aaaaaaaa', 'q', '[]', '/', "
                "'a', 'held', 't', 't', 't', 'a')"
            )
            tx.execute(
                "INSERT INTO hook_outbox (event_id, job_id, hook, payload, created_at) "
                "VALUES ('e1', 'jb-aaaaaaaa', 'post-held', '{}', 't')"
            )
        assert old.current_version() == 1
    with Store.open() as store:
        assert store.current_version() == len(MIGRATIONS)
        (row,) = store.query_all("SELECT attempts, retry_after, delivered_at FROM hook_outbox")
        assert (row["attempts"], row["retry_after"], row["delivered_at"]) == (0, None, None)


def test_the_previous_versions_statements_work_on_a_migrated_outbox() -> None:
    """What the previous release does to the outbox (it never names the new columns)."""
    with Store.open() as store, store.write() as tx:
        tx.execute(
            "INSERT INTO jobs (id, queue, argv, cwd, position, status, created_at, "
            "enqueued_at, status_changed_at, actor) VALUES ('jb-aaaaaaaa', 'q', '[]', '/', "
            "'a', 'held', 't', 't', 't', 'a')"
        )
        tx.execute(
            "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
            "VALUES ('e1', 'jb-aaaaaaaa', 'post-held', '{}', 1, 't')"
        )
        tx.execute("UPDATE hook_outbox SET retry_after = 'z' WHERE event_id = 'e1'")
        rows = tx.all(
            "SELECT * FROM hook_outbox WHERE job_id = ? AND delivered_at IS NULL ORDER BY seq",
            ["jb-aaaaaaaa"],
        )
        assert len(rows) == 1
        tx.execute("UPDATE hook_outbox SET delivered_at = 'd' WHERE event_id = 'e1'")


# -- the schema ------------------------------------------------------------------------------


def _finding(**extra: Any) -> dict[str, Any]:
    return {"job_id": "jb-aaaaaaaa", "finding": "hook_delivery_stuck", "action": "reported"} | extra


def _check_finding(doc: dict[str, Any]) -> None:
    validate_against(
        "cli/reconcile.json",
        {"already_running": False, "holder": None, "dry_run": False, "findings": [doc]},
    )


@pytest.mark.parametrize(
    "doc",
    [
        _finding(),  # event_id and attempts are required
        _finding(event_id="e1"),
        _finding(attempts=5),
        _finding(event_id="e1", attempts=5) | {"finding": "hooks_redelivered"},
    ],
)
def test_the_finding_schema_ties_event_id_and_attempts_to_the_stuck_finding(
    doc: dict[str, Any],
) -> None:
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        _check_finding(doc)


def test_the_finding_schema_accepts_the_stuck_finding_and_the_others() -> None:
    _check_finding(_finding(event_id="e1", attempts=5))
    _check_finding(_finding() | {"finding": "hooks_redelivered", "action": "redelivered"})
