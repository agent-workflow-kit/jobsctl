"""Real-process fixture for the shim: every process a test starts is recorded the moment it is
spawned and stopped by that exact record on teardown. Never by name or pattern.

* a shim is a `Popen` this test spawned (killed through the unreaped handle, or its recorded
  identity when the handle is gone);
* a job is stopped by the identity and process group the shim recorded on its attempt (the group
  is signaled only while the leader's identity holds, `proc_signal.signal_group`);
* any other PID a job prints for the test (a leftover) is recorded with its identity when it is
  announced and signaled only if that identity still holds.
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jobsctl.config import Config
from jobsctl.config_files import load_config
from jobsctl.leases import identity_text, own_identity, parse_identity
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Identity, check_identity, identify
from jobsctl.proc_signal import signal_group, signal_process
from jobsctl.spawn import spawn_internal
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from tests.kill_guard import allow
from tests.shim_helpers import CLAIM, JOB, seed_claimed

WAIT = 60


@dataclass
class RealShims:
    """`config` and `config_path` default to a file with `auto_run: false`: a real shim requests a
    pass after its final commit, and a real detached pass would outlive the test. A test that
    needs `auto_run: true` calls :meth:`configure` with it explicitly."""

    config: Config = field(default_factory=Config)
    config_path: Path | None = None
    handles: list[subprocess.Popen[bytes]] = field(default_factory=list)
    extras: list[Identity] = field(default_factory=list)

    def configure(self, directory: Path, **raw: Any) -> None:
        """Write `directory/config.yaml` with `raw` (over `auto_run: false`) and use it."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "config.yaml"
        path.write_text(json.dumps({"auto_run": False, **raw}))
        self.config_path = path
        self.config = load_config(path)

    def seed_and_spawn(
        self, argv: list[str], *, job_id: str = JOB, record_identity: bool = True, **seed: Any
    ) -> subprocess.Popen[bytes]:
        """Claim a job as a pass would (claimed_by: this live process), spawn the real `_shim`,
        and record its identity on the attempt (what the pass does right after the spawn)."""
        with Store.open(mode=OpenMode.CLI) as store:
            seed_claimed(
                store,
                argv,
                job_id=job_id,
                shim_identity="",
                claimed_by=own_identity(default_host()),
                **seed,
            )
        popen = spawn_internal(
            resolve_state_dir(), self.config, self.config_path, "_shim", [job_id, CLAIM]
        )
        self.handles.append(popen)
        if record_identity:
            self.record_identity(popen, job_id)
        return popen

    def record_identity(self, popen: subprocess.Popen[bytes], job_id: str = JOB) -> None:
        text = identity_text(default_host(), popen.pid)
        with Store.open(mode=OpenMode.CLI) as store, store.write() as tx:
            tx.execute(
                "UPDATE attempts SET shim_identity = ? WHERE job_id = ? AND claim_id = ?",
                [text, job_id, CLAIM],
            )

    def row(self, sql: str, *params: Any) -> Any:
        with Store.open(mode=OpenMode.CLI) as store:
            return store.query_one(sql, list(params))

    def wait_for(self, sql: str, *params: Any, timeout: float = WAIT) -> Any:
        """Poll until `sql` returns a row (truthy first column)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.row(sql, *params)
            if found is not None and found[0]:
                return found
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {sql}")

    def wait_status(self, status: str, job_id: str = JOB, timeout: float = WAIT) -> Any:
        return self.wait_for(
            "SELECT 1 FROM jobs WHERE id = ? AND status = ?", job_id, status, timeout=timeout
        )

    def job_identity(self, job_id: str = JOB) -> Identity:
        found = self.wait_for("SELECT job_identity FROM attempts WHERE job_id = ?", job_id)
        ident = parse_identity(found[0])
        assert ident is not None
        return ident

    def track(self, pid: int) -> Identity:
        """Record a process a job announced, with its identity now."""
        ident = identify(default_host(), pid)
        self.extras.append(ident)
        allow(pid)
        return ident

    def stop_all(self) -> None:
        host = default_host()
        try:
            with Store.open(mode=OpenMode.CLI) as store:
                rows = store.query_all("SELECT job_identity, pgid, shim_identity FROM attempts")
        except Exception:
            rows = []
        for row in rows:
            for key in ("job_identity", "shim_identity"):
                ident = parse_identity(row[key]) if row[key] else None
                if ident is None:
                    continue
                if not check_identity(host, ident, allow_zombie=True).alive:
                    continue  # gone or reused: the recorded pid is not ours to signal
                allow(ident.pid)  # the recorded identity matches the live process
                if key == "job_identity":
                    signal_group(host, ident, signal.SIGKILL, allow_zombie=True)
                signal_process(host, ident, signal.SIGKILL, allow_zombie=True)
        for ident in self.extras:
            signal_process(host, ident, signal.SIGKILL)
        for popen in self.handles:
            if popen.poll() is None:
                popen.kill()  # the exact child we spawned
            popen.wait(timeout=30)


@pytest.fixture
def real_shims(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RealShims]:
    kit = RealShims()
    kit.configure(tmp_path_factory.mktemp("shim-config"))
    try:
        yield kit
    finally:
        kit.stop_all()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


@dataclass
class JobGuard:
    """Teardown for jobs an in-process shim started: every job's recorded group (while the
    leader's identity holds) and every PID a test announced (with the identity it had when it was
    announced) is killed; nothing else, never by name."""

    extras: list[Identity] = field(default_factory=list)

    def track(self, pid: int) -> Identity:
        ident = identify(default_host(), pid)
        self.extras.append(ident)
        allow(pid)
        return ident

    def stop_all(self) -> None:
        host = default_host()
        try:
            with Store.open(mode=OpenMode.CLI) as store:
                rows = store.query_all("SELECT job_identity FROM attempts")
        except Exception:
            rows = []
        for row in rows:
            ident = parse_identity(row["job_identity"]) if row["job_identity"] else None
            if ident is not None:
                if not check_identity(host, ident, allow_zombie=True).alive:
                    continue  # gone or reused: the recorded pid is not ours to signal
                allow(ident.pid)  # the recorded identity matches the live process
                signal_group(host, ident, signal.SIGKILL, allow_zombie=True)
                signal_process(host, ident, signal.SIGKILL, allow_zombie=True)
        for ident in self.extras:
            signal_process(host, ident, signal.SIGKILL)


@pytest.fixture
def job_guard() -> Iterator[JobGuard]:
    guard = JobGuard()
    try:
        yield guard
    finally:
        guard.stop_all()
