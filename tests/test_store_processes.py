"""The store under real concurrent processes: races with one winner, crashes, busy timeouts.

Every process a test starts is recorded at launch (PID and its own session, so the group id is
the PID) and killed exactly, by group, on teardown. Each runs with a private HOME, XDG and state
dir. A worker prints ``ready``, waits for the ``go`` file so all start together, and prints one
JSON line.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway
from jobsctl.errors import JobsctlError
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_schema import MIGRATIONS
from tests.conftest import validate_against
from tests.store_helpers import insert_row, job_values

WORKER = Path(__file__).with_name("store_worker.py")
TIMEOUT = 90


@dataclass
class Worker:
    proc: subprocess.Popen[str]
    pgid: int  # recorded at launch; equals the pid because the worker leads its own session
    lines: list[dict[str, Any]] = field(default_factory=list)

    def read_line(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, f"worker exited early: {self.proc.poll()} {self._stderr()}"
        parsed = json.loads(line)
        self.lines.append(parsed)
        return parsed

    def _stderr(self) -> str:
        if self.proc.poll() is None or self.proc.stderr is None:
            return ""
        return self.proc.stderr.read()

    def result(self) -> dict[str, Any]:
        out = self.read_line()
        assert self.proc.wait(timeout=TIMEOUT) == 0, self._stderr()
        return out


class Pool:
    def __init__(self, tmp: Path, env: dict[str, str]) -> None:
        self.tmp = tmp
        self.env = env
        self.workers: list[Worker] = []
        self.go_file = tmp / "go"

    def spawn(self, state: Path, cmd: str, *args: str, wid: str | None = None) -> Worker:
        n = len(self.workers)
        proc = gateway.popen(
            [sys.executable, "-I", str(WORKER), cmd, str(state), str(self.go_file),
             wid or f"w{n:02d}", *args],
            env=self.env,
            cwd=str(self.tmp),
            start_new_session=True,
        )  # fmt: skip
        worker = Worker(proc, pgid=proc.pid)
        self.workers.append(worker)
        return worker

    def spawn_many(self, count: int, state: Path, cmd: str, *args: str) -> list[Worker]:
        workers = [self.spawn(state, cmd, *args) for _ in range(count)]
        for w in workers:
            assert w.read_line() == {"ready": True}
        return workers

    def go(self) -> None:
        self.go_file.touch()

    def kill(self, worker: Worker) -> None:
        if worker.proc.poll() is None:  # unreaped leader: its group id is still ours
            os.killpg(worker.pgid, signal.SIGKILL)
        worker.proc.wait(timeout=TIMEOUT)

    def close(self) -> None:
        for w in self.workers:
            self.kill(w)
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
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "jobsctl"


def test_concurrent_first_opens_create_one_valid_database(pool: Pool, state: Path) -> None:
    workers = pool.spawn_many(8, state, "open")
    pool.go()
    assert [w.result() for w in workers] == [{"version": 1}] * 8
    with Store.open(state) as store:
        assert store.query_one("PRAGMA integrity_check")[0] == "ok"  # type: ignore[index]


def test_concurrent_migrations_apply_once(pool: Pool, state: Path) -> None:
    Store.open(state).close()
    workers = pool.spawn_many(8, state, "open", "v2")
    pool.go()
    assert [w.result() for w in workers] == [{"version": 2}] * 8
    with Store.open(state, migrations=MIGRATIONS) as store:
        assert store.current_version() == 2


def test_racing_cas_transitions_have_exactly_one_winner(pool: Pool, state: Path) -> None:
    with Store.open(state) as store, store.write() as tx:
        insert_row(tx, "jobs", job_values("jb-aaaaaaaa"))
    for round_ in range(3):
        workers = pool.spawn_many(12, state, "cas")
        pool.go()
        results = [w.result() for w in workers]
        assert sum(r["won"] for r in results) == 1, results
        with Store.open(state) as store, store.write() as tx:
            row = tx.one("SELECT status, attempt, note FROM jobs")
            assert row is not None and (row["status"], row["attempt"]) == ("starting", 1)
            winner = next(i for i, r in enumerate(results) if r["won"])
            assert row["note"] == f"w{round_ * 12 + winner:02d}"
            tx.execute("UPDATE jobs SET status = 'queued', attempt = 0")  # reset for the next
        pool.go_file.unlink()


def test_racing_inserts_with_one_live_unique_key_leave_one_row(pool: Pool, state: Path) -> None:
    Store.open(state).close()
    workers = pool.spawn_many(12, state, "insert-key", "same-key")
    pool.go()
    results = [w.result() for w in workers]
    assert sum(r["created"] for r in results) == 1
    with Store.open(state) as store:
        assert store.query_one("SELECT count(*) AS c FROM jobs")["c"] == 1  # type: ignore[index]


def test_racing_inserts_with_distinct_keys_all_succeed(pool: Pool, state: Path) -> None:
    Store.open(state).close()
    workers = [pool.spawn(state, "insert-key", f"key-{i}") for i in range(8)]
    for w in workers:
        w.read_line()
    pool.go()
    assert all(w.result()["created"] for w in workers)


def test_counter_allocation_under_contention_is_gap_free_and_unique(
    pool: Pool, state: Path
) -> None:
    Store.open(state).close()
    workers = pool.spawn_many(6, state, "counter", "40")
    pool.go()
    values = sorted(v for w in workers for v in w.result()["values"])
    assert values == list(range(1, 6 * 40 + 1))


@pytest.mark.parametrize(
    ("liveness", "expected_winners"), [("alive", 1), ("unknown", 1), ("dead", None)]
)
def test_racing_lease_takers(
    pool: Pool, state: Path, liveness: str, expected_winners: int | None
) -> None:
    Store.open(state).close()
    workers = pool.spawn_many(8, state, "lease", liveness)
    pool.go()
    wins = sum(w.result()["won"] for w in workers)
    with Store.open(state) as store:
        holder = store.query_one("SELECT holder_identity FROM leases")
        assert holder is not None
    assert wins >= 1
    if expected_winners is not None:
        assert wins == expected_winners


def test_racing_takeover_of_an_expired_lease_has_one_winner(pool: Pool, state: Path) -> None:
    from tests.store_helpers import NOW

    with Store.open(state) as store:
        with store.write() as tx:
            tx.execute(
                "INSERT INTO leases VALUES ('runner', 'stale', ?)", ["2025-12-31T23:59:00.000Z"]
            )
        assert NOW > "2025-12-31T23:59:00.000Z"
    workers = pool.spawn_many(10, state, "lease", "unknown")
    pool.go()
    assert sum(w.result()["won"] for w in workers) == 1


def test_a_busy_database_is_exit_1_busy(pool: Pool, state: Path) -> None:
    Store.open(state).close()
    holder = pool.spawn(state, "hold")
    assert holder.read_line() == {"ready": True, "in_tx": True}
    with Store.open(state, busy_timeout=0.3) as store:
        started = time.monotonic()
        with pytest.raises(JobsctlError) as err, store.write():
            pass
        waited = time.monotonic() - started
    assert err.value.code == "busy"
    assert err.value.exit_code == 1
    assert 0.25 <= waited < 5  # it waited out the busy timeout, then gave up
    validate_against("cli/error.json", err.value.envelope())
    # readers still work, and see no uncommitted row
    with Store.open(state, busy_timeout=0.3) as reader:
        assert reader.query_all("SELECT name FROM queues_seen") == []
    pool.kill(holder)
    with Store.open(state, busy_timeout=2) as store, store.write():
        pass  # the lock died with its holder


def test_a_busy_migration_is_exit_1_busy(pool: Pool, state: Path) -> None:
    Store.open(state).close()
    holder = pool.spawn(state, "hold")
    holder.read_line()
    from tests.test_store_core import V2_COLUMN

    with pytest.raises(JobsctlError) as err:
        Store.open(state, busy_timeout=0.3, migrations=V2_COLUMN)
    assert err.value.code == "busy"
    pool.kill(holder)
    with Store.open(state) as store:
        assert store.current_version() == 1


def test_sigkill_mid_transaction_leaves_nothing_and_no_lock(pool: Pool, state: Path) -> None:
    Store.open(state).close()
    for _ in range(3):
        holder = pool.spawn(state, "hold")
        holder.read_line()
        pool.kill(holder)
        assert holder.proc.returncode == -signal.SIGKILL
        with Store.open(state, busy_timeout=2) as store:
            assert store.query_all("SELECT * FROM queues_seen") == []
            with store.write() as tx:  # the write lock is free at once
                tx.execute("SELECT 1")
            assert store.query_one("PRAGMA integrity_check")[0] == "ok"  # type: ignore[index]


def test_sigkill_while_committing_streams_keeps_transactions_atomic(
    pool: Pool, state: Path
) -> None:
    Store.open(state).close()
    for round_ in range(6):
        writer = pool.spawn(state, "stream")
        writer.read_line()
        time.sleep(0.05 + 0.03 * round_)
        pool.kill(writer)
        with Store.open(state, busy_timeout=2) as store:
            counter = store.query_one("SELECT value FROM counters WHERE name = 'stream'")
            rows = store.query_all("SELECT name FROM queues_seen")
            total = 0 if counter is None else counter["value"]
            assert len(rows) == total  # the counter and its row commit together or not at all
            assert {r["name"] for r in rows} == {f"q{i}" for i in range(1, total + 1)}
            assert store.query_one("PRAGMA integrity_check")[0] == "ok"  # type: ignore[index]
            with store.write() as tx:  # clear for the next round's writer
                tx.execute("DELETE FROM queues_seen")
                tx.execute("DELETE FROM counters")


def test_internal_open_on_an_older_schema_in_another_process_writes_nothing(
    pool: Pool, state: Path
) -> None:
    with Store.open(state) as store:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = (state / "state.db").read_bytes()
    worker = pool.spawn(state, "internal-open")
    assert worker.result() == {"older": True, "found": 1, "known": 2}
    assert (state / "state.db").read_bytes() == before
    with Store.open(state, mode=OpenMode.INTERNAL) as same:  # same version as the db: opens
        assert same.current_version() == 1
