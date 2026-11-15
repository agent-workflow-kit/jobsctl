"""The record writers under real concurrent processes (spike C6) and crashes.

Every process a test starts is recorded at launch (PID, which is also its group id because it
leads its own session) and killed exactly, by group, on teardown. Each runs with a private HOME,
XDG and state dir. Child processes write past the in-process ``validated_records`` check, so
each test validates every line of the files it produced with ``validate_record_file``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import Store
from tests.conftest import validate_record_file
from tests.store_helpers import popen

WORKER = Path(__file__).with_name("log_worker.py")
TIMEOUT = 120


@dataclass
class Worker:
    proc: subprocess.Popen[str]
    pgid: int

    def read_line(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, f"worker exited early: {self.proc.poll()}"
        parsed: dict[str, Any] = json.loads(line)
        return parsed

    def result(self) -> dict[str, Any]:
        out = self.read_line()
        assert self.proc.wait(timeout=TIMEOUT) == 0
        return out


class Pool:
    def __init__(self, tmp: Path, env: dict[str, str]) -> None:
        self.tmp, self.env = tmp, env
        self.workers: list[Worker] = []
        self.go_file = tmp / "go"

    def spawn(self, state: Path, cmd: str, count: int = 0) -> Worker:
        proc = popen(
            [sys.executable, "-I", str(WORKER), cmd, str(state), str(self.go_file),
             f"w{len(self.workers):02d}", str(count)],
            env=self.env,
            cwd=str(self.tmp),
        )  # fmt: skip
        worker = Worker(proc, pgid=proc.pid)
        self.workers.append(worker)
        assert worker.read_line() == {"ready": True}
        return worker

    def go(self) -> None:
        self.go_file.touch()

    def close(self) -> None:
        for w in self.workers:
            if w.proc.poll() is None:
                os.killpg(w.pgid, signal.SIGKILL)
            w.proc.wait(timeout=TIMEOUT)
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
def state(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "state" / "jobsctl"
    Store.open(path).close()
    yield path


def log_records(state: Path, *, torn: int = 0) -> list[dict[str, Any]]:
    return validate_record_file(state / "log.jsonl", "log.json", torn=torn)


def audit_records(state: Path) -> list[dict[str, Any]]:
    return validate_record_file(state / "audit.jsonl", "audit.json")


def test_the_file_check_fails_on_a_schema_invalid_line(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"seq": 1, "ts": "2026-11-03T12:00:00.000Z", "level": "info", "event": "pass"}\n'
        '{"seq": 2, "ts": "2026-11-03T12:00:00.000Z", "level": "loud", "event": "pass"}\n',
        encoding="ascii",
    )
    with pytest.raises(ValidationError):
        validate_record_file(path, "log.json")


def test_the_file_check_counts_torn_fragments(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"seq": 1, "ts": "2026-11-03T12:00:00.000Z", "level": "info", "event": "pass"}\n'
        '{"seq": 2, "ts": "2026-11\n',
        encoding="ascii",
    )
    assert [r["seq"] for r in validate_record_file(path, "log.json", torn=1)] == [1]
    with pytest.raises(AssertionError):
        validate_record_file(path, "log.json")


def test_ten_writers_by_300_lines_give_gap_free_seq_in_file_order(pool: Pool, state: Path) -> None:
    workers = [pool.spawn(state, "write", 300) for _ in range(10)]
    pool.go()
    results = [w.result() for w in workers]
    records = log_records(state)
    assert [r["seq"] for r in records] == list(range(1, 3001))  # file order, no gap
    for result in results:  # each writer saw its own numbers strictly increasing
        assert result["seqs"] == sorted(set(result["seqs"]))
    timestamps = [r["ts"] for r in records]
    assert timestamps == sorted(timestamps)  # taken under the lock, so also in file order


def test_concurrent_audit_writers_are_gap_free(pool: Pool, state: Path) -> None:
    workers = [pool.spawn(state, "audit", 40) for _ in range(6)]
    pool.go()
    for w in workers:
        w.result()
    assert [r["seq"] for r in audit_records(state)] == list(range(1, 241))


def test_log_and_audit_counters_are_independent(pool: Pool, state: Path) -> None:
    a, b = pool.spawn(state, "write", 20), pool.spawn(state, "audit", 20)
    pool.go()
    a.result()
    b.result()
    assert [r["seq"] for r in log_records(state)] == list(range(1, 21))
    assert [r["seq"] for r in audit_records(state)] == list(range(1, 21))


def test_a_crash_after_the_increment_leaves_only_a_gap(pool: Pool, state: Path) -> None:
    with Store.open(state) as store:
        RecordWriter(store).log("pass")
    dead = pool.spawn(state, "die_before_append")
    pool.go()
    assert dead.proc.wait(timeout=TIMEOUT) == 9
    with Store.open(state) as store:
        assert RecordWriter(store).log("pass") == 3
    assert [r["seq"] for r in log_records(state)] == [1, 3]


def test_a_crash_mid_line_does_not_damage_the_next_record(pool: Pool, state: Path) -> None:
    with Store.open(state) as store:
        RecordWriter(store).log("pass")
    dead = pool.spawn(state, "die_mid_line")
    pool.go()
    assert dead.proc.wait(timeout=TIMEOUT) == 9
    with Store.open(state) as store:
        assert RecordWriter(store).log("pass", fields={"message": "after"}) == 3
    assert [r["seq"] for r in log_records(state, torn=1)] == [1, 3]


def test_the_lock_dies_with_its_holder(pool: Pool, state: Path) -> None:
    holder = pool.spawn(state, "hold_lock")
    pool.go()
    assert holder.read_line() == {"locked": True}
    os.killpg(holder.pgid, signal.SIGKILL)  # recorded at launch; this exact group
    holder.proc.wait(timeout=TIMEOUT)
    with Store.open(state) as store:
        assert RecordWriter(store).log("pass") == 1  # not blocked by the dead holder
    log_records(state)


def test_a_writer_waits_for_the_lock_and_then_proceeds(pool: Pool, state: Path) -> None:
    holder = pool.spawn(state, "hold_lock")
    pool.go()
    assert holder.read_line() == {"locked": True}
    waiter = pool.spawn(state, "write", 1)  # starts at once: the go file exists
    with pytest.raises(subprocess.TimeoutExpired):
        waiter.proc.wait(timeout=0.5)  # blocked on the lock, nothing written
    assert not (state / "log.jsonl").read_text(encoding="ascii")
    (pool.tmp / "go2").touch()
    assert waiter.result() == {"seqs": [1]}
    assert holder.result() == {"released": True}
    log_records(state)
