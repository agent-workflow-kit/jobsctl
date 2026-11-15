"""Version skew between the shim and the database (interface.md, Upgrades; plan Stage 5).

"Old" and "new" jobsctl are the same code with a different set of migrations: the new one knows a
second, additive migration (a column with a default and a table). The shim runs as a real process
started through a tiny launcher that fixes the migrations the process knows, so the previous
version's behavior is a frozen fixture rather than a second checkout.
"""

from __future__ import annotations

import sqlite3
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from jobsctl.runner import Child, PassResult, Runner
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from tests.shim_helpers import CLAIM, JOB
from tests.shim_real import RealShims
from tests.upgrade_schemas import V2

ROOT = str(Path(__file__).resolve().parent.parent)
LAUNCHER = textwrap.dedent(
    """\
    #!{python}
    import sys
    sys.path.insert(0, {root!r})
    from jobsctl import store_db
    from tests.upgrade_schemas import SCHEMAS
    store_db.MIGRATIONS = SCHEMAS[{version!r}]
    from jobsctl.cli import main
    sys.exit(main(sys.argv[sys.argv.index("_shim"):]))
    """
)


def launcher(tmp_path: Path, version: str) -> str:
    """An executable standing in for the interpreter a pass spawns: it runs `_shim` with the
    migrations of `version` ("old": the previous release, "new": one additive migration more)."""
    import sys

    path = tmp_path / f"jobsctl-{version}"
    path.write_text(LAUNCHER.format(python=sys.executable, version=version, root=ROOT), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def user_version() -> int:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_an_old_shim_finishes_its_job_against_a_migrated_database(
    real_shims: RealShims, tmp_path: Path
) -> None:
    with Store.open(mode=OpenMode.CLI, migrations=V2):  # a newer jobsctl migrated meanwhile
        pass
    popen = real_shims.seed_and_spawn(
        ["sh", "-c", "echo from-the-job; exit 0"], python=launcher(tmp_path, "old")
    )
    real_shims.wait_status("done")
    assert popen.wait(timeout=60) == 0
    att = real_shims.row("SELECT * FROM attempts WHERE job_id = ?", JOB)
    assert att["shim_confirmed_at"] and att["note2"] is None
    job = real_shims.row("SELECT * FROM jobs WHERE id = ?", JOB)
    assert job["score"] == 7  # the column the old code never heard of keeps its default
    assert user_version() == 2  # the old shim neither downgraded nor touched the schema
    tail = real_shims.row("SELECT tail FROM outputs WHERE job_id = ?", JOB)
    assert bytes(tail["tail"]) == b"from-the-job\n"
    assert real_shims.row("SELECT 1 FROM leases WHERE name = ?", f"job:{JOB}") is None


def test_a_new_shim_on_an_older_database_exits_before_confirming_and_writes_nothing(
    real_shims: RealShims, tmp_path: Path
) -> None:
    popen = real_shims.seed_and_spawn(["sh", "-c", "exit 0"], python=launcher(tmp_path, "new"))
    assert popen.wait(timeout=60) == 1
    att = real_shims.row("SELECT * FROM attempts WHERE job_id = ?", JOB)
    assert att["shim_confirmed_at"] is None and att["status"] == "starting"
    assert real_shims.row("SELECT status, spawn_failures FROM jobs WHERE id = ?", JOB)[:] == (
        "starting",
        0,
    )
    assert real_shims.row("SELECT COUNT(*) FROM leases WHERE name = ?", f"job:{JOB}")[0] == 0
    assert user_version() == 1  # the shim does not migrate
    log = resolve_state_dir() / "shims" / f"{JOB}.log"
    assert "older than this jobsctl" in log.read_text(encoding="utf-8")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600 and not log.is_symlink()


def test_the_next_pass_requeues_it_without_counting_a_spawn_failure(
    real_shims: RealShims, tmp_path: Path
) -> None:
    popen = real_shims.seed_and_spawn(["sh", "-c", "exit 0"], python=launcher(tmp_path, "new"))
    popen.wait(timeout=60)
    with Store.open(mode=OpenMode.CLI) as store:
        runner = Runner(store, real_shims.config, real_shims.config_path)
        runner.children.append(Child(popen, "shim", JOB, CLAIM))
        result = PassResult()
        runner.reap(result)
        assert result.reconciled == [
            {"job_id": JOB, "finding": "claim_orphaned", "action": "requeued"}
        ]
        row: Any = store.query_one("SELECT status, spawn_failures FROM jobs WHERE id = ?", [JOB])
        assert (row["status"], row["spawn_failures"]) == ("queued", 0)
        assert store.query_all("SELECT 1 FROM attempts WHERE job_id = ?", [JOB]) == []
    assert isinstance(popen, subprocess.Popen)


# -- the other internal processes, in-process ------------------------------------------------------


@pytest.fixture
def internal_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobsctl.commands import internal

    monkeypatch.setattr(internal, "install", lambda path: None)


@pytest.mark.parametrize("argv", [["_deliver"], ["_kill", JOB], ["_shim", JOB, CLAIM]])
def test_an_internal_process_on_an_older_schema_exits_one_before_writing(
    argv: list[str], internal_diag: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from jobsctl import store_db
    from tests.helpers import run_cli

    with Store.open(mode=OpenMode.CLI):  # a v1 database
        pass
    monkeypatch.setattr(store_db, "MIGRATIONS", V2)
    db = resolve_state_dir() / "state.db"
    before = db.read_bytes()
    res = run_cli(capsys, *argv)
    assert res.code == 1 and "older" in res.err
    assert user_version() == 1 and db.read_bytes() == before  # not even a pragma wrote


@pytest.mark.parametrize("argv", [["_deliver"], ["_kill", JOB]])
def test_an_internal_process_of_the_previous_version_works_on_a_migrated_schema(
    argv: list[str], internal_diag: None, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from tests.helpers import run_cli

    with Store.open(mode=OpenMode.CLI, migrations=V2):
        pass
    res = run_cli(capsys, *argv)
    assert res.code in (0, 10), res.err  # nothing to deliver, or no such job: never a version error
    assert user_version() == 2
