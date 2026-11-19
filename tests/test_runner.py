"""The pass, in process (interface.md, Pass). Shims are fake executables from ``runner_helpers``;
the process-level behavior with several real processes is in ``test_runner_processes.py``."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import env_file as env_file_module
from jobsctl import gateway
from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.leases import expiry, own_identity
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Liveness, LivenessResult
from jobsctl.retention import PruneResult
from jobsctl.runner import LeaseLost, Runner
from jobsctl.store_db import Store
from jobsctl.store_ops import counter_get, counter_incr, lease_since, take_lease
from jobsctl.timestamps import format_timestamp
from jobsctl.triggers import PASS_REQUESTED, request_pass
from tests.capabilities import needs_proc, needs_waitid
from tests.conftest import validate_against, validate_record_file
from tests.runner_fixtures import PassSpawns
from tests.runner_helpers import (
    ShimKit,
    add_queued,
    attempts_of,
    job_ids,
    job_row,
    make_runner,
)
from tests.store_helpers import insert_row


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state") as s:
        yield s


class FakeProc:
    def __init__(self, pid: int = 4_000_000) -> None:
        self.pid = pid

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the process spawn: record argv, env and cwd, start nothing."""
    calls: list[dict[str, Any]] = []

    def fake(argv: list[str], *, env: dict[str, str], cwd: str) -> FakeProc:
        calls.append({"argv": argv, "env": env, "cwd": cwd})
        return FakeProc()

    monkeypatch.setattr(gateway, "spawn_detached", fake)
    return calls


def cfg(**keys: Any) -> Config:
    return Config.model_validate(keys)


def lease_row(store: Store) -> Any:
    return store.query_one("SELECT * FROM leases WHERE name = 'runner'")


def log_events(store: Store) -> list[dict[str, Any]]:
    path = store.paths.root / "log.jsonl"
    return validate_record_file(path, "log.json") if path.exists() else []


# -- a pass that claims and spawns ---------------------------------------------------------------


@needs_proc
def test_a_pass_claims_spawns_a_detached_shim_and_records_its_identity(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JOBSCTL_SECRET", "hunter2")
    monkeypatch.setenv("TOKEN_FROM_ENV_FILE", "not-inherited")
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    runner = make_runner(runners, store, kit=kit)
    leaked = str(tmp_path / "leak-me")
    fd = os.open(leaked, os.O_CREAT | os.O_WRONLY, 0o600)
    os.set_inheritable(fd, True)
    try:
        result = runner.run_pass()
    finally:
        os.close(fd)
    assert result.started == [job_id] and not result.already_running
    validate_against("cli/run.json", result.as_json())
    (attempt,) = attempts_of(store, job_id)
    record = kit.record(job_id)
    assert attempt["shim_identity"].endswith(
        f":{record['pid']}:{attempt['shim_identity'].split(':')[-1]}"
    )
    assert attempt["shim_identity"].split(":")[-2] == str(record["pid"])
    assert attempt["claimed_by"] == own_identity(default_host())
    assert attempt["claim_id"] == record["argv"][-1] and record["argv"][-2] == job_id
    assert record["argv"][:4] == ["-I", "-m", "jobsctl", "_shim"]
    assert record["sid"] == record["pid"] != os.getsid(0)  # its own session
    assert record["cwd"] == str(store.paths.root)
    assert record["fds"]["0"] == record["fds"]["1"] == record["fds"]["2"] == "/dev/null"
    assert leaked not in record["fds"].values()  # a descriptor the parent left inheritable
    others = {k: v for k, v in record["fds"].items() if int(k) > 2}
    assert all(v.startswith(("/proc/", "/sys/")) for v in others.values()), others
    assert not any(k.startswith("JOBSCTL_") for k in record["env"])
    assert "hunter2" not in str(record["env"])
    assert lease_row(store) is None  # released
    events = [e["event"] for e in log_events(store)]
    assert events == ["transition", "shim_spawned", "pass"]


def test_the_spawn_argument_vector_and_config_hand_off(
    store: Store, spawned: list[dict[str, Any]], runners: list[Runner], tmp_path: Path
) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    conf = tmp_path / "c.yaml"
    runner = make_runner(runners, store, config_path=conf, python="/opt/py")
    monkey_identity(runner)
    runner.run_pass()
    (call,) = spawned
    claim_id = attempts_of(store, job_id)[0]["claim_id"]
    assert call["argv"] == [
        "/opt/py",
        "-I",
        "-m",
        "jobsctl",
        "--config",
        str(conf),
        "_shim",
        job_id,
        claim_id,
    ]
    assert call["cwd"] == str(store.paths.root)
    assert not any(k.startswith("JOBSCTL_") for k in call["env"])


def monkey_identity(runner: Runner) -> None:
    """The fake spawn has no process behind it: identify this process instead."""
    import jobsctl.runner as module

    module.identity_text = lambda host, pid: own_identity(host)  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def restore_identity() -> Iterator[None]:
    import jobsctl.runner as module

    original = module.identity_text
    yield
    module.identity_text = original


def test_max_starts_counts_claims_across_the_repeated_claim_loop(
    store: Store, kit: ShimKit, runners: list[Runner]
) -> None:
    ids = job_ids(5)
    for i, job_id in enumerate(ids):
        add_queued(store, job_id, position=f"{i + 1:020d}")
    runner = make_runner(runners, store, cfg(default_cap=None), kit=kit)
    extra = []

    def probe(step: str) -> None:
        if step == "retention" and not extra:
            extra.append(1)
            with Store.open(store.paths.root) as other:
                request_pass(other, cfg(), None)  # a trigger mid-pass: the claim loop repeats

    runner.probe = probe
    result = runner.run_pass(max_starts=2)
    assert result.started == ids[:2]
    assert {job_row(store, j)["status"] for j in ids[2:]} == {"queued"}


def test_a_request_during_the_pass_repeats_the_claim_loop_and_spawns_no_pass(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = job_ids(2)
    add_queued(store, first, queue="a")
    runner = make_runner(runners, store, cfg(default_cap=None), kit=kit)
    seen: list[str] = []

    def probe(step: str) -> None:
        seen.append(step)
        if step == "release" and len([s for s in seen if s == "release"]) == 1:
            with Store.open(store.paths.root) as other:
                add_queued(other, second, queue="b")
                called: list[object] = []
                with monkeypatch.context() as patch:
                    patch.setattr(gateway, "spawn_detached", lambda *a, **k: called.append(1))
                    request_pass(other, cfg(), None)  # the holder (this process) is alive
                assert called == []  # a live holder: no pass is spawned, the request is kept

    runner.probe = probe
    result = runner.run_pass()
    assert result.started == [first, second]
    assert seen.count("claim") == 2 and seen.count("release") == 2
    assert lease_row(store) is None
    assert counter_get_(store, PASS_REQUESTED) == 1


def counter_get_(store: Store, name: str) -> int:
    with store.write() as tx:
        return counter_get(tx, name)


# -- the lease -----------------------------------------------------------------------------------


def test_a_live_holder_means_already_running_with_its_pid_and_start(
    store: Store, runners: list[Runner], sleeper: str
) -> None:
    now = dt.datetime.now(dt.UTC)
    assert take_lease(
        store, "runner", sleeper, now=format_timestamp(now), expires_at=expiry(now),
        is_alive=lambda _h: True,
    )  # fmt: skip
    since = lease_since(store, "runner")
    assert since == format_timestamp(now)
    runner = make_runner(runners, store)
    result = runner.run_pass()
    assert result.already_running and result.started == [] and result.reconciled == []
    document = result.as_json()
    assert document["holder"] == {"pid": int(sleeper.split(":")[1]), "since": since}
    validate_against("cli/run.json", document)
    assert lease_row(store) is not None  # not ours: untouched


def test_a_dead_holder_is_replaced(store: Store, runners: list[Runner]) -> None:
    now = dt.datetime.now(dt.UTC)
    with store.write() as tx:
        tx.execute(
            "INSERT INTO leases (name, holder_identity, expires_at) VALUES ('runner', ?, ?)",
            ["other-boot:42:7", expiry(now)],
        )
    result = make_runner(runners, store).run_pass()
    assert not result.already_running and lease_row(store) is None


@pytest.mark.parametrize(("offset", "taken"), [(120, False), (-120, True)])
def test_an_inconclusive_holder_counts_until_the_lease_expires(
    store: Store,
    runners: list[Runner],
    monkeypatch: pytest.MonkeyPatch,
    sleeper: str,
    offset: int,
    taken: bool,
) -> None:
    unknown = LivenessResult(Liveness.UNKNOWN, "hidepid")  # e.g. /proc mounted with hidepid
    monkeypatch.setattr("jobsctl.leases.check_identity", lambda *a, **k: unknown)
    now = dt.datetime.now(dt.UTC)
    when = format_timestamp(now + dt.timedelta(seconds=offset))
    with store.write() as tx:
        tx.execute(
            "INSERT INTO leases (name, holder_identity, expires_at) VALUES ('runner', ?, ?)",
            [sleeper, when],
        )
    result = make_runner(runners, store).run_pass()
    assert result.already_running is (not taken)
    if not taken:
        validate_against("cli/run.json", result.as_json())
        assert result.as_json()["holder"]["pid"] == int(sleeper.split(":")[1])


def test_a_holder_text_that_is_no_identity_is_free(store: Store, runners: list[Runner]) -> None:
    with store.write() as tx:
        tx.execute(
            "INSERT INTO leases (name, holder_identity, expires_at) VALUES ('runner', 'opaque', ?)",
            [format_timestamp(dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))],
        )
    assert not make_runner(runners, store).run_pass().already_running


def test_a_live_holder_that_stopped_renewing_is_logged_stuck_once(
    store: Store, runners: list[Runner], sleeper: str
) -> None:
    long_ago = format_timestamp(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30))
    with store.write() as tx:
        tx.execute(
            "INSERT INTO leases (name, holder_identity, expires_at) VALUES ('runner', ?, ?)",
            [sleeper, long_ago],
        )
    for _ in range(2):
        assert make_runner(runners, store).run_pass().already_running
    stuck = [e for e in log_events(store) if e["event"] == "runner_stuck"]
    assert len(stuck) == 1 and stuck[0]["level"] == "warn"


def test_a_pass_that_lost_its_lease_stops_and_leaves_the_new_holder(
    store: Store, kit: ShimKit, runners: list[Runner]
) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    runner = make_runner(runners, store, kit=kit)

    def probe(step: str) -> None:
        if step == "claim":
            with store.write() as tx:
                tx.execute("UPDATE leases SET holder_identity = 'thief:1:1' WHERE name = 'runner'")
            runner._last_renew = -1e9  # make the step renew now

    runner.probe = probe
    with pytest.raises(LeaseLost):
        runner.run_pass()
    assert lease_row(store)["holder_identity"] == "thief:1:1"
    assert job_row(store, job_id)["status"] == "queued"


def test_an_error_in_a_step_releases_the_lease(store: Store, runners: list[Runner]) -> None:
    runner = make_runner(runners, store)

    def probe(step: str) -> None:
        if step == "claim":
            raise RuntimeError("boom")

    runner.probe = probe
    with pytest.raises(RuntimeError):
        runner.run_pass()
    assert lease_row(store) is None


def test_an_abandoned_pass_passes_a_request_on(
    store: Store, runners: list[Runner], trigger_spawns: PassSpawns
) -> None:
    runner = make_runner(runners, store)

    def probe(step: str) -> None:
        if step == "delivery":
            with Store.open(store.paths.root) as other:
                with other.write() as tx:
                    counter_incr(tx, PASS_REQUESTED)
            raise RuntimeError("boom")

    runner.probe = probe
    with pytest.raises(RuntimeError):
        runner.run_pass()
    assert [c["retried"] for c in trigger_spawns] == [0]


def test_a_pass_abandoned_before_its_first_claim_still_passes_a_request_on(
    store: Store, runners: list[Runner], trigger_spawns: PassSpawns
) -> None:
    """The request arrives during reconcile; the step then fails with `busy`."""
    runner = make_runner(runners, store)

    def probe(step: str) -> None:
        if step == "reconcile":
            with Store.open(store.paths.root) as other:
                request_pass(other, cfg(), None)
            raise JobsctlError("busy", "state.db is busy")

    runner.probe = probe
    with pytest.raises(JobsctlError):
        runner.run_pass()
    assert lease_row(store) is None
    assert [c["retried"] for c in trigger_spawns] == [0]


def test_an_abandoned_pass_without_a_request_spawns_nothing(
    store: Store, runners: list[Runner], trigger_spawns: PassSpawns
) -> None:
    runner = make_runner(runners, store)

    def probe(step: str) -> None:
        if step == "reconcile":
            raise JobsctlError("busy", "state.db is busy")

    runner.probe = probe
    with pytest.raises(JobsctlError):
        runner.run_pass()
    assert trigger_spawns == []


def test_a_release_that_fails_while_abandoning_is_warned_about(
    store: Store, runners: list[Runner], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = make_runner(runners, store)

    def busy_write() -> Any:
        raise JobsctlError("busy", "state.db is busy")

    def probe(step: str) -> None:
        if step == "reconcile":
            runner.store = type("S", (), {"write": staticmethod(busy_write)})()  # type: ignore[assignment]
            raise RuntimeError("boom")

    runner.probe = probe
    with pytest.raises(RuntimeError):
        runner.run_pass()
    assert "could not release the runner lease" in capsys.readouterr().err


def test_a_lease_holder_that_vanishes_between_the_take_and_the_read_is_retaken(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobsctl.runner as module

    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    real = module.take_lease
    calls: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> bool:
        calls.append(1)
        return False if len(calls) == 1 else real(*args, **kwargs)  # the first take loses a race

    monkeypatch.setattr(module, "take_lease", flaky)
    result = make_runner(runners, store, kit=kit).run_pass()  # no lease row exists to report
    assert len(calls) == 2
    assert not result.already_running and result.started == [job_id]


# -- spawn failures and reaping ------------------------------------------------------------------


def test_a_missing_interpreter_requeues_then_fails_for_good_on_the_third_try(
    store: Store, runners: list[Runner]
) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, position="9".zfill(20))
    runner = make_runner(runners, store, python="/nonexistent/python")
    for n in (1, 2):
        result = runner.run_pass()
        assert result.started == []
        (warning,) = result.warnings
        assert warning["code"] == "shim_spawn_failed" and warning["job_id"] == job_id
        validate_against("cli/run.json", result.as_json())
        row = job_row(store, job_id)
        assert (row["status"], row["spawn_failures"], row["position"]) == (
            "queued",
            n,
            "9".zfill(20),
        )
        assert attempts_of(store, job_id) == []
    result = runner.run_pass()
    row = job_row(store, job_id)
    assert (row["status"], row["reason"]) == ("failed", "spawn_failed")
    assert [a["status"] for a in attempts_of(store, job_id)] == ["failed"]
    assert result.warnings[0]["job_id"] == job_id
    assert runner.run_pass().started == []  # final: never claimed again
    assert lease_row(store) is None


def test_an_unreadable_shim_identity_requeues_and_keeps_the_handle_for_reaping(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.proc_identity import ProcessGone

    (job_id,) = job_ids(1)
    add_queued(store, job_id)

    def gone(host: object, pid: int) -> str:
        raise ProcessGone(pid)

    monkeypatch.setattr("jobsctl.runner.identity_text", gone)
    runner = make_runner(runners, store, kit=kit)
    result = runner.run_pass()
    assert result.started == [] and result.warnings[0]["code"] == "shim_spawn_failed"
    assert job_row(store, job_id)["status"] == "queued"
    assert len(runner.children) == 1  # still reaped by its handle; never signaled by the pass


def test_a_shim_child_that_exits_unconfirmed_is_requeued_as_claim_orphaned(
    store: Store, runners: list[Runner]
) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, position="3".zfill(20))
    runner = make_runner(runners, store, python="/bin/true")
    first = runner.run_pass()
    assert first.started == [job_id] and first.reconciled == []
    assert job_row(store, job_id)["status"] == "starting"
    runner.children[0].popen.wait(timeout=10)
    second = runner.run_pass()
    assert {
        "job_id": job_id,
        "finding": "claim_orphaned",
        "action": "requeued",
    } in second.reconciled
    validate_against("cli/run.json", second.as_json())
    # requeued, then claimed again by the same pass: a new attempt, no spawn failure counted
    row = job_row(store, job_id)
    assert row["spawn_failures"] == 0 and row["position"] == "3".zfill(20)
    assert any(
        e["event"] == "reconcile" and e["finding"] == "claim_orphaned" for e in log_events(store)
    )


def test_a_confirmed_shim_that_exits_is_failed_lost_by_reconcile(
    store: Store, runners: list[Runner]
) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    runner = make_runner(runners, store, cfg(default_cap=1), python="/bin/true")
    runner.run_pass()
    with store.write() as tx:
        tx.execute("UPDATE attempts SET shim_confirmed_at = ?", ["2026-01-01T00:00:00.000Z"])
    runner.children[0].popen.wait(timeout=10)
    result = runner.run_pass()
    assert result.reconciled == [
        {"job_id": job_id, "finding": "shim_dead", "action": "failed_lost"}
    ]
    row = job_row(store, job_id)
    assert (row["status"], row["reason"]) == ("failed", "lost")


def test_a_busy_identity_write_of_one_claim_does_not_stop_the_others(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobsctl.runner as module

    first, second, third = job_ids(3)
    for i, job_id in enumerate((first, second, third)):
        add_queued(store, job_id, position=f"{i + 1:020d}")
    real = module.update_attempt_by_claim

    def flaky(tx: Any, job_id: str, *args: Any, **kwargs: Any) -> bool:
        if job_id == first:
            raise JobsctlError("busy", "state.db is busy")
        return real(tx, job_id, *args, **kwargs)

    monkeypatch.setattr(module, "update_attempt_by_claim", flaky)
    runner = make_runner(runners, store, cfg(default_cap=None), kit=kit)
    result = runner.run_pass()
    assert result.started == [second, third]
    assert job_row(store, first)["status"] == "queued"
    assert job_row(store, first)["spawn_failures"] == 1
    assert lease_row(store) is None


def test_a_busy_identity_write_names_the_real_cause(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobsctl.runner as module

    (job_id,) = job_ids(1)
    add_queued(store, job_id)

    def busy(*args: Any, **kwargs: Any) -> bool:
        raise JobsctlError("busy", "state.db is busy")

    monkeypatch.setattr(module, "update_attempt_by_claim", busy)
    result = make_runner(runners, store, cfg(default_cap=None), kit=kit).run_pass()
    (warning,) = [w for w in result.warnings if w["code"] == "shim_spawn_failed"]
    assert warning["message"] == "recording the shim failed (busy)"
    assert "could not be started" not in warning["message"]


def test_a_busy_failure_handler_leaves_the_claim_for_reconcile_and_goes_on(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobsctl.runner as module

    first, second = job_ids(2)
    add_queued(store, first, position="1".zfill(20))
    add_queued(store, second, position="2".zfill(20))
    real = module.update_attempt_by_claim

    def flaky(tx: Any, job_id: str, *args: Any, **kwargs: Any) -> bool:
        if job_id == first:
            raise JobsctlError("busy", "state.db is busy")
        return real(tx, job_id, *args, **kwargs)

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise JobsctlError("busy", "state.db is busy")

    monkeypatch.setattr(module, "update_attempt_by_claim", flaky)
    monkeypatch.setattr(module, "fail_spawn", failing)
    result = make_runner(runners, store, cfg(default_cap=None), kit=kit).run_pass()
    assert result.started == [second]
    assert job_row(store, first)["status"] == "starting"


def test_a_job_canceled_before_its_identity_is_recorded_is_not_reported_started(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobsctl.runner as module

    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    real = module.identity_text

    def cancel_meanwhile(host: Any, pid: int) -> str:
        with store.write() as tx:
            tx.execute("UPDATE jobs SET status = 'canceled' WHERE id = ?", [job_id])
        return real(host, pid)

    monkeypatch.setattr(module, "identity_text", cancel_meanwhile)
    result = make_runner(runners, store, kit=kit).run_pass()
    assert result.started == []
    assert attempts_of(store, job_id)[0]["shim_identity"] is None
    assert "shim_spawned" not in [e["event"] for e in log_events(store)]


# -- the other steps -----------------------------------------------------------------------------


def test_retention_runs_with_the_pass_clock_and_config(
    store: Store, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake(store_: Store, writer: object, **kwargs: Any) -> PruneResult:
        calls.append(kwargs)
        return PruneResult()

    monkeypatch.setattr("jobsctl.runner.run_retention", fake)
    conf = cfg(retention="1h", queues={"known": {"cap": 2}})
    make_runner(runners, store, conf).run_pass()
    (kwargs,) = calls
    assert kwargs["retention"] == 3600
    assert set(kwargs["configured_queues"]) == {"known"}
    assert isinstance(kwargs["now"], dt.datetime) and kwargs["now"].tzinfo is not None


@pytest.mark.parametrize("failure", ["audit", "busy", "oserror"])
def test_a_failed_retention_does_not_skip_the_checkpoint_or_the_release(
    store: Store, runners: list[Runner], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fake(store_: Store, writer: object, **kwargs: Any) -> PruneResult:
        if failure == "busy":
            raise JobsctlError("busy", "state.db is busy")
        if failure == "oserror":
            raise OSError("disk")
        return PruneResult(audit_error=RuntimeError("audit"))

    monkeypatch.setattr("jobsctl.runner.run_retention", fake)
    checkpoints: list[int] = []
    runner = make_runner(runners, store)
    original = runner._checkpoint
    runner._checkpoint = lambda: (checkpoints.append(1), original())[1]  # type: ignore[method-assign]
    result = runner.run_pass()
    assert not result.already_running and checkpoints == [1] and lease_row(store) is None
    (warn,) = [e for e in log_events(store) if e["event"] == "pass"]
    assert warn["level"] == "warn" and warn["details"]["step"] == "retention"


def test_delivery_is_requested_for_undelivered_events_unless_a_holder_is_alive(
    store: Store, runners: list[Runner], spawned: list[dict[str, Any]]
) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, status="failed")
    with store.write() as tx:
        insert_row(
            tx, "hook_outbox",
            {"event_id": "e1", "job_id": job_id, "hook": "post-fail", "payload": "{}",
             "async": 0, "created_at": "2026-01-01T00:00:00.000Z"},
        )  # fmt: skip
    make_runner(runners, store).run_pass()
    assert [c["argv"][-1] for c in spawned] == ["_deliver"]
    assert counter_get_(store, "deliver_requested") == 1
    # a live job lease holder: the shim delivers it itself
    spawned.clear()
    now = dt.datetime.now(dt.UTC)
    take_lease(
        store, f"job:{job_id}", own_identity(default_host()), now=format_timestamp(now),
        expires_at=expiry(now), is_alive=lambda _h: True,
    )  # fmt: skip
    make_runner(runners, store).run_pass()
    assert spawned == []
    # ... but an async event is always handed over, unless `deliver` has a live holder
    with store.write() as tx:
        tx.execute("UPDATE hook_outbox SET async = 1")
    take_lease(
        store, "deliver", own_identity(default_host()), now=format_timestamp(now),
        expires_at=expiry(now), is_alive=lambda _h: True,
    )  # fmt: skip
    make_runner(runners, store).run_pass()
    assert spawned == []
    with store.write() as tx:
        tx.execute("DELETE FROM leases WHERE name = 'deliver'")
    make_runner(runners, store).run_pass()
    assert [c["argv"][-1] for c in spawned] == ["_deliver"]


def test_nothing_to_deliver_spawns_nothing_and_a_quiet_pass_logs_nothing(
    store: Store, runners: list[Runner], spawned: list[dict[str, Any]]
) -> None:
    make_runner(runners, store).run_pass()
    assert spawned == [] and log_events(store) == []


def test_an_untrusted_env_file_keeps_pass_env_jobs_queued_and_warns(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exempt_tmp_ancestors(monkeypatch, tmp_path)
    wants, plain = job_ids(2)
    add_queued(store, wants, position="1".zfill(20), pass_env='["TOKEN"]')
    add_queued(store, plain, position="2".zfill(20))
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)  # group- and world-writable: not trusted
    runner = make_runner(runners, store, cfg(default_cap=None, env_file=str(env_file)), kit=kit)
    result = runner.run_pass()
    assert result.started == [plain]
    assert job_row(store, wants)["status"] == "queued"
    assert [(w["code"], w["reason"]) for w in result.warnings] == [("env_file_unsafe", "mode")]
    validate_against("cli/run.json", result.as_json())
    assert "abc123secret" not in (store.paths.root / "log.jsonl").read_text(encoding="utf-8")
    assert "abc123secret" not in str(kit.record(plain)["env"])


def _exempt_tmp_ancestors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pytest's tmp dir sits under the world-writable /tmp, which would fail every env_file with
    `ancestor`: exempt the ancestors up to tmp_path so the check under test is the one failing."""

    def load(path: str | None) -> Any:
        return env_file_module.load_env_file(path, check_ancestors_from=tmp_path)

    monkeypatch.setattr("jobsctl.runner.load_env_file", load)


def _env_file_for_reason(reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An env_file path that fails exactly the `reason` check, built under tmp_path."""
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "env"
    if reason == "path":
        return str(directory / ".." / "env")  # a `..` component (config rejects relative paths)
    if reason == "open":
        return str(target)  # missing
    if reason == "not_regular":
        target.mkdir(mode=0o700)
        return str(target)
    if reason == "owner":
        target.write_text("TOKEN=abc123secret\n", encoding="utf-8")
        target.chmod(0o600)
        real = env_file_module._file_untrusted_reason
        monkeypatch.setattr(  # only the file looks foreign; the directories stay trusted
            env_file_module,
            "_file_untrusted_reason",
            lambda mode, uid, size, euid: real(mode, uid, size, euid + 1),
        )
        return str(target)
    target.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    target.chmod(0o600)
    if reason == "read":

        def failing_read(fd: int) -> tuple[None, str]:
            os.close(fd)  # the real _read_trusted closes the fd in its finally
            return None, "read"

        # the file opens and passes every check, then the read fails
        monkeypatch.setattr(env_file_module, "_read_trusted", failing_read)
    elif reason == "mode":
        target.chmod(0o666)
    elif reason == "size":
        target.write_bytes(b"A=" + b"x" * env_file_module.MAX_BYTES + b"\n")
    elif reason == "ancestor":
        directory.chmod(0o770)
    return str(target)


@pytest.mark.parametrize(
    "reason", ["path", "open", "not_regular", "owner", "read", "mode", "size", "ancestor"]
)
def test_the_run_warning_names_the_failed_check_and_matches_the_log_record(
    reason: str,
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (wants,) = job_ids(1)
    add_queued(store, wants, pass_env='["TOKEN"]')
    _exempt_tmp_ancestors(monkeypatch, tmp_path)
    path = _env_file_for_reason(reason, tmp_path, monkeypatch)
    runner = make_runner(runners, store, cfg(default_cap=None, env_file=path), kit=kit)
    result = runner.run_pass()
    document = result.as_json()
    validate_against("cli/run.json", document)
    (warning,) = document["warnings"]
    assert warning["code"] == "env_file_unsafe"
    assert warning["reason"] == reason
    (record,) = [e for e in log_events(store) if e["event"] == "env_file_unsafe"]
    assert record["details"]["reason"] == warning["reason"]
    assert "abc123secret" not in str(document)


def _unsafe_records(store: Store) -> list[dict[str, Any]]:
    return [e for e in log_events(store) if e["event"] == "env_file_unsafe"]


@pytest.mark.parametrize(
    "reason", ["path", "open", "not_regular", "owner", "read", "mode", "size", "ancestor"]
)
def test_a_configured_unsafe_env_file_is_reported_with_nothing_queued(
    reason: str,
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exempt_tmp_ancestors(monkeypatch, tmp_path)
    path = _env_file_for_reason(reason, tmp_path, monkeypatch)
    runner = make_runner(runners, store, cfg(default_cap=None, env_file=path), kit=kit)
    result = runner.run_pass()
    document = result.as_json()
    validate_against("cli/run.json", document)
    (warning,) = document["warnings"]
    assert (warning["code"], warning["reason"]) == ("env_file_unsafe", reason)
    (record,) = _unsafe_records(store)
    assert record["details"]["reason"] == reason
    assert "abc123secret" not in str(document) + (store.paths.root / "log.jsonl").read_text()
    second = runner.run_pass()  # same state: the warning is still reported, the log is not
    assert [w["code"] for w in second.warnings] == ["env_file_unsafe"]
    assert len(_unsafe_records(store)) == 1


def test_a_safe_configured_env_file_gives_no_warning(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exempt_tmp_ancestors(monkeypatch, tmp_path)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    env_file = directory / "env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o600)
    runner = make_runner(runners, store, cfg(default_cap=None, env_file=str(env_file)), kit=kit)
    result = runner.run_pass()
    assert result.warnings == []
    validate_against("cli/run.json", result.as_json())
    assert _unsafe_records(store) == []


def test_no_env_file_configured_means_no_check_and_no_file_access(
    store: Store, kit: ShimKit, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def load(path: str | None) -> Any:
        calls.append(path)
        return env_file_module.load_env_file(path)

    monkeypatch.setattr("jobsctl.runner.load_env_file", load)
    result = make_runner(runners, store, cfg(default_cap=None), kit=kit).run_pass()
    assert calls == [] and result.warnings == [] and log_events(store) == []


def test_an_unsafe_env_file_is_loaded_once_per_pass_and_with_a_job_warns_once(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (wants,) = job_ids(1)
    add_queued(store, wants, pass_env='["TOKEN"]')
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)
    loads: list[str | None] = []
    real = env_file_module.load_env_file

    def counting(path: str | None) -> Any:
        loads.append(path)
        return real(path, check_ancestors_from=tmp_path)

    monkeypatch.setattr("jobsctl.runner.load_env_file", counting)
    result = make_runner(runners, store, cfg(default_cap=None, env_file=str(env_file)), kit=kit)
    out = result.run_pass()
    assert loads == [str(env_file)]  # one load, however many claims or queued jobs need it
    assert job_row(store, wants)["status"] == "queued"
    assert [w["code"] for w in out.warnings] == ["env_file_unsafe"]
    assert len(_unsafe_records(store)) == 1


def test_the_env_file_state_changes_follow_the_once_per_state_rule(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exempt_tmp_ancestors(monkeypatch, tmp_path)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    env_file = directory / "env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    runner = make_runner(runners, store, cfg(default_cap=None, env_file=str(env_file)), kit=kit)
    env_file.chmod(0o666)
    assert [w["reason"] for w in runner.run_pass().warnings] == ["mode"]
    env_file.chmod(0o600)
    assert runner.run_pass().warnings == []
    env_file.chmod(0o666)
    assert [w["reason"] for w in runner.run_pass().warnings] == ["mode"]
    assert len(_unsafe_records(store)) == 2  # clearing re-arms silently; unsafe again logs


def test_a_pass_that_does_not_hold_the_lease_does_not_check_the_env_file(
    store: Store,
    runners: list[Runner],
    sleeper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime.now(dt.UTC)
    assert take_lease(
        store, "runner", sleeper, now=format_timestamp(now), expires_at=expiry(now),
        is_alive=lambda _h: True,
    )  # fmt: skip
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)
    loads: list[str | None] = []
    monkeypatch.setattr("jobsctl.runner.load_env_file", lambda p: loads.append(p))
    result = make_runner(runners, store, cfg(env_file=str(env_file))).run_pass()
    assert result.already_running and result.warnings == [] and loads == []
    assert _unsafe_records(store) == []


def test_the_env_file_warning_is_reported_once_per_pass_however_often_it_claims(
    store: Store, kit: ShimKit, runners: list[Runner], tmp_path: Path
) -> None:
    (wants,) = job_ids(1)
    add_queued(store, wants, pass_env='["TOKEN"]')
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)
    runner = make_runner(runners, store, cfg(env_file=str(env_file)), kit=kit)
    releases: list[str] = []

    def probe(step: str) -> None:
        if step == "release":
            releases.append(step)
            if len(releases) == 1:
                with Store.open(store.paths.root) as other:
                    request_pass(other, cfg(), None)  # a second claim iteration

    runner.probe = probe
    result = runner.run_pass()
    assert len(releases) == 2
    assert [w["code"] for w in result.warnings] == ["env_file_unsafe"]


def test_a_failing_env_state_log_write_still_reports_the_warning(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)

    def failing(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("jobsctl.runner.log_env_file_state", failing)
    result = make_runner(runners, store, cfg(env_file=str(env_file)), kit=kit).run_pass()
    assert [w["code"] for w in result.warnings] == ["env_file_unsafe"]


def test_a_fresh_pass_reports_the_unchanged_unsafe_env_file_again_without_logging(
    store: Store, kit: ShimKit, runners: list[Runner], tmp_path: Path
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)
    config = cfg(env_file=str(env_file))
    first = make_runner(runners, store, config, kit=kit).run_pass()
    second = make_runner(runners, store, config, kit=kit).run_pass()  # a new Runner: new cache
    assert [w["code"] for w in first.warnings] == ["env_file_unsafe"]
    assert [w["code"] for w in second.warnings] == ["env_file_unsafe"]
    assert len(_unsafe_records(store)) == 1


def test_reconcile_does_not_check_the_env_file(
    store: Store,
    kit: ShimKit,
    runners: list[Runner],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text("TOKEN=abc123secret\n", encoding="utf-8")
    env_file.chmod(0o666)
    loads: list[str | None] = []
    monkeypatch.setattr("jobsctl.runner.load_env_file", lambda p: loads.append(p))
    result = make_runner(runners, store, cfg(env_file=str(env_file)), kit=kit).run_reconcile()
    assert loads == []
    assert [w for w in result.warnings if w["code"] == "env_file_unsafe"] == []


def test_the_pass_summary_is_logged_only_when_something_changed(
    store: Store, kit: ShimKit, runners: list[Runner]
) -> None:
    runner = make_runner(runners, store, kit=kit)
    runner.run_pass()
    assert log_events(store) == []
    add_queued(store, job_ids(1)[0])
    runner.run_pass()
    assert [e["event"] for e in log_events(store)][-1] == "pass"


@needs_waitid
def test_reaping_waits_for_exact_pids_never_for_any_child(
    store: Store, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
    waited: list[tuple[str, tuple[Any, ...]]] = []
    real_waitpid, real_waitid = os.waitpid, os.waitid

    def waitpid(pid: int, flags: int) -> Any:
        waited.append(("waitpid", (pid,)))
        return real_waitpid(pid, flags)

    def waitid(idtype: int, ident: int, flags: int) -> Any:
        waited.append(("waitid", (idtype, ident)))
        return real_waitid(idtype, ident, flags)

    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(os, "waitid", waitid)
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    runner = make_runner(runners, store, python="/bin/true")
    runner.run_pass()
    pid = runner.children[0].popen.pid
    runner.children[0].popen.wait(timeout=10)
    runner.run_pass()
    assert waited and all(call[1][0] == pid for call in waited if call[0] == "waitpid")
    assert not [c for c in waited if c[0] == "waitid" and c[1][0] == os.P_ALL]


# -- the spawn environment -----------------------------------------------------------------------


def test_an_internal_process_always_gets_home_and_xdg_state_home(
    store: Store, spawned: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `env.inherit` empty the child must still find the parent's state directory."""
    from jobsctl.spawn import spawn_internal

    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("XDG_STATE_HOME", "/somewhere/state")
    monkeypatch.setenv("UNRELATED", "x")
    spawn_internal(store.paths.root, cfg(env={"inherit": []}), None, "run")
    (call,) = spawned
    assert call["env"]["HOME"] == "/home/someone"
    assert call["env"]["XDG_STATE_HOME"] == "/somewhere/state"
    assert "UNRELATED" not in call["env"]


def test_an_internal_process_gets_no_home_the_parent_lacks(
    store: Store, spawned: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.spawn import spawn_internal

    monkeypatch.delenv("HOME", raising=False)
    spawn_internal(store.paths.root, cfg(env={"inherit": []}), None, "run")
    assert "HOME" not in spawned[0]["env"]
