"""State store: directory safety, connection setup, transactions, migrations, version checks."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from jobsctl.errors import JobsctlError
from jobsctl.store_db import OpenMode, SchemaOlderError, Store
from jobsctl.store_dir import prepare_state_dir, resolve_state_dir
from jobsctl.store_schema import MIGRATIONS, SCHEMA_VERSION, check_additive
from tests.store_helpers import insert_row, job_values


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "jobsctl"


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# -- location and permissions ----------------------------


def test_state_dir_follows_xdg_state_home(private_home: Path) -> None:
    assert resolve_state_dir() == private_home / ".local/state/jobsctl"
    assert resolve_state_dir({"XDG_STATE_HOME": "/x/y", "HOME": "/h"}) == Path("/x/y/jobsctl")


def test_state_dir_defaults_and_ignores_relative_xdg() -> None:
    env = {"HOME": "/h"}
    assert resolve_state_dir(env) == Path("/h/.local/state/jobsctl")
    assert resolve_state_dir({**env, "XDG_STATE_HOME": "rel"}) == Path("/h/.local/state/jobsctl")
    assert resolve_state_dir({**env, "XDG_STATE_HOME": ""}) == Path("/h/.local/state/jobsctl")


@pytest.mark.parametrize("umask", [0o000, 0o077, 0o022])
def test_modes_do_not_depend_on_the_umask(state: Path, umask: int) -> None:
    old = os.umask(umask)
    try:
        with Store.open(state) as store:
            with store.write() as tx:
                insert_row(tx, "jobs", job_values())
    finally:
        os.umask(old)
    assert mode(state) == 0o700
    assert mode(state / "shims") == 0o700
    assert mode(state / "diag") == 0o700
    assert mode(state / "state.db") == 0o600


def test_wal_and_shm_files_are_private_while_open(state: Path) -> None:
    old = os.umask(0)
    try:
        with Store.open(state) as store:
            with store.write() as tx:
                insert_row(tx, "jobs", job_values())
            for suffix in ("-wal", "-shm"):
                assert (state / f"state.db{suffix}").exists()
                assert mode(state / f"state.db{suffix}") == 0o600
    finally:
        os.umask(old)


def test_a_group_readable_state_dir_is_tightened(state: Path) -> None:
    state.mkdir(parents=True)
    os.chmod(state, 0o755)
    Store.open(state).close()
    assert mode(state) == 0o700


def test_a_group_readable_database_is_tightened(state: Path) -> None:
    Store.open(state).close()
    os.chmod(state / "state.db", 0o664)
    Store.open(state).close()
    assert mode(state / "state.db") == 0o600


def test_a_symlinked_state_dir_is_refused(tmp_path: Path, state: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    state.parent.mkdir(parents=True)
    state.symlink_to(target)
    with pytest.raises(JobsctlError) as err:
        Store.open(state)
    assert err.value.code == "internal"
    assert list(target.iterdir()) == []


def test_a_symlinked_subdirectory_is_refused(tmp_path: Path, state: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    state.mkdir(parents=True)
    (state / "shims").symlink_to(target)
    with pytest.raises(JobsctlError, match="unsafe"):
        Store.open(state)


def test_symlinked_ancestors_are_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    Store.open(tmp_path / "link" / "jobsctl").close()
    assert (real / "jobsctl" / "state.db").is_file()


def test_a_symlinked_database_is_refused_and_its_target_untouched(
    tmp_path: Path, state: Path
) -> None:
    state.mkdir(parents=True)
    target = tmp_path / "victim"
    target.write_bytes(b"precious")
    (state / "state.db").symlink_to(target)
    with pytest.raises(JobsctlError, match="unsafe"):
        Store.open(state)
    assert target.read_bytes() == b"precious"


def test_a_database_that_is_a_directory_or_fifo_is_refused(tmp_path: Path, state: Path) -> None:
    state.mkdir(parents=True)
    (state / "state.db").mkdir()
    with pytest.raises(JobsctlError):
        Store.open(state)
    (state / "state.db").rmdir()
    os.mkfifo(state / "state.db")
    with pytest.raises(JobsctlError, match="not a regular file"):
        Store.open(state, busy_timeout=0.2)


def test_the_state_dir_being_a_file_is_refused(state: Path) -> None:
    state.parent.mkdir(parents=True)
    state.write_text("x")
    with pytest.raises(JobsctlError):
        Store.open(state)


def test_read_only_state_dir_is_an_internal_error_not_busy(state: Path) -> None:
    Store.open(state).close()
    os.chmod(state, 0o500)
    try:
        with pytest.raises(JobsctlError) as err:
            Store.open(state)
        assert err.value.code == "internal"
    finally:
        os.chmod(state, 0o700)


def test_unwritable_parent_is_an_internal_error(tmp_path: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir()
    os.chmod(parent, 0o500)
    try:
        with pytest.raises(JobsctlError) as err:
            Store.open(parent / "a" / "jobsctl")
        assert err.value.code == "internal"
    finally:
        os.chmod(parent, 0o700)


def test_internal_open_never_creates_anything(state: Path) -> None:
    with pytest.raises(JobsctlError):
        Store.open(state, mode=OpenMode.INTERNAL)
    assert not state.exists()
    state.mkdir(parents=True)
    with pytest.raises(JobsctlError):
        prepare_state_dir(state, create=False)  # shims/ and diag/ are missing
    assert not (state / "state.db").exists()


# -- connection setup (spike B1) -----------------------------------------------------------------


def test_connection_pragmas(state: Path) -> None:
    with Store.open(state, busy_timeout=2.5) as store:

        def pragma(name: str) -> object:
            return store.conn.execute(f"PRAGMA {name}").fetchone()[0]

        assert str(pragma("journal_mode")).lower() == "wal"
        assert pragma("synchronous") == 1  # NORMAL
        assert pragma("busy_timeout") == 2500
        assert pragma("journal_size_limit") == 64 * 1024 * 1024
        assert pragma("foreign_keys") == 1
        assert store.conn.autocommit is True
        assert not store.conn.in_transaction
        assert pragma("user_version") == SCHEMA_VERSION


def test_wal_is_persistent_and_set_outside_a_transaction(state: Path) -> None:
    Store.open(state).close()
    raw = sqlite3.connect(state / "state.db")
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        raw.close()


def test_plain_begin_that_upgrades_fails_but_write_does_not(state: Path) -> None:
    """Spike B1: a deferred transaction that reads and then writes fails immediately."""
    with Store.open(state, busy_timeout=2.0) as a, Store.open(state, busy_timeout=2.0) as b:
        b.conn.execute("BEGIN")  # the pitfall: a plain BEGIN
        b.conn.execute("SELECT count(*) FROM jobs").fetchone()
        with a.write() as tx:
            insert_row(tx, "jobs", job_values())
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            b.conn.execute("DELETE FROM jobs")  # upgrade after a concurrent commit
        b.conn.execute("ROLLBACK")
        with b.write() as tx:  # the helper reads the latest state under the write lock
            assert tx.one("SELECT count(*) AS c FROM jobs")["c"] == 1  # type: ignore[index]
            tx.execute("DELETE FROM jobs")


def test_write_takes_the_write_lock_at_begin(state: Path) -> None:
    with Store.open(state, busy_timeout=0.2) as a, Store.open(state, busy_timeout=0.2) as b:
        with a.write():
            with pytest.raises(JobsctlError) as err, b.write():
                pass
        assert err.value.code == "busy"
        assert err.value.exit_code == 1
        with b.write():  # released after the first transaction ended
            pass


def test_readers_do_not_block_on_a_writer(state: Path) -> None:
    with Store.open(state, busy_timeout=0.2) as a, Store.open(state, busy_timeout=0.2) as b:
        with a.write() as tx:
            insert_row(tx, "jobs", job_values())
            assert b.query_one("SELECT count(*) AS c FROM jobs")["c"] == 0  # type: ignore[index]


# -- transactions ----------------------------


def test_commit_and_rollback(state: Path) -> None:
    with Store.open(state) as store:
        with store.write() as tx:
            insert_row(tx, "jobs", job_values("jb-aaaaaaaa"))
        with pytest.raises(RuntimeError, match="boom"), store.write() as tx:
            insert_row(tx, "jobs", job_values("jb-bbbbbbbb"))
            raise RuntimeError("boom")
        ids = [r["id"] for r in store.query_all("SELECT id FROM jobs")]
        assert ids == ["jb-aaaaaaaa"]
        assert not store.conn.in_transaction


def test_after_commit_runs_only_after_a_commit(state: Path) -> None:
    calls: list[str] = []
    with Store.open(state) as store:
        with store.write() as tx:
            tx.after_commit(lambda: calls.append("committed"))
            assert calls == []
        assert calls == ["committed"]
        with pytest.raises(ValueError), store.write() as tx:
            tx.after_commit(lambda: calls.append("rolled back"))
            raise ValueError
        assert calls == ["committed"]


def test_after_commit_failure_is_reported_after_every_callback_ran(state: Path) -> None:
    calls: list[str] = []

    def bad() -> None:
        raise OSError("log failed")

    with Store.open(state) as store:
        with pytest.raises(OSError, match="log failed"), store.write() as tx:
            insert_row(tx, "jobs", job_values())
            tx.after_commit(bad)
            tx.after_commit(lambda: calls.append("second"))
        assert calls == ["second"]
        assert store.query_one("SELECT count(*) AS c FROM jobs")["c"] == 1  # type: ignore[index]


def test_nested_write_and_transaction_control_are_refused(state: Path) -> None:
    with Store.open(state) as store:
        with store.write() as tx:
            with pytest.raises(RuntimeError):
                with store.write():
                    pass
            for sql in ("COMMIT", "begin", " ROLLBACK", "SAVEPOINT x", "END"):
                with pytest.raises(ValueError):
                    tx.execute(sql)
        assert not store.conn.in_transaction


def test_a_transaction_cannot_be_used_after_it_ends(state: Path) -> None:
    with Store.open(state) as store:
        with store.write() as tx:
            pass
        with pytest.raises(RuntimeError, match="closed"):
            tx.execute("SELECT 1")


def test_read_transaction_is_a_snapshot_and_refuses_writes(state: Path) -> None:
    with Store.open(state) as a, Store.open(state) as b:
        with a.read() as rx:
            assert rx.one("SELECT count(*) AS c FROM jobs")["c"] == 0  # type: ignore[index]
            with b.write() as tx:
                insert_row(tx, "jobs", job_values())
            assert rx.one("SELECT count(*) AS c FROM jobs")["c"] == 0  # type: ignore[index]
            with pytest.raises(JobsctlError):
                rx.execute("DELETE FROM jobs")
            with pytest.raises(RuntimeError):
                rx.after_commit(lambda: None)
        assert a.query_one("SELECT count(*) AS c FROM jobs")["c"] == 1  # type: ignore[index]
        with a.write() as tx:  # query_only is switched back off
            tx.execute("DELETE FROM jobs")


def test_integrity_errors_reach_the_caller_untranslated(state: Path) -> None:
    with Store.open(state) as store:
        with pytest.raises(sqlite3.IntegrityError), store.write() as tx:
            insert_row(tx, "jobs", job_values(status="bogus"))


# -- schema ----------------------------


def test_every_table_and_the_two_indexes_exist(state: Path) -> None:
    with Store.open(state) as store:
        names = {r["name"] for r in store.query_all("SELECT name FROM sqlite_master")}
    for table in (
        "jobs", "job_labels", "attempts", "outputs", "hook_outbox", "leases", "counters",
        "queues_seen", "pruned_ids",
    ):  # fmt: skip
        assert table in names
    assert {"jobs_unique_key_live", "jobs_order"} <= names


def test_order_index_columns(state: Path) -> None:
    with Store.open(state) as store:
        cols = [r["name"] for r in store.query_all("PRAGMA index_info(jobs_order)")]
    assert cols == ["queue", "status", "pinned", "priority", "position"]


def test_shipped_migrations_are_additive() -> None:
    for step in MIGRATIONS:
        for statement in step:
            check_additive(statement)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE jobs",
        "ALTER TABLE jobs RENAME TO j",
        "ALTER TABLE jobs RENAME COLUMN note TO n",
        "ALTER TABLE jobs DROP COLUMN note",
        "ALTER TABLE jobs ADD COLUMN x INTEGER NOT NULL",
        "ALTER TABLE jobs ADD COLUMN x TEXT UNIQUE",
        "UPDATE jobs SET note = 'x'",
        "DELETE FROM jobs",
        "CREATE TABLE a (x); DROP TABLE jobs",
        "CREATE TABLE a (x); CREATE TABLE b (y)",
        "PRAGMA user_version = 99",
        "CREATE TRIGGER t AFTER INSERT ON jobs BEGIN SELECT 1; END",
        "",
    ],
)
def test_non_additive_statements_are_refused(sql: str) -> None:
    with pytest.raises(ValueError):
        check_additive(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t (a)",
        "create index i on jobs (note)",
        "CREATE UNIQUE INDEX i ON jobs (note) WHERE note IS NOT NULL;",
        "ALTER TABLE jobs ADD COLUMN x INTEGER",
        "ALTER TABLE jobs ADD COLUMN x INTEGER NOT NULL DEFAULT 7",
    ],
)
def test_additive_statements_are_allowed(sql: str) -> None:
    check_additive(sql)


V2_COLUMN = MIGRATIONS + (("ALTER TABLE jobs ADD COLUMN score INTEGER NOT NULL DEFAULT 7",),)


def test_a_migration_adds_a_column_with_a_default_and_old_rows_read_correctly(
    state: Path,
) -> None:
    with Store.open(state) as v1:
        assert v1.schema_version == 1
        with v1.write() as tx:
            insert_row(tx, "jobs", job_values("jb-aaaaaaaa"))
            insert_row(tx, "jobs", job_values("jb-bbbbbbbb", queue="other"))
    with Store.open(state, migrations=V2_COLUMN) as v2:
        assert v2.schema_version == 2
        assert v2.current_version() == 2
        rows = v2.query_all("SELECT id, score FROM jobs ORDER BY id")
        assert [(r["id"], r["score"]) for r in rows] == [("jb-aaaaaaaa", 7), ("jb-bbbbbbbb", 7)]
        assert "score" in v2.columns("jobs")
    # the previous version keeps working against the migrated database
    with Store.open(state, mode=OpenMode.INTERNAL) as old:
        assert old.current_version() == 2
        assert old.query_one("SELECT count(*) AS c FROM jobs")["c"] == 2  # type: ignore[index]
        with old.write() as tx:
            insert_row(tx, "jobs", job_values("jb-cccccccc"))
        assert old.query_one("SELECT score FROM jobs WHERE id = 'jb-cccccccc'")["score"] == 7  # type: ignore[index]


def test_migration_is_idempotent_across_reopens(state: Path) -> None:
    for _ in range(3):
        with Store.open(state, migrations=V2_COLUMN) as store:
            assert store.current_version() == 2


def test_a_failing_migration_rolls_back_completely(state: Path) -> None:
    Store.open(state).close()
    bad = MIGRATIONS + (
        (
            "CREATE TABLE half_done (a INTEGER)",
            "ALTER TABLE no_such_table ADD COLUMN x INTEGER",
        ),
    )
    with pytest.raises(JobsctlError) as err:
        Store.open(state, migrations=bad)
    assert err.value.code == "internal"
    with Store.open(state) as store:
        assert store.current_version() == 1
        names = {r["name"] for r in store.query_all("SELECT name FROM sqlite_master")}
        assert "half_done" not in names


def test_a_non_additive_migration_is_refused_before_touching_the_database(state: Path) -> None:
    Store.open(state).close()
    with pytest.raises(ValueError, match="additive"):
        Store.open(state, migrations=MIGRATIONS + (("DROP TABLE counters",),))
    with Store.open(state) as store:
        assert store.current_version() == 1
        assert store.query_one("SELECT count(*) AS c FROM counters") is not None


def test_an_unversioned_foreign_database_is_refused(state: Path) -> None:
    state.mkdir(parents=True)
    foreign = sqlite3.connect(state / "state.db")
    foreign.execute("CREATE TABLE precious (x)")
    foreign.commit()
    foreign.close()
    with pytest.raises(JobsctlError, match="not a jobsctl database"):
        Store.open(state)
    check = sqlite3.connect(state / "state.db")
    try:
        assert [r[0] for r in check.execute("SELECT name FROM sqlite_master")] == ["precious"]
    finally:
        check.close()


@pytest.mark.parametrize(
    "junk", [b"this is not a database" * 100, b"SQLite format 3\x00" + b"\xff" * 4000]
)
def test_a_corrupt_database_is_an_internal_error_and_left_as_it_was(
    state: Path, junk: bytes
) -> None:
    state.mkdir(parents=True)
    (state / "state.db").write_bytes(junk)
    with pytest.raises(JobsctlError) as err:
        Store.open(state)
    assert err.value.code == "internal"
    assert err.value.exit_code == 1
    assert (state / "state.db").read_bytes() == junk


def test_a_truncated_database_is_an_internal_error(state: Path) -> None:
    with Store.open(state) as store:
        with store.write() as tx:
            for i in range(50):
                insert_row(tx, "jobs", job_values(f"jb-{i:08d}"[:11].replace("0", "a")))
    db = state / "state.db"
    for suffix in ("-wal", "-shm"):
        (state / f"state.db{suffix}").unlink(missing_ok=True)
    data = db.read_bytes()
    db.write_bytes(data[: len(data) // 2 + 7])
    with pytest.raises(JobsctlError) as err:
        with Store.open(state) as store:
            store.query_all("SELECT * FROM jobs")
    assert err.value.code == "internal"


# -- version checks ----------------------------


def test_internal_open_on_an_older_schema_writes_nothing(state: Path) -> None:
    with Store.open(state, migrations=MIGRATIONS[:0] + MIGRATIONS) as store:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = (state / "state.db").read_bytes()
    with pytest.raises(SchemaOlderError) as err:
        Store.open(state, mode=OpenMode.INTERNAL, migrations=V2_COLUMN)
    assert err.value.code == "internal"
    assert (err.value.found, err.value.known) == (1, 2)
    assert (state / "state.db").read_bytes() == before
    assert not (state / "state.db-wal").exists() or (state / "state.db-wal").stat().st_size == 0
    with Store.open(state) as store:
        assert store.current_version() == 1  # still not migrated


def test_internal_open_on_a_fresh_database_exits_before_writing(state: Path) -> None:
    prepare_state_dir(state, create=True)
    (state / "state.db").touch(mode=0o600)
    with pytest.raises(SchemaOlderError) as err:
        Store.open(state, mode=OpenMode.INTERNAL)
    assert err.value.found == 0
    assert (state / "state.db").stat().st_size == 0


def test_a_long_lived_check_on_a_newer_schema_reports_version_skew(state: Path) -> None:
    with Store.open(state) as long_lived:  # the old binary, knows version 1
        long_lived.raise_if_newer()  # same version: fine
        with Store.open(state, migrations=V2_COLUMN):  # an upgraded CLI command migrates
            pass
        with pytest.raises(JobsctlError) as err:
            long_lived.raise_if_newer()
        assert err.value.code == "version_skew"
        assert err.value.exit_code == 12
        assert "run it again" in err.value.message


def test_the_columns_allow_list_follows_a_migration(state: Path) -> None:
    with Store.open(state) as old:
        assert "score" not in old.columns("jobs")
        Store.open(state, migrations=V2_COLUMN).close()
        assert "score" in old.columns("jobs")
        with pytest.raises(ValueError):
            old.columns("jobs; DROP TABLE jobs")
        with pytest.raises(ValueError):
            old.columns("nope")


def test_sqlite_below_the_minimum_is_refused(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 31, 1))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.31.1")
    with pytest.raises(JobsctlError, match="too old"):
        Store.open(state)
    assert not state.exists()
