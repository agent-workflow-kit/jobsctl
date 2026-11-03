"""The state store: connection setup, write transactions, migrations and version checks.

Every connection uses ``autocommit=True`` and explicit transactions (design.md, Data model;
spike B1): a write is ``BEGIN IMMEDIATE`` ... ``COMMIT`` through :meth:`Store.write`, never a
plain ``BEGIN`` that upgrades to a write (that fails with "database is locked" despite the busy
timeout), and ``journal_mode=WAL`` is set outside any transaction (Python 3.12's legacy
transaction mode ignores it inside one).

CLI commands open with :attr:`OpenMode.CLI`, which creates the state directory and migrates an
older schema. Internal processes open with :attr:`OpenMode.INTERNAL`: they never create or
migrate, and exit with :class:`SchemaOlderError` before writing on an older schema. A newer
schema is not an error at open (migrations are additive, so older code keeps working); long-lived
processes call :meth:`Store.raise_if_newer` before each iteration to report ``version_skew``.

No process logs inside a write transaction: register the log write with :meth:`Tx.after_commit`.
A gap-free counter (``seq``) can be allocated inside the same transaction with
``store_ops.counter_incr``, so a rolled-back transaction leaves no gap.
"""

from __future__ import annotations

import enum
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from jobsctl.errors import JobsctlError
from jobsctl.store_dir import StatePaths, prepare_db_file, prepare_state_dir, resolve_state_dir
from jobsctl.store_schema import MIGRATIONS, check_additive

DEFAULT_BUSY_TIMEOUT = 5.0  # seconds; TODO(#3): the `busy_timeout` config key
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
MIN_SQLITE = (3, 35, 0)  # UPSERT and RETURNING

_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6
_BUSY_CODES = (_SQLITE_BUSY, _SQLITE_LOCKED)
_CORRUPT_CODES = (11, 26)  # SQLITE_CORRUPT, SQLITE_NOTADB
_TX_CONTROL_RE = re.compile(r"^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I)
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*\Z")


class OpenMode(enum.Enum):
    CLI = "cli"
    INTERNAL = "internal"


class SchemaOlderError(JobsctlError):
    """An internal process found a schema older than it knows; it must exit before writing.

    Exit 1 (`internal`). The next CLI command migrates; this is not a shim-spawn failure
    (interface.md, Upgrades).
    """

    def __init__(self, found: int, known: int) -> None:
        super().__init__(
            "internal",
            f"state.db schema v{found} is older than this jobsctl (v{known}); "
            "run any jobsctl command to migrate it",
        )
        self.found = found
        self.known = known


def translate(exc: sqlite3.Error, *, timeout: float | None = None) -> JobsctlError:
    """Map a sqlite3 error to a JobsctlError: `busy` for lock timeouts, else `internal`."""
    code = getattr(exc, "sqlite_errorcode", None)
    primary = None if code is None else code & 0xFF
    if primary in _BUSY_CODES or "database is locked" in str(exc):
        waited = f" after {timeout:g}s" if timeout is not None else ""
        return JobsctlError("busy", f"state.db is busy{waited}; try again")
    if primary in _CORRUPT_CODES:
        return JobsctlError("internal", f"state.db is corrupt or not a database ({exc})")
    return JobsctlError("internal", f"state.db error: {exc}")


class Tx:
    """A transaction on a :class:`Store` connection; valid only inside its ``with`` block."""

    def __init__(self, store: Store, *, writable: bool) -> None:
        self._store = store
        self._writable = writable
        self._open = True
        self._callbacks: list[Callable[[], None]] = []

    @property
    def store(self) -> Store:
        return self._store

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        if not self._open:
            raise RuntimeError("the transaction is closed")
        if _TX_CONTROL_RE.match(sql):
            raise ValueError("transaction control belongs to Store.write()/read()")
        try:
            return self._store.conn.execute(sql, params)
        except sqlite3.IntegrityError:
            raise  # callers decide what a constraint violation means
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self._store.busy_timeout) from exc

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        cur = self.execute(sql, params)
        try:  # stepping to the next row can fail too (busy, corrupt page)
            row = cur.fetchone()
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self._store.busy_timeout) from exc
        assert row is None or isinstance(row, sqlite3.Row)
        return row

    def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        cur = self.execute(sql, params)
        try:
            return list(cur.fetchall())
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self._store.busy_timeout) from exc

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after a successful COMMIT (never on rollback): the place to log."""
        if not self._writable:
            raise RuntimeError("only a write transaction has commit callbacks")
        self._callbacks.append(callback)


class Store:
    """An open ``state.db``. One connection; not thread-safe (each process opens its own)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: StatePaths,
        *,
        mode: OpenMode,
        busy_timeout: float,
        migrations: tuple[tuple[str, ...], ...],
        schema_version: int,
    ) -> None:
        self.conn = conn
        self.paths = paths
        self.mode = mode
        self.busy_timeout = busy_timeout
        self.migrations = migrations
        self.schema_version = schema_version
        self._columns: dict[str, frozenset[str]] = {}
        self._columns_version = -1

    @property
    def known_version(self) -> int:
        return len(self.migrations)

    # -- opening -----------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        state_dir: Path | None = None,
        *,
        mode: OpenMode = OpenMode.CLI,
        busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
        migrations: tuple[tuple[str, ...], ...] = MIGRATIONS,
    ) -> Self:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise JobsctlError(
                "internal",
                f"SQLite {sqlite3.sqlite_version} is too old; jobsctl needs 3.35 or later",
            )
        create = mode is OpenMode.CLI
        paths = prepare_state_dir(state_dir or resolve_state_dir(), create=create)
        prepare_db_file(paths.db, create=create)
        try:
            conn = sqlite3.connect(paths.db, timeout=busy_timeout, autocommit=True)
        except sqlite3.Error as exc:
            raise translate(exc, timeout=busy_timeout) from exc
        conn.row_factory = sqlite3.Row
        try:
            version = _refuse_foreign(conn)  # before _configure, so a refused file is untouched
            known = len(migrations)
            if mode is OpenMode.INTERNAL and version < known:
                raise SchemaOlderError(version, known)
            _configure(conn, busy_timeout)
            if version < known:
                version = _migrate(conn, migrations)
        except sqlite3.Error as exc:
            conn.close()
            raise translate(exc, timeout=busy_timeout) from exc
        except BaseException:
            conn.close()
            raise
        return cls(
            conn,
            paths,
            mode=mode,
            busy_timeout=busy_timeout,
            migrations=migrations,
            schema_version=version,
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transactions ------------------------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``; roll back on any exception.

        Waits up to ``busy_timeout`` for the write lock, then raises `busy` (exit 1). Not
        reentrant. Long work (hooks, waiting, process spawns other than a job's own exec) never
        happens inside.
        """
        if self.conn.in_transaction:
            raise RuntimeError("a transaction is already open on this connection")
        self._begin("BEGIN IMMEDIATE")
        tx = Tx(self, writable=True)
        try:
            yield tx
        except BaseException:
            tx._open = False
            self._rollback()
            raise
        tx._open = False
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback()
            raise translate(exc, timeout=self.busy_timeout) from exc
        first: BaseException | None = None
        for callback in tx._callbacks:
            try:
                callback()
            except BaseException as exc:  # run every callback (even after Ctrl-C), report the first
                first = first or exc
        if first is not None:
            raise first

    @contextmanager
    def read(self) -> Iterator[Tx]:
        """A deferred read transaction: one consistent snapshot; writes are refused."""
        if self.conn.in_transaction:
            raise RuntimeError("a transaction is already open on this connection")
        self._begin("BEGIN")
        tx = Tx(self, writable=False)
        try:
            self.conn.execute("PRAGMA query_only = ON")
            yield tx
        finally:
            tx._open = False
            try:
                self.conn.execute("PRAGMA query_only = OFF")
            finally:
                self._rollback()

    def _begin(self, sql: str) -> None:
        try:
            self.conn.execute(sql)
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self.busy_timeout) from exc

    def _rollback(self) -> None:
        if self.conn.in_transaction:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # the connection is unusable; the transaction ends with it

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """One read outside any transaction (autocommit)."""
        try:
            row = self.conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self.busy_timeout) from exc
        assert row is None or isinstance(row, sqlite3.Row)
        return row

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return list(self.conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self.busy_timeout) from exc

    # -- versions ----------------------------------------------------------------------------

    def current_version(self) -> int:
        """``PRAGMA user_version`` as it is now (another process may have migrated)."""
        try:
            return _read_version(self.conn)
        except sqlite3.Error as exc:
            raise translate(exc, timeout=self.busy_timeout) from exc

    def raise_if_newer(self) -> None:
        """Long-lived processes call this before each iteration or transaction.

        Raises `version_skew` (exit 12) when the schema is newer than this binary knows.
        """
        if self.current_version() > self.known_version:
            raise JobsctlError(
                "version_skew",
                "jobsctl was upgraded underneath this process; run it again",
            )

    # -- schema introspection ----------------------------------------------------------------

    def columns(self, table: str) -> frozenset[str]:
        """Column names of ``table`` (an allow-list for the dynamic SQL in ``store_ops``)."""
        if not _IDENT_RE.match(table):
            raise ValueError(f"not a table name: {table!r}")
        version = self.current_version()
        if version != self._columns_version:  # a migration may have added columns
            self._columns.clear()
            self._columns_version = version
        cached = self._columns.get(table)
        if cached is None:
            rows = self.query_all(f'PRAGMA table_info("{table}")')
            cached = frozenset(str(r["name"]) for r in rows)
            self._columns[table] = cached
        if not cached:
            raise ValueError(f"unknown table: {table!r}")
        return cached


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _refuse_foreign(conn: sqlite3.Connection) -> int:
    """Refuse a database jobsctl did not create: a negative version, or version 0 with tables.

    Returns the version. It and the table count come from one statement, so a migration another
    process commits in between cannot make a fresh database look foreign.
    """
    version, count = conn.execute(
        "SELECT (SELECT user_version FROM pragma_user_version),"
        " (SELECT count(*) FROM sqlite_master)"
    ).fetchone()
    version = int(version)
    if version < 0:
        raise JobsctlError("internal", f"state.db is not a jobsctl database (version {version})")
    if version == 0 and count:
        raise JobsctlError("internal", "state.db is not a jobsctl database (unversioned)")
    return version


def _configure(conn: sqlite3.Connection, busy_timeout: float) -> None:
    """Per-connection pragmas; WAL is persistent, set outside any transaction."""
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
    deadline = time.monotonic() + busy_timeout
    while True:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode == "wal":
            break
        try:
            mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            # Switching the journal mode can fail with "locked" while another process does the
            # same on a fresh database; the busy handler is not always consulted.
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
            continue
        if mode != "wal":
            raise JobsctlError("internal", f"state.db cannot use WAL (journal_mode is {mode})")
        break
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate(conn: sqlite3.Connection, migrations: tuple[tuple[str, ...], ...]) -> int:
    """Apply pending migrations in one ``BEGIN IMMEDIATE`` transaction; return the new version."""
    for step in migrations:
        for statement in step:
            check_additive(statement)
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _refuse_foreign(conn)  # another process may have migrated while we waited
        for target in range(current, len(migrations)):
            for statement in migrations[target]:
                conn.execute(statement)
        if current < len(migrations):
            conn.execute(f"PRAGMA user_version = {len(migrations)}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return max(current, len(migrations))
