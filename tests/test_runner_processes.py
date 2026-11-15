"""The runner under real concurrent processes (interface.md, Pass; design.md B1, A1, A7).

Every process a test starts is recorded at launch (PID, and its own session, so the group id is
the PID) and killed by that exact group on teardown; shims are killed by the identity the pass
recorded, after a re-check. Each process runs with the private HOME and XDG directories.
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

from jobsctl.claims import requeue_orphaned
from jobsctl.leases import parse_identity
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import check_identity
from jobsctl.store_db import Store
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.manual_helpers import config_file
from tests.runner_helpers import ShimKit, add_queued, attempts_of, job_ids, job_row
from tests.store_helpers import popen

WORKER = Path(__file__).with_name("runner_worker.py")
TIMEOUT = 120


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
        while "holding" in out:
            out = self.read_line()
        return out

    def finish(self) -> None:
        assert self.proc.wait(timeout=TIMEOUT) == 0, self._stderr()


class Pool:
    def __init__(self, tmp: Path, env: dict[str, str], config: str, kit: ShimKit) -> None:
        self.tmp, self.env, self.config, self.kit = tmp, env, config, kit
        self.workers: list[Worker] = []
        self.go_file = tmp / "go"

    def spawn(self, command: str, *, hold: float = 0, stay: bool = False) -> Worker:
        env = dict(self.env)
        if stay:
            env["RUNNER_WORKER_STAY"] = "1"
        n = len(self.workers)
        proc = popen(
            [sys.executable, "-I", str(WORKER), command, str(resolve_state_dir()),
             str(self.go_file), f"w{n:02d}", self.config, str(self.kit.wrapper), str(hold)],
            env=env,
            cwd=str(self.tmp),
            start_new_session=True,
        )  # fmt: skip
        worker = Worker(proc, pgid=proc.pid)
        self.workers.append(worker)
        return worker

    def spawn_many(self, count: int, command: str, **kwargs: Any) -> list[Worker]:
        workers = [self.spawn(command, **kwargs) for _ in range(count)]
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
def pool(tmp_path: Path, private_home: Path, kit: ShimKit) -> Iterator[Pool]:
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
    p = Pool(work, env, config_file(work, queues={"q": {"cap": 3}, "r": {"cap": 3}}), kit)
    try:
        yield p
    finally:
        p.close()


def seed(count: int, queue: str = "q") -> list[str]:
    ids = job_ids(count)
    with Store.open() as store:
        for i, job_id in enumerate(ids):
            add_queued(store, job_id, queue=queue, position=f"{i + 1:020d}")
    return ids


def statuses() -> dict[str, int]:
    with Store.open() as store:
        rows = store.query_all("SELECT status, count(*) AS n FROM jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


# -- the lease -----------------------------------------------------------------------------------


def test_racing_passes_have_one_lease_holder_and_the_rest_see_it(pool: Pool) -> None:
    seed(2)
    workers = pool.spawn_many(8, "pass", hold=1.5)
    pool.go()
    results = [w.result() for w in workers]
    for w in workers:
        w.finish()
    winners = [r for r in results if not r["already_running"]]
    losers = [r for r in results if r["already_running"]]
    assert len(winners) == 1 and len(losers) == 7
    for lost in losers:
        validate_against("cli/run.json", {k: v for k, v in lost.items() if k != "pid"})
        assert lost["holder"]["pid"] == winners[0]["pid"] and lost["started"] == []
    assert len(winners[0]["started"]) == 2
    with Store.open() as store:
        assert store.query_one("SELECT * FROM leases") is None


# -- caps ----------------------------------------------------------------------------------------


def test_caps_hold_under_twenty_concurrent_passes(pool: Pool) -> None:
    seed(6, "q")
    seed_other = job_ids(12)[6:]
    with Store.open() as store:
        for i, job_id in enumerate(seed_other):
            add_queued(store, job_id, queue="r", position=f"{i + 1:020d}")
    workers = pool.spawn_many(20, "pass")
    pool.go()
    results = [w.result() for w in workers]
    for w in workers:
        w.finish()
    started = [j for r in results for j in r["started"]]
    assert len(started) == len(set(started)) == 6  # cap 3 in each of two queues
    counts = statuses()
    assert counts == {"starting": 6, "queued": 6}
    with Store.open() as store:
        per_queue = store.query_all(
            "SELECT queue, count(*) AS n FROM jobs WHERE status = 'starting' GROUP BY queue"
        )
    assert {r["queue"]: r["n"] for r in per_queue} == {"q": 3, "r": 3}


def test_claims_without_the_lease_still_never_exceed_the_cap(pool: Pool) -> None:
    seed(10)
    workers = pool.spawn_many(20, "claim")
    pool.go()
    claimed = [j for w in workers for j in w.result()["claimed"]]
    for w in workers:
        w.finish()
    assert len(claimed) == len(set(claimed)) == 3
    assert statuses() == {"starting": 3, "queued": 7}
    with Store.open() as store:
        rows = store.query_all("SELECT job_id, n, claim_id FROM attempts")
    assert len(rows) == 3 and len({r["claim_id"] for r in rows}) == 3


# -- detached shims --------------------------------------------------------------------------------


def test_a_shim_survives_its_passes_exit_and_a_hangup_and_reads_dev_null(
    pool: Pool, kit: ShimKit
) -> None:
    (job_id,) = seed(1)
    (worker,) = pool.spawn_many(1, "pass", stay=True)
    pool.go()
    assert worker.result()["started"] == [job_id]
    record = kit.record(job_id)
    assert record["fds"]["0"] == "/dev/null"
    assert record["sid"] == record["pid"] != worker.pgid  # its own session and group
    os.killpg(worker.pgid, signal.SIGHUP)  # the pass's group (recorded at launch) hangs up
    worker.proc.wait(timeout=TIMEOUT)
    with Store.open() as store:
        ident = parse_identity(attempts_of(store, job_id)[0]["shim_identity"])
    assert ident is not None and ident.pid == record["pid"]
    time.sleep(0.3)
    assert check_identity(default_host(), ident).alive  # still running, and no longer a child


def test_a_crash_between_claim_and_spawn_leaves_a_claim_that_can_be_recovered(
    pool: Pool,
) -> None:
    (job_id,) = seed(1)
    worker = pool.spawn("claim-hang")
    said = worker.read_line()
    assert said["claimed"] == [job_id]
    pool.kill(worker)  # SIGKILL: no spawn, no identity recorded
    with Store.open() as store:
        job, (attempt,) = job_row(store, job_id), attempts_of(store, job_id)
        assert job["status"] == "starting" and attempt["claim_id"] == said["claim_id"]
        assert attempt["shim_identity"] is None and attempt["shim_confirmed_at"] is None
        claimer = parse_identity(attempt["claimed_by"])
        assert claimer is not None and not check_identity(default_host(), claimer).alive
        with store.write() as tx:  # what reconcile's claim_orphaned does with it
            requeued, _ = requeue_orphaned(
                tx, job_id, said["claim_id"], now="2026-01-01T00:00:00.000Z"
            )
        assert requeued and job_row(store, job_id)["status"] == "queued"


# -- the command, in real processes ----------------------------------------------------------------


def cli(pool: Pool, *args: str) -> subprocess.Popen[str]:
    return popen(
        [sys.executable, "-I", "-m", "jobsctl", "--config", pool.config, *args],
        env=pool.env,
        cwd=str(pool.tmp),
        start_new_session=True,
    )


def test_run_loop_stops_on_sigterm_after_finishing_its_pass(pool: Pool) -> None:
    (job_id,) = seed(1)
    proc = cli(pool, "--json", "run", "--loop", "--interval", "1s")
    pgid = proc.pid  # recorded at launch; the leader is reaped only below
    try:
        assert proc.stdout is not None
        first = json.loads(proc.stdout.readline())
        validate_against("cli/run.json", first)
        assert first["started"] == [job_id]
        proc.send_signal(signal.SIGTERM)  # the exact process we started
        assert proc.wait(timeout=60) == 0
        rest = [json.loads(line) for line in proc.stdout.read().splitlines()]
        for doc in rest:
            validate_against("cli/run.json", doc)
    finally:
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=60)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.real_trigger
def test_add_starts_a_detached_pass_and_returns_at_once(pool: Pool) -> None:
    proc = cli(pool, "--json", "add", "--cwd", "/", "--", "true")
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    job_id = json.loads(out)["job"]["id"]
    deadline = time.monotonic() + 60
    status = "queued"
    while status == "queued" and time.monotonic() < deadline:
        time.sleep(0.1)
        with Store.open() as store:
            status = job_row(store, job_id)["status"]
    assert status == "starting"  # claimed by the triggered pass (the stub shim ends at once)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with Store.open() as store:
            if store.query_one("SELECT 1 FROM leases WHERE name = 'runner'") is None:
                break
        time.sleep(0.1)
    with Store.open() as store:
        assert store.query_one("SELECT 1 FROM leases WHERE name = 'runner'") is None
    assert list((resolve_state_dir() / "diag").iterdir()) == []  # nothing was written
