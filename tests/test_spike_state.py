"""Platform probes for spikes B and C5/C6 (design.md, Live verification): SQLite, pipes, files.

These test the host kernel, Python and SQLite, not jobsctl. They fail when a platform change
breaks something the design relies on (WAL and busy semantics, SIGPIPE, flock, O_APPEND,
O_NOFOLLOW). The map from every spike item to its tests is in design.md, "Regression coverage".
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import signal
import sqlite3
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from tests.spike_helpers import (
    PY,
    Procs,
    linux_only,
    read_line,
    wait_for,
)


def py(code: str, *args: str) -> list[str]:
    return [*PY, "-c", textwrap.dedent(code), *args]


def run_all(procs: Procs, commands: list[list[str]], timeout: float = 120.0) -> list[str]:
    """Start all at once, wait for all (bounded), return stdouts; any non-zero exit fails."""
    started = [procs.popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for c in commands]
    outs = []
    for p in started:
        out, err = p.communicate(timeout=timeout)
        assert p.returncode == 0, err.decode()
        outs.append(out.decode())
    return outs


def make_db(path: Path, ddl: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.executescript(ddl)
    conn.close()


# -- B1: caps under concurrency -----------------------------------------------------------------

CLAIM_WORKER = """
    import sqlite3, sys
    db, mode, attempts, cap, owner = sys.argv[1:6]
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    won = 0
    for _ in range(int(attempts)):
        if mode == "immediate":
            conn.execute("BEGIN IMMEDIATE")
            (n,) = conn.execute("SELECT count(*) FROM claims").fetchone()
            if n < int(cap):
                conn.execute("INSERT INTO claims(owner) VALUES (?)", (owner,))
                won += 1
            conn.execute("COMMIT")
        else:
            row = conn.execute(
                "UPDATE slots SET owner = ? WHERE id = (SELECT id FROM slots WHERE owner IS NULL"
                " AND (SELECT count(*) FROM slots WHERE owner IS NOT NULL) < ? LIMIT 1)"
                " RETURNING id", (owner, int(cap))).fetchone()
            won += row is not None
    print(won)
"""


def test_b1_sqlite_has_upsert_and_returning() -> None:
    assert sqlite3.sqlite_version_info >= (3, 35)  # the store refuses older libraries


@pytest.mark.parametrize("mode", ["immediate", "update_returning"])
def test_b1_count_and_claim_never_exceeds_the_cap_across_processes(
    procs: Procs, tmp_path: Path, mode: str
) -> None:
    db = tmp_path / "caps.db"
    make_db(
        db,
        "CREATE TABLE claims(id INTEGER PRIMARY KEY, owner TEXT);"
        "CREATE TABLE slots(id INTEGER PRIMARY KEY, owner TEXT);"
        "INSERT INTO slots(id) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10);",
    )
    cap = 3
    outs = run_all(
        procs, [py(CLAIM_WORKER, str(db), mode, "15", str(cap), f"w{i}") for i in range(6)]
    )
    assert sum(int(o) for o in outs) == cap
    conn = sqlite3.connect(db)
    table = "claims" if mode == "immediate" else "slots WHERE owner IS NOT NULL"
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == cap  # noqa: S608


def test_b1_a_deferred_read_then_write_fails_at_once_after_another_commit(tmp_path: Path) -> None:
    db = tmp_path / "plain.db"
    make_db(db, "CREATE TABLE t(v INTEGER);")
    first = sqlite3.connect(db, timeout=30, isolation_level=None)
    second = sqlite3.connect(db, timeout=30, isolation_level=None)
    first.execute("BEGIN")
    first.execute("SELECT count(*) FROM t").fetchone()
    second.execute("INSERT INTO t VALUES (1)")
    began = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        first.execute("INSERT INTO t VALUES (2)")
    assert time.monotonic() - began < 5  # an immediate failure, not the 30 s busy wait
    first.execute("ROLLBACK")


# -- B2: contention and crashes ----------------------------------------------------------------

WRITER = """
    import sqlite3, sys
    conn = sqlite3.connect(sys.argv[1], timeout=10, isolation_level=None)
    conn.execute("PRAGMA synchronous=NORMAL")
    for i in range(int(sys.argv[2])):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(v) VALUES (?)", (i,))
        conn.execute("COMMIT")
    print("ok")
"""


def test_b2_many_writers_under_wal_get_no_lock_errors(procs: Procs, tmp_path: Path) -> None:
    db = tmp_path / "wal.db"
    make_db(db, "CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER);")
    outs = run_all(procs, [py(WRITER, str(db), "30") for _ in range(12)])
    assert outs == ["ok\n"] * 12
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 360
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_b2_sigkill_mid_transaction_leaves_the_database_intact_and_unlocked(
    procs: Procs, tmp_path: Path
) -> None:
    db = tmp_path / "crash.db"
    make_db(db, "CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER); INSERT INTO t(v) VALUES (0);")
    holder = procs.popen(
        py(
            """
            import sqlite3, sys, time
            conn = sqlite3.connect(sys.argv[1], isolation_level=None)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO t(v) VALUES (99)")
            print("ready", flush=True)
            time.sleep(600)
            """,
            str(db),
        ),
        stdout=subprocess.PIPE,
    )
    assert read_line(holder) == "ready"
    other = sqlite3.connect(db, timeout=0.2, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError, match="locked"):  # the holder really holds it
        other.execute("BEGIN IMMEDIATE")
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=15)
    fresh = sqlite3.connect(db, timeout=5, isolation_level=None)
    began = time.monotonic()
    fresh.execute("BEGIN IMMEDIATE")  # unlocked at once: the dead holder's lock is gone
    fresh.execute("INSERT INTO t(v) VALUES (1)")
    fresh.execute("COMMIT")
    assert time.monotonic() - began < 4
    assert fresh.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [r[0] for r in fresh.execute("SELECT v FROM t ORDER BY id")] == [0, 1]  # no 99


# -- B3: lease identity ingredients -------------------------------------------------------------


@linux_only
def test_b3_boot_id_is_a_stable_uuid() -> None:
    first = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", first)
    assert Path("/proc/sys/kernel/random/boot_id").read_text().strip() == first


# -- B4: output capture -------------------------------------------------------------------------


def test_b4_a_writer_to_a_pipe_with_no_reader_dies_of_sigpipe(procs: Procs) -> None:
    read_end, write_end = os.pipe()
    child = procs.popen(
        py(
            """
            import signal, sys
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
            sys.stdin.readline()
            sys.stdout.write("x" * 100000)
            sys.stdout.flush()
            """
        ),
        stdin=subprocess.PIPE,
        stdout=write_end,
    )
    os.close(write_end)
    os.close(read_end)  # the reader (the shim) is gone
    assert child.stdin is not None
    child.stdin.write(b"go\n")
    child.stdin.flush()
    assert child.wait(timeout=30) == -signal.SIGPIPE


def test_b4_a_pipe_is_bounded_and_a_write_to_a_closed_reader_is_epipe() -> None:
    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)
    total = 0
    with pytest.raises(BlockingIOError):
        while True:
            total += os.write(write_end, b"x" * 4096)
    assert 4096 <= total <= 64 * 1024 * 1024  # a full pipe blocks the writer (bounded memory)
    os.close(read_end)
    previous = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        with pytest.raises(BrokenPipeError):
            os.write(write_end, b"y")
    finally:
        signal.signal(signal.SIGPIPE, previous)
        os.close(write_end)


# -- B7: encoding -------------------------------------------------------------------------------


def test_b7_backslashreplace_keeps_information_and_surrogateescape_strings_cannot_be_stored(
    tmp_path: Path,
) -> None:
    assert b"a\xffb".decode("utf-8", "backslashreplace") == "a\\xffb"
    escaped = b"a\xffb".decode("utf-8", "surrogateescape")
    with pytest.raises(UnicodeEncodeError):
        escaped.encode("utf-8")
    conn = sqlite3.connect(tmp_path / "enc.db")
    conn.execute("CREATE TABLE t(s TEXT)")
    with pytest.raises(UnicodeEncodeError):
        conn.execute("INSERT INTO t VALUES (?)", (escaped,))
    conn.execute("INSERT INTO t VALUES (?)", ("a\\xffb",))


# -- C5: O_NOFOLLOW ------------------------------------------------------------------------------


def test_c5_a_final_symlink_is_eloop_and_a_symlinked_ancestor_is_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "file").write_text("x")
    (tmp_path / "link").symlink_to(real / "file")
    with pytest.raises(OSError) as info:
        os.open(tmp_path / "link", os.O_RDONLY | os.O_NOFOLLOW)
    assert info.value.errno == errno.ELOOP
    (tmp_path / "dirlink").symlink_to(real, target_is_directory=True)
    fd = os.open(tmp_path / "dirlink" / "file", os.O_RDONLY | os.O_NOFOLLOW)  # not refused
    os.close(fd)


def test_c5_a_fifo_without_a_writer_opens_nonblocking_and_fstat_rejects_it(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # does not hang
    try:
        assert stat.S_ISFIFO(os.fstat(fd).st_mode)
        assert not stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


# -- C6: log seq order ---------------------------------------------------------------------------

SEQ_WORKER = """
    import fcntl, os, sqlite3, sys
    d, n, size = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    conn = sqlite3.connect(d + "/counter.db", timeout=60, isolation_level=None)
    lock = os.open(d + "/lock", os.O_CREAT | os.O_RDWR)
    log = os.open(d + "/log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    for _ in range(n):
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE counter SET v = v + 1")
            seq = conn.execute("SELECT v FROM counter").fetchone()[0]
            conn.execute("COMMIT")
            line = ("%d %s\\n" % (seq, "x" * size)).encode()
            assert os.write(log, line) == len(line)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    print("ok")
"""


def test_c6_flock_around_the_counter_and_one_append_gives_seq_in_file_order(
    procs: Procs, tmp_path: Path
) -> None:
    make_db(
        tmp_path / "counter.db", "CREATE TABLE counter(v INTEGER); INSERT INTO counter VALUES (0);"
    )
    run_all(procs, [py(SEQ_WORKER, str(tmp_path), "100", "20") for _ in range(6)])
    lines = (tmp_path / "log").read_bytes().split(b"\n")
    assert lines.pop() == b""  # the file ends in a newline: no torn tail
    assert [int(ln.split()[0]) for ln in lines] == list(range(1, 601))  # gap-free, in order


APPENDER = """
    import os, sys
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    char = sys.argv[2]
    for _ in range(int(sys.argv[3])):
        record = (char * int(sys.argv[4]) + "\\n").encode()
        assert os.write(fd, record) == len(record)
    print("ok")
"""


def test_c6_large_single_append_writes_from_concurrent_processes_are_never_torn(
    procs: Procs, tmp_path: Path
) -> None:
    path = tmp_path / "big.log"
    size = 200_000  # records of up to 256 KiB
    run_all(procs, [py(APPENDER, str(path), chr(ord("a") + i), "20", str(size)) for i in range(8)])
    lines = path.read_bytes().split(b"\n")
    assert lines.pop() == b""
    assert len(lines) == 8 * 20
    for line in lines:
        assert len(line) == size and len(set(line)) == 1  # one writer's record, whole


def test_c6_flock_conflicts_between_descriptions_and_dies_with_its_holder(
    procs: Procs, tmp_path: Path
) -> None:
    lock = tmp_path / "lock"
    first = os.open(lock, os.O_CREAT | os.O_RDWR)
    second = os.open(lock, os.O_RDWR)  # another open file description, same process
    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(first, fcntl.LOCK_UN)
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(second, fcntl.LOCK_UN)
        holder = procs.popen(
            py(
                """
                import fcntl, os, sys, time
                fd = os.open(sys.argv[1], os.O_RDWR)
                fcntl.flock(fd, fcntl.LOCK_EX)
                print("held", flush=True)
                time.sleep(600)
                """,
                str(lock),
            ),
            stdout=subprocess.PIPE,
        )
        assert read_line(holder) == "held"
        with pytest.raises(BlockingIOError):
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=15)

        def taken() -> bool:
            try:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

        assert wait_for(taken)  # released by the kernel when the holder died
    finally:
        os.close(first)
        os.close(second)


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="no /dev/full on this host")
def test_c6_a_write_with_no_space_left_is_enospc() -> None:
    fd = os.open("/dev/full", os.O_WRONLY | os.O_APPEND)
    try:
        with pytest.raises(OSError) as info:
            os.write(fd, b"x")
        assert info.value.errno == errno.ENOSPC
    finally:
        os.close(fd)
