"""The reconcile rules over a scripted host (docs/interface.md, Reconcile).

Every PID, start time and boot id is chosen by the test; nothing real is signaled, and every test
asserts that reconcile itself sent no signal (`host.sent() == []`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway
from jobsctl.config import Config
from jobsctl.leases import expiry
from jobsctl.reconcile import Reconciler, Step, run_rules
from jobsctl.result_file import ResultRecord, ResultTail, result_name, write_result
from jobsctl.shim_exit import ExitRecord
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_ops import take_lease
from jobsctl.timestamps import format_timestamp
from tests.conftest import validate_record_file
from tests.proc_fakes import FakeHost, proc
from tests.shim_helpers import CLAIM, JOB, attempt, job, log_records, seed_claimed, shims_dir
from tests.store_helpers import attempt_values, insert_row, job_values

ME = os.getpid()
SHIM, LEADER, MEMBER, PASS = 900, 500, 501, 800
START, BOOT = 1000, "boot-1"


def ident(pid: int, start: int = START, boot: str = BOOT) -> str:
    return f"{boot}:{pid}:{start}"


class Rig:
    def __init__(self, store: Store, host: FakeHost) -> None:
        self.store = store
        self.host = host
        self.warnings: list[str] = []
        self.kills: list[str] = []

    def reconciler(self, config: Config | None = None) -> Reconciler:
        def spawn(job_id: str) -> subprocess.Popen[bytes] | None:
            self.kills.append(job_id)
            return None

        return Reconciler(
            config or Config(auto_run=False),
            self.host,
            self.store.paths.shims,
            warn=self.warnings.append,
            spawn_kill=spawn,
        )

    def run(self, *, dry_run: bool = False, config: Config | None = None) -> list[Step]:
        applied = run_rules(
            self.reconciler(config),
            read=self.store.read,
            write=self.store.write,
            clock=gateway.utc_now,
            dry_run=dry_run,
            emit=self._emit,
        )
        assert self.host.sent() == []  # reconcile signals nothing
        return applied

    def _emit(self, records: Any) -> None:
        from jobsctl.log_writer import RecordWriter

        writer = RecordWriter(self.store)
        for r in records:
            writer.log(r.event, level=r.level, fields=r.fields)

    def findings(self, **kw: Any) -> list[tuple[str, str]]:
        return [(a.finding.finding, a.finding.action) for a in self.run(**kw)]


@pytest.fixture
def rig() -> Any:
    host = FakeHost(
        procs={
            ME: proc(ME),
            SHIM: proc(SHIM),
            LEADER: proc(LEADER),
            MEMBER: proc(MEMBER, pgid=LEADER),
            PASS: proc(PASS),
        },
        pidfd_ok=False,
        boot=BOOT,
    )
    with Store.open(mode=OpenMode.CLI) as store:
        seed_claimed(store, ["sleep", "600"], claimed_by=ident(PASS), shim_identity=ident(SHIM))
        yield Rig(store, host)


def confirm(rig: Rig, at: str | None = "2026-01-01T00:00:00.000Z") -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET shim_confirmed_at = ?", [at])


def running(rig: Rig, **over: Any) -> None:
    values = {
        "status": "running",
        "shim_confirmed_at": "2026-01-01T00:00:00.000Z",
        "job_identity": ident(LEADER),
        "pgid": LEADER,
        "shim_identity": ident(SHIM),
    }
    values.update(over)
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET status = 'running' WHERE id = ?", [JOB])
        cols = ", ".join(f"{k} = ?" for k in values)
        tx.execute(f"UPDATE attempts SET {cols}", list(values.values()))  # noqa: S608


def status(rig: Rig) -> tuple[str, str | None]:
    row = job(rig.store)
    return row["status"], row["reason"]


def exit_of(rig: Rig) -> Any:
    raw = attempt(rig.store)["exit"]
    return None if raw is None else json.loads(raw)


def kill_shim(rig: Rig) -> None:
    del rig.host.procs[SHIM]


def kill_leader(rig: Rig) -> None:
    del rig.host.procs[LEADER]


def dump(rig: Rig) -> list[tuple[Any, ...]]:
    out = []
    for table in ("jobs", "attempts", "hook_outbox", "leases", "counters", "outputs"):
        out += [tuple(r) for r in rig.store.query_all(f"SELECT * FROM {table}")]  # noqa: S608
    return out


# -- claim_orphaned ----------------------------------------------------------------------------


def test_an_unconfirmed_dead_shim_requeues_the_claim(rig: Rig) -> None:
    kill_shim(rig)
    assert rig.findings() == [("claim_orphaned", "requeued")]
    assert status(rig) == ("queued", None)
    assert job(rig.store)["attempt"] == 0 and attempt(rig.store) is None
    assert job(rig.store)["spawn_failures"] == 0  # not a spawn failure


def test_a_live_unconfirmed_shim_is_left_alone(rig: Rig) -> None:
    assert rig.findings() == []
    assert status(rig)[0] == "starting"


def test_an_unknown_shim_identity_is_left_alone(rig: Rig) -> None:
    rig.host.info_errors[SHIM] = PermissionError("hidepid")
    del rig.host.procs[SHIM]
    assert rig.findings() == []
    assert status(rig)[0] == "starting"


def test_a_dead_claimer_without_a_shim_identity_requeues(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET shim_identity = NULL")
    del rig.host.procs[PASS]
    assert rig.findings() == [("claim_orphaned", "requeued")]
    assert status(rig)[0] == "queued"


def test_a_live_claimer_without_a_shim_identity_is_left_alone(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET shim_identity = NULL")
    assert rig.findings() == []


def test_a_reused_claimer_pid_counts_as_dead(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET shim_identity = NULL")
    rig.host.procs[PASS] = proc(PASS, start=START + 9)
    assert rig.findings() == [("claim_orphaned", "requeued")]


def test_a_canceled_starting_job_is_not_requeued(rig: Rig) -> None:
    kill_shim(rig)
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET status = 'canceled', reason = 'canceled'")
    assert rig.findings() == []
    assert status(rig) == ("canceled", "canceled") and attempt(rig.store) is not None


def test_the_requeued_job_keeps_its_position_unless_a_newer_job_took_the_key(rig: Rig) -> None:
    key = "5".zfill(20)
    kill_shim(rig)
    with rig.store.write() as tx:
        insert_row(tx, "jobs", job_values("jb-bbbbbbbb", queue="q", position=key))
        tx.execute("UPDATE jobs SET position = ? WHERE id = ?", [key, JOB])
    rig.run()
    assert job(rig.store)["position"] > key  # the nearest free key after it


# -- shim_dead ---------------------------------------------------------------------------------


def test_a_confirmed_dead_shim_fails_the_job_lost(rig: Rig) -> None:
    confirm(rig)
    kill_shim(rig)
    assert rig.findings() == [("shim_dead", "failed_lost")]
    assert status(rig) == ("failed", "lost")
    assert exit_of(rig) is None
    assert attempt(rig.store)["status"] == "failed" and attempt(rig.store)["reason"] == "lost"
    assert job(rig.store)["ended_at"] is not None


def test_shim_dead_is_retried_when_the_attempt_has_budget(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET overrides = ?", [json.dumps({"max_retries": 2})])
    confirm(rig)
    kill_shim(rig)
    assert rig.findings() == [("shim_dead", "failed_lost")]
    row = job(rig.store)
    assert row["status"] == "queued" and row["not_before"] is not None and row["auto_retries"] == 1
    assert attempt(rig.store)["status"] == "failed"  # the attempt is recorded either way


def test_a_live_confirmed_shim_is_never_finalized(rig: Rig) -> None:
    confirm(rig)
    assert rig.findings() == []
    assert status(rig)[0] == "starting"


def test_losses_write_their_post_hook_event_with_the_transition(rig: Rig) -> None:
    from tests.conftest import validate_against

    cfg = Config.model_validate({"auto_run": False, "hooks": {"post-fail": [{"cmd": "true"}]}})
    confirm(rig)
    kill_shim(rig)
    rig.run(config=cfg)
    (row,) = rig.store.query_all("SELECT hook, payload FROM hook_outbox")
    payload = json.loads(row["payload"])
    validate_against("hooks/payload.json", payload)
    assert row["hook"] == "post-fail" and payload["previous_status"] == "starting"
    assert payload["job"]["status"] == "failed" and payload["attempt"]["reason"] == "lost"


# -- boot_changed ------------------------------------------------------------------------------


def test_boot_changed_requeues_an_unconfirmed_claim(rig: Rig) -> None:
    rig.host.boot = "boot-2"
    assert rig.findings() == [("boot_changed", "requeued")]
    assert status(rig)[0] == "queued"


def test_boot_changed_fails_a_confirmed_starting_job_lost(rig: Rig) -> None:
    confirm(rig)
    rig.host.boot = "boot-2"
    assert rig.findings() == [("boot_changed", "failed_lost")]
    assert status(rig) == ("failed", "lost") and exit_of(rig) is None


def test_boot_changed_fails_a_running_job_lost_with_an_unknown_exit(rig: Rig) -> None:
    running(rig)
    rig.host.boot = "boot-2"
    assert rig.findings() == [("boot_changed", "failed_lost")]
    assert status(rig) == ("failed", "lost")
    assert exit_of(rig)["kind"] == "unknown"
    assert attempt(rig.store)["leftovers"] is None


def test_boot_changed_with_a_force_request_cancels_forced(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    rig.host.boot = "boot-2"
    assert rig.findings() == [("boot_changed", "canceled_forced")]
    assert status(rig) == ("canceled", "forced")


def test_an_unreadable_boot_id_does_nothing(rig: Rig) -> None:
    running(rig)
    rig.host.boot = None
    assert rig.findings() == []
    assert status(rig)[0] == "running"


# -- job_gone, shim_lost, force_resumed --------------------------------------------------------


def test_job_gone_counts_the_leftovers_of_a_verifiable_group(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    assert rig.findings() == [("job_gone", "failed_lost")]
    assert status(rig) == ("failed", "lost")
    assert exit_of(rig)["kind"] == "unknown"
    assert attempt(rig.store)["leftovers"] == 1  # MEMBER is still in the group
    assert [r["details"]["count"] for r in log_records("leftovers")] == [1]


def test_job_gone_leaves_the_leftovers_null_when_the_pid_was_reused(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    rig.host.procs[LEADER] = proc(LEADER, start=START + 5)
    assert rig.findings() == [("job_gone", "failed_lost")]
    assert attempt(rig.store)["leftovers"] is None
    assert rig.host.procs[LEADER].start == START + 5  # untouched


def test_job_gone_counts_zero_for_an_empty_group(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    del rig.host.procs[MEMBER]
    rig.run()
    assert attempt(rig.store)["leftovers"] == 0


def test_job_gone_with_a_hidden_proc_is_not_proof(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    del rig.host.procs[MEMBER]
    rig.host.hidden = True
    assert rig.findings() == []  # the identity check is inconclusive: nothing happens
    assert status(rig)[0] == "running"


def test_job_gone_with_a_force_request_cancels_forced(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    kill_leader(rig)
    assert rig.findings() == [("job_gone", "canceled_forced")]
    assert status(rig) == ("canceled", "forced")


def test_job_gone_also_applies_with_shim_lost_set(rig: Rig) -> None:
    running(rig)
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET shim_lost = 1")
    kill_shim(rig)
    kill_leader(rig)
    assert rig.findings() == [("job_gone", "failed_lost")]


def test_job_gone_is_retried_with_budget(rig: Rig) -> None:
    running(rig)
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET overrides = ?", [json.dumps({"max_retries": 1})])
    kill_shim(rig)
    kill_leader(rig)
    rig.run()
    assert status(rig)[0] == "queued" and job(rig.store)["shim_lost"] == 0


def test_a_forced_lost_attempt_is_never_retried(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET overrides = ?", [json.dumps({"max_retries": 5})])
    kill_shim(rig)
    kill_leader(rig)
    rig.run()
    assert status(rig) == ("canceled", "forced")


def test_a_live_job_without_its_shim_is_marked_shim_lost_once(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    assert rig.findings() == [("shim_lost", "marked_shim_lost")]
    assert status(rig)[0] == "running" and job(rig.store)["shim_lost"] == 1
    assert len(log_records("shim_lost")) == 1
    assert rig.findings() == []  # not yet marked only
    assert len(log_records("shim_lost")) == 1


def test_a_slow_shim_is_not_marked_lost(rig: Rig) -> None:
    running(rig)
    assert rig.findings() == []
    assert job(rig.store)["shim_lost"] == 0


def test_an_unknown_job_identity_changes_nothing(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    rig.host.info_errors[LEADER] = PermissionError("hidepid")
    assert rig.findings() == []
    assert job(rig.store)["shim_lost"] == 0


def test_a_running_job_without_a_job_identity_is_left_alone(rig: Rig) -> None:
    running(rig, job_identity=None)
    kill_shim(rig)
    assert rig.findings() == []


def test_force_resumed_starts_a_kill_for_a_request_without_a_holder(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    assert rig.findings() == [("shim_lost", "marked_shim_lost"), ("force_resumed", "kill_started")]
    assert rig.kills == [JOB]
    assert status(rig)[0] == "running"  # reconcile finalizes nothing here: `_kill` does


def test_force_resumed_waits_for_a_live_lease_holder(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    now = gateway.utc_now()
    assert take_lease(
        rig.store,
        f"job:{JOB}",
        ident(PASS),  # a live process holds the job lease (a running `_kill`)
        now=format_timestamp(now),
        expires_at=expiry(now),
        is_alive=lambda holder: True,
    )
    assert ("force_resumed", "kill_started") not in rig.findings()
    assert rig.kills == []


def test_force_resumed_ignores_a_dead_lease_holder(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    now = gateway.utc_now()
    take_lease(
        rig.store,
        f"job:{JOB}",
        ident(4242),
        now=format_timestamp(now),
        expires_at=expiry(now),
        is_alive=lambda holder: False,
    )
    assert ("force_resumed", "kill_started") in rig.findings()


def test_force_resumed_needs_a_proven_dead_shim(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    assert rig.findings() == []  # the shim is alive: it acts on the request itself
    assert rig.kills == []


def test_no_force_request_means_no_kill(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    rig.run()
    assert rig.kills == []


# -- result_file_finalized ---------------------------------------------------------------------


def write(rig: Rig, **over: Any) -> Path:
    exit_record = over.pop("exit", ExitRecord("exited", code=0))
    values: dict[str, Any] = {
        "job_id": JOB,
        "attempt": 1,
        "claim_id": CLAIM,
        "status": "done",
        "reason": "exit",
        "hold_reason": None,
        "exit": exit_record,
        "leftovers": 2,
        "tail": ResultTail("hello\n", 6, False),
        "ended_at": "2026-01-01T00:00:05.000Z",
    }
    values.update(over)
    write_result(rig.store.paths.shims, ResultRecord(**values))
    return shims_dir() / result_name(JOB, 1)


def test_a_result_file_finalizes_the_attempt_and_is_deleted(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    path = write(rig)
    assert rig.findings() == [("result_file_finalized", "finalized")]
    assert status(rig) == ("done", "exit")
    assert attempt(rig.store)["leftovers"] == 2 and exit_of(rig)["code"] == 0
    assert not path.exists()
    from tests.shim_helpers import tail_of

    assert tail_of(rig.store) == "hello\n"


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        (
            {
                "status": "held",
                "reason": None,
                "hold_reason": "exit_13",
                "exit": ExitRecord("exited", code=13),
            },
            ("held", None),
        ),
        (
            {"status": "failed", "reason": "exit", "exit": ExitRecord("exited", code=3)},
            ("failed", "exit"),
        ),
        (
            {"status": "failed", "reason": "signaled", "exit": ExitRecord("signaled", signal=9)},
            ("failed", "signaled"),
        ),
        (
            {"status": "failed", "reason": "timeout", "exit": ExitRecord("signaled", signal=15)},
            ("failed", "timeout"),
        ),
        (
            {"status": "failed", "reason": "terminated", "exit": ExitRecord("signaled", signal=15)},
            ("failed", "terminated"),
        ),
    ],
)
def test_the_file_records_the_transition(rig: Rig, over: dict[str, Any], expected: Any) -> None:
    running(rig)
    kill_shim(rig)
    write(rig, **over)
    rig.run()
    assert status(rig) == expected


def test_a_force_request_committed_after_the_file_wins(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    write(rig)
    assert rig.findings() == [("result_file_finalized", "finalized")]
    assert status(rig) == ("canceled", "forced")


def test_a_failed_file_outcome_is_retried_like_the_shim_would(rig: Rig) -> None:
    running(rig)
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET overrides = ?", [json.dumps({"max_retries": 1})])
    kill_shim(rig)
    write(rig, status="failed", reason="exit", exit=ExitRecord("exited", code=2))
    rig.run()
    assert status(rig)[0] == "queued"


def test_a_live_shim_keeps_its_result_file(rig: Rig) -> None:
    running(rig)
    path = write(rig)
    assert rig.findings() == []
    assert path.exists() and status(rig)[0] == "running"


def test_a_file_of_another_claim_is_ignored(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    path = write(rig, claim_id="claim-other")
    assert rig.findings() == [("job_gone", "failed_lost")]
    assert path.exists() and any("another claim" in w for w in rig.warnings)


def test_a_symlinked_file_is_ignored_and_never_followed(rig: Rig, tmp_path: Path) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    target = tmp_path / "elsewhere"
    target.write_text("{}")
    os.symlink(target, shims_dir() / result_name(JOB, 1))
    assert rig.findings() == [("job_gone", "failed_lost")]
    assert target.read_text() == "{}" and rig.warnings


@pytest.mark.parametrize(
    "content", [b"not json", b"{}", b"[]", b'{"version": 2}', b"[" * 1_000_000]
)
def test_a_damaged_file_is_ignored(rig: Rig, content: bytes) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    path = shims_dir() / result_name(JOB, 1)
    path.write_bytes(content)
    path.chmod(0o600)
    assert rig.findings() == [("job_gone", "failed_lost")]
    assert rig.warnings


def test_a_result_file_end_time_is_the_files_own(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    write(rig, ended_at="2026-01-01T00:00:05.000Z")
    rig.run()
    assert attempt(rig.store)["ended_at"] == "2026-01-01T00:00:05.000Z"
    assert job(rig.store)["ended_at"] == "2026-01-01T00:00:05.000Z"
    assert job(rig.store)["status_changed_at"] != "2026-01-01T00:00:05.000Z"  # reconcile time


@pytest.mark.parametrize("forged", ["2999-01-01T00:00:00.000Z", "2000-01-01T00:00:00.000Z"])
def test_a_forged_result_file_end_time_is_clamped_to_now(rig: Rig, forged: str) -> None:
    running(rig)
    kill_shim(rig)
    write(rig, ended_at=forged)
    before = format_timestamp(gateway.utc_now())
    rig.run()
    ended = attempt(rig.store)["ended_at"]
    assert before <= ended <= format_timestamp(gateway.utc_now())
    assert job(rig.store)["ended_at"] == ended


def test_a_lost_outcome_keeps_the_reconcile_time(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    rig.run()
    assert attempt(rig.store)["ended_at"] >= "2026-06"  # now, not any file's time


def test_a_file_with_loose_mode_is_ignored(rig: Rig) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    write(rig).chmod(0o644)
    assert rig.findings() == [("job_gone", "failed_lost")]


@pytest.mark.parametrize(
    "over",
    [
        {"status": "done", "reason": "forced"},
        {"status": "failed", "reason": "lost", "exit": ExitRecord("unknown")},
        {"status": "done", "reason": "exit", "exit": ExitRecord("exited", code=7)},
        {"status": "failed", "reason": "exit", "exit": ExitRecord("exited", code=0)},
        {"status": "failed", "reason": "signaled", "exit": ExitRecord("exited", code=1)},
        {"status": "failed", "reason": "exit", "exit": ExitRecord("signaled", signal=9)},
    ],
)
def test_an_outcome_no_shim_writes_is_ignored(rig: Rig, over: dict[str, Any]) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    write(rig, **over)
    assert rig.findings() == [("job_gone", "failed_lost")]
    assert any("no shim writes" in w for w in rig.warnings)


def test_a_file_for_a_starting_job_is_not_used(rig: Rig) -> None:
    confirm(rig)
    kill_shim(rig)
    path = write(rig)
    assert rig.findings() == [("shim_dead", "failed_lost")]
    assert path.exists()  # not ours to delete: the prune sweep removes it with the job


# -- idempotence and dry runs ------------------------------------------------------------------


def test_a_second_run_repeats_no_marking_or_finalization(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    assert rig.findings() == [("shim_lost", "marked_shim_lost"), ("force_resumed", "kill_started")]
    assert rig.findings() == [("force_resumed", "kill_started")]  # until a `_kill` holds the lease
    assert job(rig.store)["shim_lost"] == 1 and len(log_records("shim_lost")) == 1


def test_a_second_run_after_finalization_is_empty(rig: Rig) -> None:
    confirm(rig)
    kill_shim(rig)
    rig.run()
    before = dump(rig)
    assert rig.findings() == []
    assert dump(rig) == before


@pytest.mark.parametrize("scenario", ["claim", "shim_dead", "job_gone", "shim_lost", "result"])
def test_a_dry_run_reports_what_a_run_does_and_writes_nothing(rig: Rig, scenario: str) -> None:
    if scenario == "claim":
        kill_shim(rig)
    elif scenario == "shim_dead":
        confirm(rig)
        kill_shim(rig)
    elif scenario == "shim_lost":
        running(rig)
        kill_shim(rig)
    else:
        running(rig)
        kill_shim(rig)
        if scenario == "job_gone":
            kill_leader(rig)
        else:
            write(rig)
    before = dump(rig)
    projected = rig.findings(dry_run=True)
    assert dump(rig) == before and rig.kills == []
    assert projected
    assert projected == rig.findings()


def test_a_dry_run_that_would_kill_spawns_nothing(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    assert ("force_resumed", "kill_started") in rig.findings(dry_run=True)
    assert rig.kills == []


# -- robustness --------------------------------------------------------------------------------


def test_one_bad_job_does_not_stop_the_others(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    other = "jb-bbbbbbbb"
    with rig.store.write() as tx:
        insert_row(
            tx, "jobs", job_values(other, queue="q", status="starting", attempt=1, position="n")
        )
        insert_row(
            tx,
            "attempts",
            attempt_values(other, 1, "claim-2", claimed_by=ident(PASS), shim_identity=ident(SHIM)),
        )
    kill_shim(rig)
    real = Reconciler.apply

    def flaky(self: Reconciler, tx: Any, d: Any, now: str) -> Any:
        if d.subject.job_id == JOB:
            raise OSError("disk")
        return real(self, tx, d, now)

    monkeypatch.setattr(Reconciler, "apply", flaky)
    failures: list[str] = []
    applied = run_rules(
        rig.reconciler(),
        read=rig.store.read,
        write=rig.store.write,
        clock=gateway.utc_now,
        dry_run=False,
        fail=lambda job_id, rule, exc: failures.append(job_id),
    )
    assert [a.finding.job_id for a in applied] == [other] and failures == [JOB]
    assert status(rig)[0] == "starting"


def test_a_failed_rule_skips_the_job_for_the_rest_of_the_run(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    running(rig)
    kill_shim(rig)
    kill_leader(rig)
    write(rig)  # result_file_finalized fails; job_gone must not then end it as lost
    real = Reconciler.apply
    calls: list[str] = []

    def flaky(self: Reconciler, tx: Any, d: Any, now: str) -> Any:
        calls.append(d.finding)
        if len(calls) == 1:
            raise OSError("disk")
        return real(self, tx, d, now)

    monkeypatch.setattr(Reconciler, "apply", flaky)
    failures: list[str] = []
    applied = run_rules(
        rig.reconciler(),
        read=rig.store.read,
        write=rig.store.write,
        clock=gateway.utc_now,
        dry_run=False,
        fail=lambda job_id, rule, exc: failures.append(rule),
    )
    assert applied == [] and failures == ["result_file_finalized"]
    assert calls == ["result_file_finalized"]
    assert status(rig)[0] == "running"


def test_any_error_of_a_rule_is_a_per_job_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    kill_shim(rig)

    def boom(self: Reconciler, *a: Any) -> Any:
        raise RecursionError("deep")

    monkeypatch.setattr(Reconciler, "decide", boom)
    failures: list[str] = []
    applied = run_rules(
        rig.reconciler(),
        read=rig.store.read,
        write=rig.store.write,
        clock=gateway.utc_now,
        dry_run=False,
        fail=lambda job_id, rule, exc: failures.append(type(exc).__name__),
    )
    assert applied == [] and failures == ["RecursionError"]


def test_the_run_stops_after_its_time_budget(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    kill_shim(rig)
    ticks = iter([0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0])
    monkeypatch.setattr(gateway, "monotonic", lambda: next(ticks))
    applied = run_rules(
        rig.reconciler(),
        read=rig.store.read,
        write=rig.store.write,
        clock=gateway.utc_now,
        dry_run=False,
        budget=20.0,
    )
    assert applied == [] and status(rig)[0] == "starting"


def test_the_tick_can_stop_the_run(rig: Rig) -> None:
    kill_shim(rig)

    class Stop(Exception):
        pass

    def tick() -> None:
        raise Stop

    with pytest.raises(Stop):
        run_rules(
            rig.reconciler(),
            read=rig.store.read,
            write=rig.store.write,
            clock=gateway.utc_now,
            dry_run=False,
            tick=tick,
        )
    assert status(rig)[0] == "starting"


def test_every_log_record_matches_the_schema(rig: Rig) -> None:
    running(rig, force_requested_by="human:t")
    kill_shim(rig)
    rig.run()
    kill_leader(rig)
    rig.run()
    path = shims_dir().parent / "log.jsonl"
    records = validate_record_file(path, "log.json")
    events = [r["event"] for r in records]
    assert "reconcile" in events and "shim_lost" in events and "transition" in events
