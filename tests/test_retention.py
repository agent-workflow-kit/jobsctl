"""Retention and `prune`: the batch loop, the budget, files, audit records and the CLI command."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import retention
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from jobsctl.timestamps import format_timestamp
from tests.conftest import validate_against, validate_record_file
from tests.helpers import Result, run_cli
from tests.store_helpers import attempt_values, insert_row, job_values

pytestmark = pytest.mark.usefixtures("validated_records")

NOW = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.UTC)
DAY = 86400


def ago(seconds: float) -> str:
    return format_timestamp(NOW - dt.timedelta(seconds=seconds))


def jid(n: int) -> str:
    """The n-th well-formed job id (base32 digits, distinct for distinct n)."""
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return "jb-" + "".join(alphabet[(n >> shift) & 31] for shift in range(35, -1, -5))[-8:]


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as opened:
        yield opened


def add_job(
    store: Store,
    job_id: str,
    *,
    status: str = "done",
    age: float | None = 40 * DAY,
    queue: str = "default",
    attempts: int = 0,
    **over: Any,
) -> None:
    with store.write() as tx:
        ended = None if age is None else ago(age)
        insert_row(
            tx,
            "jobs",
            job_values(
                job_id,
                status=status,
                ended_at=ended,
                queue=queue,
                **{"attempt": attempts, **over},
            ),
        )
        for n in range(1, attempts + 1):
            insert_row(tx, "attempts", attempt_values(job_id, n, f"c{n}", status="done"))
            insert_row(
                tx,
                "outputs",
                {"job_id": job_id, "n": n, "tail": b"x", "updated_at": ago(0)},
            )


def ids_in(store: Store, table: str = "jobs", col: str = "id") -> set[str]:
    return {r[0] for r in store.conn.execute(f"SELECT {col} FROM {table}")}


def audits() -> list[dict[str, Any]]:
    path = resolve_state_dir() / "audit.jsonl"
    return validate_record_file(path, "audit.json") if path.exists() else []


class FakeClock:
    """A monotonic clock that advances by `step` on every read."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def run(
    store: Store,
    older: int = 30 * DAY,
    *,
    trigger: str = "command",
    actor: str = "human:test",
    **kw: Any,
) -> retention.PruneResult:
    return retention.prune_jobs(
        store,
        RecordWriter(store),
        older_than=older,
        trigger=trigger,
        actor=actor,
        decision_ids=kw.pop("decision_ids", []),
        now=NOW,
        **kw,
    )


# -- selection ---------------------------------------------------------------------------------


def test_prunes_only_old_terminal_jobs(store: Store) -> None:
    add_job(store, jid(1), status="done", age=40 * DAY)
    add_job(store, jid(2), status="failed", age=31 * DAY)
    add_job(store, jid(3), status="canceled", age=100 * DAY)
    add_job(store, jid(4), status="done", age=29 * DAY)  # too young
    add_job(store, jid(5), status="queued", age=None)
    add_job(store, jid(6), status="running", age=None)
    add_job(store, jid(7), status="held", age=None)
    add_job(store, jid(8), status="starting", age=None)
    result = run(store)
    assert sorted(result.pruned) == sorted([jid(1), jid(2), jid(3)])
    assert ids_in(store) == {jid(4), jid(5), jid(6), jid(7), jid(8)}


def test_age_boundary_is_exclusive(store: Store) -> None:
    add_job(store, jid(1), age=30 * DAY)  # ended exactly at the cutoff: not before it
    add_job(store, jid(2), age=30 * DAY + 1)
    assert run(store).pruned == [jid(2)]


def test_terminal_job_without_ended_at_is_kept(store: Store) -> None:
    add_job(store, jid(1), age=None)
    assert run(store).pruned == []
    assert ids_in(store) == {jid(1)}


def test_unreadable_ended_at_text_is_kept(store: Store) -> None:
    add_job(store, jid(1), age=None)
    with store.write() as tx:
        tx.execute("UPDATE jobs SET ended_at = 'not a time'")
    assert run(store).pruned == []


@pytest.mark.parametrize("stamp", ["", "0", "2026-06-01"])
def test_non_timestamp_ended_at_is_never_pruned(store: Store, stamp: str) -> None:
    add_job(store, jid(1))
    with store.write() as tx:
        tx.execute("UPDATE jobs SET ended_at = ?", [stamp])
    assert run(store).pruned == []
    assert ids_in(store) == {jid(1)}


def test_oldest_jobs_are_pruned_first(store: Store) -> None:
    for n in range(1, 6):
        add_job(store, jid(n), age=(40 + n) * DAY)
    result = run(store, batch_size=2, monotonic=FakeClock(step=4.0), budget=5.0)
    assert result.pruned == [jid(5), jid(4)]


def test_job_with_undelivered_hook_events_survives(store: Store) -> None:
    add_job(store, jid(1))
    add_job(store, jid(2))
    add_job(store, jid(3))
    with store.write() as tx:
        for n, (job, delivered) in enumerate([(jid(1), None), (jid(2), ago(1))]):
            insert_row(
                tx,
                "hook_outbox",
                {
                    "event_id": f"e{n}",
                    "job_id": job,
                    "hook": "post-fail",
                    "payload": "{}",
                    "created_at": ago(DAY),
                    "delivered_at": delivered,
                },
            )
    result = run(store)
    assert sorted(result.pruned) == sorted([jid(2), jid(3)])
    assert ids_in(store) == {jid(1)}
    assert run(store).pruned == []  # still skipped, and the loop terminates


def test_children_cascade_and_ids_are_reserved(store: Store) -> None:
    add_job(store, jid(1), attempts=2)
    with store.write() as tx:
        tx.execute("INSERT INTO job_labels VALUES (?, 'k', 'v')", [jid(1)])
        insert_row(
            tx,
            "hook_outbox",
            {
                "event_id": "e1",
                "job_id": jid(1),
                "hook": "h",
                "payload": "{}",
                "created_at": ago(DAY),
                "delivered_at": ago(1),
            },
        )
    run(store)
    for table in ("jobs", "attempts", "outputs", "job_labels", "hook_outbox"):
        assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert ids_in(store, "pruned_ids") == {jid(1)}


def test_job_lease_row_is_deleted_and_other_leases_kept(store: Store) -> None:
    add_job(store, jid(1))
    with store.write() as tx:
        for name in (f"job:{jid(1)}", "runner", "deliver", f"job:{jid(9)}"):
            tx.execute("INSERT INTO leases VALUES (?, 'h', ?)", [name, ago(-60)])
    run(store)
    assert ids_in(store, "leases", "name") == {"runner", "deliver", f"job:{jid(9)}"}


def test_counters_are_never_touched(store: Store) -> None:
    add_job(store, jid(1))
    with store.write() as tx:
        for name in ("seq:audit", "seq:log", "warn:abc", "pass_requested"):
            tx.execute("INSERT INTO counters VALUES (?, 7)", [name])
    run(store)
    rows = dict(store.conn.execute("SELECT name, value FROM counters").fetchall())
    assert rows["seq:audit"] >= 7 and rows["seq:log"] == 7
    assert rows["warn:abc"] == 7 and rows["pass_requested"] == 7


def test_pruned_id_is_not_regenerated(store: Store) -> None:
    from jobsctl.commands.add import _taken

    add_job(store, jid(1))
    run(store)
    with store.write() as tx:
        assert _taken(tx, jid(1))


# -- batches and the budget --------------------------------------------------------------------


def count_transactions(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The number of jobs each write transaction removed."""
    sizes: list[int] = []
    real = retention.delete_batch

    def spy(tx: Any, ids: list[str]) -> Any:
        sizes.append(len(ids))
        return real(tx, ids)

    monkeypatch.setattr(retention, "delete_batch", spy)
    return sizes


def test_batches_of_100(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    with store.write() as tx:
        for n in range(250):
            insert_row(tx, "jobs", job_values(jid(n + 1), status="done", ended_at=ago(40 * DAY)))
    sizes = count_transactions(store, monkeypatch)
    result = run(store)
    assert sizes == [100, 100, 50]
    assert len(result.pruned) == 250
    assert [len(a["inputs"]["ids"]) for a in audits()] == [100, 100, 50]


def test_budget_stops_between_batches(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    with store.write() as tx:
        for n in range(350):
            insert_row(tx, "jobs", job_values(jid(n + 1), status="done", ended_at=ago(40 * DAY)))
    sizes = count_transactions(store, monkeypatch)
    # Each clock read advances 4 s: start=0, check before batch 1 = 4, 2 = 8, 3 = 12 (> 10).
    result = run(store, monotonic=FakeClock(step=4.0), budget=retention.BUDGET_SECONDS)
    assert sizes == [100, 100]
    assert result.out_of_budget is True
    assert store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 150
    # The next pass continues where it stopped.
    again = run(store)
    assert len(again.pruned) == 150 and again.out_of_budget is False


def test_default_budget_is_ten_seconds() -> None:
    assert retention.BUDGET_SECONDS == 10.0
    assert retention.BATCH_SIZE == 100


def test_exhausted_budget_still_leaves_no_partial_state(store: Store) -> None:
    add_job(store, jid(1))
    result = run(store, monotonic=FakeClock(step=100.0), budget=retention.BUDGET_SECONDS)
    assert result.pruned == [] and result.out_of_budget is True
    assert ids_in(store) == {jid(1)}
    assert ids_in(store, "pruned_ids") == set()


def test_dry_run_selection_changes_nothing(store: Store) -> None:
    add_job(store, jid(1))
    add_job(store, jid(2), age=DAY)
    with store.read() as tx:
        assert retention.candidates(tx, NOW, 30 * DAY) == [jid(1)]
    assert ids_in(store) == {jid(1), jid(2)}


# -- audit -------------------------------------------------------------------------------------


def test_one_audit_record_per_batch(store: Store) -> None:
    add_job(store, jid(1))
    add_job(store, jid(2))
    run(store, decision_ids=["d-1"])
    [rec] = audits()
    assert rec["action"] == "prune" and rec["target"] is None and rec["result"] == "deleted"
    assert rec["actor"] == "human:test" and rec["decision_id"] == ["d-1"]
    assert sorted(rec["inputs"]["ids"]) == sorted([jid(1), jid(2)])
    assert rec["inputs"]["older_than"] == 30 * DAY and rec["inputs"]["trigger"] == "command"


def test_no_audit_record_when_nothing_is_pruned(store: Store) -> None:
    add_job(store, jid(1), age=DAY)
    run(store)
    assert audits() == []


def test_audit_is_written_after_commit(store: Store) -> None:
    """A write transaction never holds the audit append: it would raise if it did."""
    add_job(store, jid(1))
    seen: list[bool] = []
    real = RecordWriter.audit

    def spy(self: RecordWriter, **kw: Any) -> int:
        seen.append(store.conn.in_transaction)
        return real(self, **kw)

    RecordWriter.audit = spy  # type: ignore[method-assign]
    try:
        run(store)
    finally:
        RecordWriter.audit = real  # type: ignore[method-assign]
    assert seen == [False]


def test_large_batch_is_split_into_several_audit_records(store: Store) -> None:
    total = retention.AUDIT_IDS_PER_RECORD + 5
    with store.write() as tx:
        tx.store.conn.executemany(
            "INSERT INTO jobs (id, queue, argv, cwd, position, status, created_at, enqueued_at,"
            " status_changed_at, ended_at, actor) VALUES (?, 'default', '[]', '/', 'm', 'done',"
            " ?, ?, ?, ?, 'human:t')",
            [
                (
                    jid(n),
                    ago(0),
                    ago(0),
                    ago(0),
                    ago(40 * DAY),
                )
                for n in range(total)
            ],
        )
    result = run(store, batch_size=total)
    assert len(result.pruned) == total
    sizes = [len(a["inputs"]["ids"]) for a in audits()]
    assert sizes == [retention.AUDIT_IDS_PER_RECORD, 5]
    assert len(set(i for a in audits() for i in a["inputs"]["ids"])) == total


def seed_many(store: Store, total: int) -> None:
    with store.write() as tx:
        for n in range(total):
            insert_row(tx, "jobs", job_values(jid(n + 1), status="done", ended_at=ago(40 * DAY)))


def test_audit_failure_keeps_the_result_and_stops(store: Store) -> None:
    seed_many(store, 250)
    (resolve_state_dir() / "audit.jsonl").mkdir()
    result = run(store)
    assert len(result.pruned) == 100  # the committed batch is reported
    assert result.audit_error is not None
    assert len(ids_in(store)) == 150  # no further batch
    assert len(ids_in(store, "pruned_ids")) == 100


def test_command_audit_failure_prints_ids_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with Store.open(mode=OpenMode.CLI) as s:
        seed_many(s, 250)
    (resolve_state_dir() / "audit.jsonl").mkdir()
    res = run_cli(capsys, "prune", "--older-than", "30d")
    assert res.code != 0
    assert len(res.out.split()) == 100
    assert res.err.strip()


def test_retention_step_reports_audit_failure(store: Store) -> None:
    seed_many(store, 3)
    (resolve_state_dir() / "audit.jsonl").mkdir()
    result = retention.run_retention(
        store, RecordWriter(store), retention=30 * DAY, configured_queues=set(), now=NOW
    )
    assert len(result.pruned) == 3 and result.audit_error is not None


def test_retention_step_forwards_budget_and_batch_size(store: Store) -> None:
    seed_many(store, 250)
    result = retention.run_retention(
        store,
        RecordWriter(store),
        retention=30 * DAY,
        configured_queues=set(),
        now=NOW,
        monotonic=FakeClock(step=4.0),
        budget=10.0,
        batch_size=50,
    )
    assert result.out_of_budget is True
    assert 0 < len(result.pruned) < 250 and len(result.pruned) % 50 == 0


def test_retention_trigger_uses_the_reserved_actor(store: Store) -> None:
    add_job(store, jid(1))
    run(store, trigger="retention", actor="auto:jobsctl")
    [rec] = audits()
    assert rec["actor"] == "auto:jobsctl" and rec["inputs"]["trigger"] == "retention"


# -- files -------------------------------------------------------------------------------------


def shim_files(job: str, attempts: int = 2) -> list[str]:
    return [f"{job}.log", *(f"{job}.{n}.result" for n in range(1, attempts + 1))]


def test_removes_shim_files_of_pruned_jobs_only(store: Store) -> None:
    shims = store.paths.shims
    add_job(store, jid(1), attempts=2)
    add_job(store, jid(2), age=DAY, attempts=1)
    for name in [*shim_files(jid(1)), *shim_files(jid(2), 1), "other.log"]:
        (shims / name).write_text("x")
    run(store)
    assert sorted(os.listdir(shims)) == sorted([*shim_files(jid(2), 1), "other.log"])


def test_shim_symlinks_are_removed_but_never_followed(store: Store, tmp_path: Path) -> None:
    shims = store.paths.shims
    add_job(store, jid(1), attempts=1)
    target = tmp_path / "precious"
    target.write_text("keep")
    os.symlink(target, shims / f"{jid(1)}.log")
    os.symlink(tmp_path, shims / f"{jid(1)}.1.result")  # symlink to a directory
    run(store)
    assert target.read_text() == "keep"
    assert os.listdir(shims) == []


def test_shim_directory_named_like_a_file_is_left(store: Store) -> None:
    shims = store.paths.shims
    add_job(store, jid(1), attempts=1)
    (shims / f"{jid(1)}.log").mkdir()
    (shims / f"{jid(1)}.log" / "inner").write_text("x")
    run(store)
    assert (shims / f"{jid(1)}.log" / "inner").exists()
    assert ids_in(store) == set()  # the job is still pruned


def test_replaced_shims_directory_is_not_followed(store: Store, tmp_path: Path) -> None:
    """If `shims/` is swapped for a symlink, nothing outside the state directory is touched."""
    shims = store.paths.shims
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{jid(1)}.log").write_text("keep")
    shims.rmdir()
    os.symlink(outside, shims)
    add_job(store, jid(1))
    run(store)
    assert (outside / f"{jid(1)}.log").read_text() == "keep"
    assert ids_in(store) == set()


def test_malformed_ids_are_never_pruned_or_resolved_to_paths(store: Store, tmp_path: Path) -> None:
    victim = tmp_path / "victim.log"
    victim.write_text("keep")
    bad = "../../../victim"
    with store.write() as tx:
        insert_row(tx, "jobs", job_values(bad, status="done", ended_at=ago(40 * DAY)))
    result = run(store)
    assert result.pruned == [] and ids_in(store) == {bad}
    assert victim.read_text() == "keep"


def test_result_files_of_any_attempt_number_are_removed(store: Store) -> None:
    add_job(store, jid(1), attempts=0, attempt=1500)
    (store.paths.shims / f"{jid(1)}.1500.result").write_text("x")
    (store.paths.shims / f"{jid(1)}.1.result").write_text("x")
    (store.paths.shims / f"{jid(1)}.x.result").write_text("x")  # not an attempt number: kept
    run(store)
    assert os.listdir(store.paths.shims) == [f"{jid(1)}.x.result"]


def test_huge_attempt_number_is_bounded(store: Store) -> None:
    add_job(store, jid(1), attempts=0, attempt=10**9)
    run(store)  # must not loop a billion times
    assert ids_in(store) == set()


def test_diag_files_older_than_seven_days_are_removed(store: Store, tmp_path: Path) -> None:
    diag = store.paths.diag
    old, young, odd = diag / "run-1.log", diag / "run-2.log", diag / "sub"
    old.write_text("x")
    young.write_text("x")
    odd.mkdir()
    (odd / "f").write_text("x")
    link = diag / "run-3.log"
    target = tmp_path / "precious"
    target.write_text("keep")
    os.symlink(target, link)
    for path, age in ((old, 8 * DAY), (young, 6 * DAY), (odd, 9 * DAY)):
        stamp = (NOW - dt.timedelta(seconds=age)).timestamp()
        os.utime(path, (stamp, stamp))
    os.utime(link, (0, 0), follow_symlinks=False)
    removed = retention.sweep_diag(store.paths, NOW)
    assert removed == 2  # the old file and the old symlink (the link itself, not its target)
    assert sorted(os.listdir(diag)) == ["run-2.log", "sub"]
    assert target.read_text() == "keep" and (odd / "f").exists()


def test_orphan_shim_files_of_pruned_ids_are_swept(store: Store, tmp_path: Path) -> None:
    shims = store.paths.shims
    add_job(store, jid(2), age=DAY)  # live job: untouched
    with store.write() as tx:
        tx.execute("INSERT INTO pruned_ids (id) VALUES (?)", [jid(1)])
    target = tmp_path / "precious"
    target.write_text("keep")
    for name in (f"{jid(1)}.log", f"{jid(1)}.7.result", f"{jid(2)}.log", f"{jid(3)}.log"):
        (shims / name).write_text("x")
    os.symlink(target, shims / f"{jid(1)}.9.result")
    assert retention.sweep_orphans(store, cap=100) == 3
    assert sorted(os.listdir(shims)) == sorted([f"{jid(2)}.log", f"{jid(3)}.log"])
    assert target.read_text() == "keep"


def test_orphan_sweep_is_capped(store: Store) -> None:
    with store.write() as tx:
        tx.execute("INSERT INTO pruned_ids (id) VALUES (?)", [jid(1)])
    for n in range(1, 6):
        (store.paths.shims / f"{jid(1)}.{n}.result").write_text("x")
    assert retention.sweep_orphans(store, cap=3) <= 3
    assert len(os.listdir(store.paths.shims)) >= 2


def test_retention_step_sweeps_orphans(store: Store) -> None:
    with store.write() as tx:
        tx.execute("INSERT INTO pruned_ids (id) VALUES (?)", [jid(1)])
    (store.paths.shims / f"{jid(1)}.log").write_text("x")
    retention.run_retention(
        store, RecordWriter(store), retention=30 * DAY, configured_queues=set(), now=NOW
    )
    assert os.listdir(store.paths.shims) == []


def test_diag_fifo_and_foreign_entries_are_left(store: Store) -> None:
    diag = store.paths.diag
    os.mkfifo(diag / "pipe")
    (diag / "old.log").write_text("x")
    for name in ("pipe", "old.log"):
        os.utime(diag / name, (0, 0))
    assert retention.sweep_diag(store.paths, NOW) == 1
    assert os.listdir(diag) == ["pipe"]


def test_diag_sweep_stops_at_the_budget(store: Store) -> None:
    for n in range(5):
        path = store.paths.diag / f"old{n}.log"
        path.write_text("x")
        os.utime(path, (0, 0))
    calls = iter([False, False, True, True, True, True, True])
    removed = retention.sweep_diag(store.paths, NOW, out_of_time=lambda: next(calls))
    assert removed == 2
    assert len(os.listdir(store.paths.diag)) == 3


def test_diag_boundary_and_missing_directory(store: Store) -> None:
    diag = store.paths.diag
    (diag / "run-1.log").write_text("x")
    stamp = (NOW - dt.timedelta(days=7)).timestamp()
    os.utime(diag / "run-1.log", (stamp, stamp))
    assert retention.sweep_diag(store.paths, NOW) == 0  # exactly 7 days: not older
    diag.joinpath("run-1.log").unlink()
    diag.rmdir()
    assert retention.sweep_diag(store.paths, NOW) == 0  # nothing to do


def test_diag_directory_symlink_is_not_followed(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run-1.log").write_text("x")
    os.utime(outside / "run-1.log", (0, 0))
    store.paths.diag.rmdir()
    os.symlink(outside, store.paths.diag)
    assert retention.sweep_diag(store.paths, NOW) == 0
    assert (outside / "run-1.log").exists()


# -- queue records -----------------------------------------------------------------------------


def test_unconfigured_queue_without_jobs_is_forgotten(store: Store) -> None:
    add_job(store, jid(1), queue="gone")
    add_job(store, jid(2), queue="kept", age=DAY)
    with store.write() as tx:
        for name in ("gone", "kept", "configured", "empty"):
            tx.execute("INSERT INTO queues_seen VALUES (?, ?)", [name, ago(DAY)])
    run(store)
    with store.write() as tx:
        removed = retention.forget_queues(tx, configured={"configured"})
    assert sorted(removed) == ["empty", "gone"]
    assert ids_in(store, "queues_seen", "name") == {"kept", "configured"}


# -- the retention step ------------------------------------------------------------------------


def test_retention_step_prunes_sweeps_and_forgets(store: Store) -> None:
    add_job(store, jid(1), queue="gone")
    (store.paths.diag / "run-1.log").write_text("x")
    os.utime(store.paths.diag / "run-1.log", (0, 0))
    with store.write() as tx:
        tx.execute("INSERT INTO queues_seen VALUES ('gone', ?)", [ago(DAY)])
    result = retention.run_retention(
        store, RecordWriter(store), retention=30 * DAY, configured_queues=set(), now=NOW
    )
    assert result.pruned == [jid(1)]
    assert os.listdir(store.paths.diag) == []
    assert ids_in(store, "queues_seen", "name") == set()
    [rec] = audits()
    assert rec["actor"] == "auto:jobsctl" and rec["inputs"]["trigger"] == "retention"
    assert rec["inputs"]["older_than"] == 30 * DAY


def test_retention_null_keeps_jobs_but_still_sweeps_diag(store: Store) -> None:
    add_job(store, jid(1), age=1000 * DAY)
    (store.paths.diag / "run-1.log").write_text("x")
    os.utime(store.paths.diag / "run-1.log", (0, 0))
    result = retention.run_retention(
        store, RecordWriter(store), retention=None, configured_queues=set(), now=NOW
    )
    assert result.pruned == [] and ids_in(store) == {jid(1)}
    assert os.listdir(store.paths.diag) == []


# -- concurrency -------------------------------------------------------------------------------


def test_concurrent_add_never_reuses_a_pruned_id(store: Store) -> None:
    """The delete and the `pruned_ids` insert share one transaction: no window for reuse."""
    from jobsctl.commands.add import _taken

    add_job(store, jid(1))
    real = retention.delete_batch

    def spy(tx: Any, ids: list[str]) -> Any:
        out = real(tx, ids)
        # Still inside the transaction: the id is already both gone and reserved.
        assert tx.one("SELECT 1 FROM jobs WHERE id = ?", [ids[0]]) is None
        assert _taken(tx, ids[0])
        return out

    retention.delete_batch = spy
    try:
        run(store)
    finally:
        retention.delete_batch = real


def test_no_write_lock_is_held_between_batches(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another connection can write while a later batch is about to be selected."""
    with store.write() as tx:
        for n in range(150):
            insert_row(tx, "jobs", job_values(jid(n + 1), status="done", ended_at=ago(40 * DAY)))
    real = retention.remove_job_files
    calls: list[int] = []

    def watching(paths: Any, attempts: Any) -> None:
        # Runs right after a batch's commit: another connection must be able to write now.
        with Store.open(mode=OpenMode.CLI, busy_timeout=0.2) as other, other.write() as otx:
            if not calls:
                insert_row(otx, "jobs", job_values(jid(900), status="queued"))
        calls.append(1)
        real(paths, attempts)

    monkeypatch.setattr(retention, "remove_job_files", watching)
    run(store)
    assert ids_in(store) == {jid(900)}


# -- the command -------------------------------------------------------------------------------


def cli(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(capsys, "--json", "prune", *flags)


def seed_state(capsys: pytest.CaptureFixture[str]) -> None:
    with Store.open(mode=OpenMode.CLI) as s:
        now = dt.datetime.now(dt.UTC)
        with s.write() as tx:
            for n, age in ((1, 40), (2, 35), (3, 5)):
                stamp = format_timestamp(now - dt.timedelta(days=age))
                insert_row(tx, "jobs", job_values(jid(n), status="done", ended_at=stamp))


def test_command_prunes_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    seed_state(capsys)
    res = cli(capsys, "--older-than", "30d", "--actor", "human:op", "--decision-id", "d-9")
    assert res.code == 0, res.err
    doc = res.envelope()
    validate_against("cli/prune.json", doc)
    assert doc["dry_run"] is False and sorted(doc["pruned"]) == sorted([jid(1), jid(2)])
    [rec] = audits()
    assert rec["actor"] == "human:op" and rec["decision_id"] == ["d-9"]
    assert rec["inputs"]["trigger"] == "command" and rec["inputs"]["older_than"] == 30 * DAY


def test_command_dry_run_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    seed_state(capsys)
    res = cli(capsys, "--older-than", "30d", "--dry-run")
    doc = res.envelope()
    validate_against("cli/prune.json", doc)
    assert doc["dry_run"] is True and sorted(doc["pruned"]) == sorted([jid(1), jid(2)])
    assert audits() == []
    with sqlite3.connect(resolve_state_dir() / "state.db") as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM pruned_ids").fetchone()[0] == 0


def test_command_dry_run_without_state(capsys: pytest.CaptureFixture[str]) -> None:
    res = cli(capsys, "--older-than", "30d", "--dry-run")
    assert res.code == 0 and res.envelope() == {"dry_run": True, "pruned": []}
    assert not resolve_state_dir().exists()


def test_command_requires_older_than(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(capsys).code == 2


def test_command_refuses_the_reserved_actor(capsys: pytest.CaptureFixture[str]) -> None:
    res = cli(capsys, "--older-than", "1d", "--actor", "auto:jobsctl")
    assert res.code == 2


def test_command_human_output_lists_ids(capsys: pytest.CaptureFixture[str]) -> None:
    seed_state(capsys)
    res = run_cli(capsys, "prune", "--older-than", "30d")
    assert res.code == 0 and sorted(res.out.split()) == sorted([jid(1), jid(2)])


def test_command_nothing_to_prune(capsys: pytest.CaptureFixture[str]) -> None:
    res = cli(capsys, "--older-than", "30d")
    assert res.code == 0 and res.envelope() == {"dry_run": False, "pruned": []}
    assert json.loads(res.out)["pruned"] == []


def test_command_does_not_forget_queues(capsys: pytest.CaptureFixture[str]) -> None:
    seed_state(capsys)
    with Store.open(mode=OpenMode.CLI) as s, s.write() as tx:
        tx.execute("INSERT INTO queues_seen VALUES ('idle', ?)", [ago(DAY)])
    assert cli(capsys, "--older-than", "30d").code == 0
    with Store.open(mode=OpenMode.CLI) as s:
        assert "idle" in ids_in(s, "queues_seen", "name")
