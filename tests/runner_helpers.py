"""Helpers for the runner tests: a fake shim (an executable that stands in for the interpreter a
pass spawns), job rows, and exact-handle teardown of every process a test starts.

The fake shim records how it was started (pid, session, cwd, argv, environment, open descriptors)
in ``<records>/<job id>.json``, then waits for the ``stop`` file. Every process a pass spawned is
a child of the pass: in-process tests keep the ``Popen`` handles (``runner.children``) and kill
exactly those on teardown; shims spawned by a worker process are killed by their identity recorded
in the database (``proc_signal.signal_process`` re-checks it first).
"""

from __future__ import annotations

import json
import signal
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobsctl.config import Config
from jobsctl.leases import parse_identity
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import check_identity
from jobsctl.proc_signal import signal_process
from jobsctl.runner import Runner
from jobsctl.store_db import OpenMode, Store
from tests.kill_guard import allow
from tests.store_helpers import NOW, attempt_values, insert_row, job_values

FAKE_SHIM = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, time
    records, stop = {records!r}, {stop!r}
    listing = {{}}  # empty where there is no /proc/self/fd (macOS): tests that read it skip
    for name in os.listdir("/proc/self/fd") if os.path.isdir("/proc/self/fd") else []:
        try:
            listing[name] = os.readlink("/proc/self/fd/" + name)
        except OSError:
            pass
    job_id = sys.argv[-2] if "_shim" in sys.argv else sys.argv[-1]
    record = {{
        "pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp(), "sid": os.getsid(0),
        "cwd": os.getcwd(), "argv": sys.argv[1:], "env": dict(os.environ), "fds": listing,
    }}
    tmp = os.path.join(records, job_id + ".tmp")
    with open(tmp, "w") as out:
        json.dump(record, out)
    os.rename(tmp, os.path.join(records, job_id + ".json"))
    deadline = time.monotonic() + 120
    while not os.path.exists(stop) and time.monotonic() < deadline:
        time.sleep(0.05)
    """
)


@dataclass
class ShimKit:
    wrapper: Path
    records: Path
    stop: Path

    def record(self, job_id: str, timeout: float = 20) -> dict[str, Any]:
        path = self.records / f"{job_id}.json"
        deadline = time.monotonic() + timeout
        while not path.exists():
            assert time.monotonic() < deadline, f"no fake shim record for {job_id}"
            time.sleep(0.02)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        return parsed


def make_kit(tmp: Path) -> ShimKit:
    records = tmp / "shim-records"
    records.mkdir()
    stop = tmp / "shim-stop"
    wrapper = tmp / "fake-python"
    wrapper.write_text(
        FAKE_SHIM.format(python=sys.executable, records=str(records), stop=str(stop)),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return ShimKit(wrapper, records, stop)


def kill_recorded_shims() -> None:
    """SIGKILL the shims recorded in the state database, each only while its identity holds."""
    state_db = _state_db()
    if state_db is None:
        return
    host = default_host()
    with Store.open(mode=OpenMode.CLI) as store:
        for row in store.query_all(
            "SELECT shim_identity FROM attempts WHERE shim_identity IS NOT NULL"
        ):
            ident = parse_identity(row["shim_identity"])
            if ident is not None and check_identity(host, ident).alive:
                allow(ident.pid)  # the recorded identity matches the live process
                signal_process(host, ident, signal.SIGKILL)


def _state_db() -> Path | None:
    from jobsctl.store_dir import resolve_state_dir

    path = resolve_state_dir() / "state.db"
    return path if path.exists() else None


def make_runner(
    runners: list[Runner],
    store: Store,
    config: Config | None = None,
    *,
    kit: ShimKit | None = None,
    **kwargs: Any,
) -> Runner:
    runner = Runner(
        store,
        config or Config(),
        kwargs.pop("config_path", None),
        python=str(kit.wrapper) if kit is not None else kwargs.pop("python", None),
        **kwargs,
    )
    runners.append(runner)
    return runner


def add_queued(
    store: Store, job_id: str, *, queue: str = "q", position: str = "m", **over: Any
) -> None:
    with store.write() as tx:
        insert_row(tx, "jobs", job_values(job_id, queue=queue, position=position, **over))


def put_starting(store: Store, job_id: str, *, claim_id: str = "claim-1", **over: Any) -> None:
    """A job already claimed (`starting`, attempt 1), as an earlier pass left it."""
    with store.write() as tx:
        tx.execute("UPDATE jobs SET status = 'starting', attempt = 1 WHERE id = ?", [job_id])
        insert_row(tx, "attempts", attempt_values(job_id, 1, claim_id, **over))


def job_row(store: Store, job_id: str) -> Any:
    return store.query_one("SELECT * FROM jobs WHERE id = ?", [job_id])


def attempts_of(store: Store, job_id: str) -> list[Any]:
    return store.query_all("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", [job_id])


def job_ids(n: int) -> list[str]:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return [f"jb-{alphabet[i // 32]}{alphabet[i % 32]}aaaaaa" for i in range(n)]


__all__ = ["NOW"]
