"""Retention and `prune` (interface.md, `prune` and Pass step 6).

Terminal jobs whose `ended_at` is before the cutoff are deleted in batches of 100 per
`BEGIN IMMEDIATE` transaction. Inside one transaction a batch is selected, its `job:<id>` lease
rows are deleted, its ids are inserted into `pruned_ids` and the jobs are deleted (the child
tables cascade), so an id is never free while it is gone, and a concurrent `add` or transition
waits for the write lock instead of interleaving. A job with an undelivered hook event is never
selected, and only a timestamp-shaped `ended_at` counts; the oldest go first. After the commit
(`Tx.after_commit`) the batch's ids are reported, the job's files in `shims/` are removed and
the audit record(s) are appended: never inside the transaction. An audit failure after a commit
does not undo the deletion: it is kept in `PruneResult.audit_error`, no further batch starts and
the partial result is returned. Files left behind by a crash between the commit and their
removal are removed by `sweep_orphans`.

Filesystem safety: file removals are anchored in the state directory by file descriptors
(`O_NOFOLLOW` on the state directory and on `shims/` and `diag/` below it, same device, owned by
us) and use `unlink(name, dir_fd=...)`, which never follows a symlink. Job files are found by
listing the anchored directory and matching names against the job-id grammar, so nothing can
traverse and no attempt number is out of reach.
Counters (`seq:*`, `warn:*`) are never touched; `audit.jsonl` and `log.jsonl` are never rotated.

The runner pass calls `run_retention` at step 6 (`Runner._retention`) with its own clock; it
treats `result.audit_error` and a store error as a failed step (logged) without skipping the
checkpoint or the lease release.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import stat
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from typing import Final

from jobsctl import gateway
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import Store, Tx
from jobsctl.store_dir import StatePaths
from jobsctl.store_schema import TERMINAL_STATUSES
from jobsctl.timestamps import TimestampError, format_timestamp

BATCH_SIZE: Final = 100
BUDGET_SECONDS: Final = 10.0
# An audit record holds about 13 bytes per id and is capped at 256 KiB (about 19,000 ids).
AUDIT_IDS_PER_RECORD: Final = 15000
DIAG_MAX_AGE_SECONDS: Final = 7 * 86400
RESERVED_ACTOR: Final = "auto:jobsctl"
ORPHAN_SCAN_CAP: Final = 10000  # shims/ entries looked at per orphan sweep

_ID_GLOB = "jb-" + "[a-z2-7]" * 8
_FILE_RE = re.compile(r"(jb-[a-z2-7]{8})(?:\.log|\.[0-9]+\.result)")
_TERMINAL_SQL = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)


@dataclass
class PruneResult:
    pruned: list[str] = field(default_factory=list)
    out_of_budget: bool = False  # stopped at the budget; more may remain
    diag_removed: int = 0
    queues_forgotten: list[str] = field(default_factory=list)
    orphans_removed: int = 0
    audit_error: Exception | None = None  # first audit append failure; the deletions stand


# -- selection and deletion --------------------------------------------------------------------


def _cutoff(now: dt.datetime, older_than: int) -> str | None:
    try:
        return format_timestamp(now - dt.timedelta(seconds=older_than))
    except (OverflowError, TimestampError):
        return None  # before year 1: nothing is that old


def candidates(
    tx: Tx, now: dt.datetime, older_than: int, limit: int | None = BATCH_SIZE
) -> list[str]:
    """Ids of terminal jobs ended before `now - older_than` with no undelivered hook event.

    Rows whose id is not a well-formed job id are never selected (they cannot be audited and
    their name must not reach the filesystem). `limit=None` lists all (`--dry-run`)."""
    cutoff = _cutoff(now, older_than)
    if cutoff is None:
        return []
    sql = (
        f"SELECT id FROM jobs WHERE status IN ({_TERMINAL_SQL}) AND ended_at IS NOT NULL"
        " AND ended_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*Z'"
        " AND ended_at < ? AND id GLOB ?"
        " AND NOT EXISTS (SELECT 1 FROM hook_outbox o WHERE o.job_id = jobs.id"
        " AND o.delivered_at IS NULL) ORDER BY ended_at, id"
    )
    params: list[object] = [cutoff, _ID_GLOB]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row[0] for row in tx.all(sql, params)]


def delete_batch(tx: Tx, ids: list[str]) -> None:
    """Reserve and delete `ids` in the open write transaction."""
    for job_id in ids:
        tx.execute("INSERT OR IGNORE INTO pruned_ids (id) VALUES (?)", [job_id])
        tx.execute("DELETE FROM leases WHERE name = ?", [f"job:{job_id}"])
        tx.execute("DELETE FROM jobs WHERE id = ?", [job_id])


def forget_queues(tx: Tx, configured: Collection[str]) -> list[str]:
    """Delete `queues_seen` rows of queues that are not configured and have no jobs."""
    gone = [
        row[0]
        for row in tx.all(
            "SELECT name FROM queues_seen WHERE NOT EXISTS"
            " (SELECT 1 FROM jobs WHERE jobs.queue = queues_seen.name)"
        )
        if row[0] not in configured
    ]
    for name in gone:
        tx.execute("DELETE FROM queues_seen WHERE name = ?", [name])
    return sorted(gone)


# -- files -------------------------------------------------------------------------------------


@contextlib.contextmanager
def _anchored(paths: StatePaths, sub: str) -> Iterator[int | None]:
    """An fd of `<state dir>/<sub>`, or None when it is missing or not a safe directory.

    The state directory and `sub` are opened without following a final symlink, must be owned
    by us and on one device."""
    root = sub_fd = None
    safe = False
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        root = os.open(paths.root, flags)
        root_st = os.fstat(root)
        sub_fd = os.open(sub, flags, dir_fd=root)
        st = os.fstat(sub_fd)
        safe = (
            root_st.st_uid == os.geteuid()
            and st.st_uid == os.geteuid()
            and st.st_dev == root_st.st_dev
        )
    except OSError:
        safe = False
    try:
        yield sub_fd if safe else None
    finally:
        for fd in (sub_fd, root):
            if fd is not None:
                os.close(fd)


def _unlink(fd: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=fd)  # a symlink is removed itself, a directory is left
    except OSError:
        return False
    return True


def remove_job_files(paths: StatePaths, ids: Collection[str]) -> None:
    """Remove `shims/<id>.log` and `shims/<id>.<n>.result` of pruned jobs (best effort), found
    by listing the directory."""
    wanted = set(ids)
    with _anchored(paths, "shims") as fd:
        if fd is None:
            return
        try:
            with os.scandir(fd) as entries:
                names = [
                    entry.name
                    for entry in entries
                    if (m := _FILE_RE.fullmatch(entry.name)) and m.group(1) in wanted
                ]
        except OSError:
            return
        for name in names:
            _unlink(fd, name)


def sweep_orphans(
    store: Store, *, cap: int = ORPHAN_SCAN_CAP, out_of_time: Callable[[], bool] | None = None
) -> int:
    """Remove job files in `shims/` whose id is in `pruned_ids` (left by a crash between a
    commit and the removal); looks at no more than `cap` matching entries. Returns the count."""
    with _anchored(store.paths, "shims") as fd:
        if fd is None:
            return 0
        found: list[tuple[str, str]] = []
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    m = _FILE_RE.fullmatch(entry.name)
                    if m:
                        if len(found) >= cap:
                            break
                        found.append((entry.name, m.group(1)))
        except OSError:
            return 0
        ids = sorted({job_id for _, job_id in found})
        reserved: set[str] = set()
        with store.read() as tx:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                sql = f"SELECT id FROM pruned_ids WHERE id IN ({marks})"
                reserved.update(row[0] for row in tx.all(sql, chunk))
        removed = 0
        for name, job_id in found:
            if out_of_time is not None and out_of_time():
                break
            if job_id in reserved and _unlink(fd, name):
                removed += 1
        return removed


def sweep_diag(
    paths: StatePaths, now: dt.datetime, out_of_time: Callable[[], bool] | None = None
) -> int:
    """Delete regular files and symlinks of `diag/` that we own and that were last modified
    more than 7 days ago; returns the count. A symlink is removed itself, never followed;
    directories, FIFOs, sockets, devices and foreign files are left. Stops when `out_of_time`
    says so. Runs whatever `retention` says."""
    limit = now.timestamp() - DIAG_MAX_AGE_SECONDS
    removed = 0
    uid = os.geteuid()
    with _anchored(paths, "diag") as fd:
        if fd is None:
            return 0
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if out_of_time is not None and out_of_time():
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    continue
                if st.st_uid != uid or st.st_mtime >= limit:
                    continue
                names.append(entry.name)
        for name in names:
            if _unlink(fd, name):
                removed += 1
    return removed


# -- the loops ---------------------------------------------------------------------------------


def prune_jobs(
    store: Store,
    writer: RecordWriter,
    *,
    older_than: int,
    trigger: str,
    actor: str,
    decision_ids: list[str],
    now: dt.datetime,
    monotonic: Callable[[], float] = gateway.monotonic,
    budget: float | None = None,
    batch_size: int = BATCH_SIZE,
    started: float | None = None,
) -> PruneResult:
    """Delete old terminal jobs batch by batch, one audit record per batch (split at
    `AUDIT_IDS_PER_RECORD`). With a `budget` in seconds, no new batch starts once it is spent
    (counted from `started`, default now). A failing audit append stops the loop and is left
    in `audit_error`; the batches already committed are in `pruned`."""
    result = PruneResult()
    if started is None:
        started = monotonic()
    while True:
        if budget is not None and monotonic() - started >= budget:
            result.out_of_budget = True
            break
        with store.write() as tx:
            ids = candidates(tx, now, older_than, batch_size)
            if not ids:
                break
            delete_batch(tx, ids)
            tx.after_commit(
                lambda ids=ids: _after_commit(
                    store, writer, result, ids, older_than, trigger, actor, decision_ids
                )
            )
        if result.audit_error is not None:
            break
    return result


def _after_commit(
    store: Store,
    writer: RecordWriter,
    result: PruneResult,
    ids: list[str],
    older_than: int,
    trigger: str,
    actor: str,
    decision_ids: list[str],
) -> None:
    result.pruned.extend(ids)  # first: the deletion stands whatever happens next
    remove_job_files(store.paths, ids)
    try:
        _audit(writer, ids, older_than, trigger, actor, decision_ids)
    except Exception as exc:  # reported through the result, never lost with the ids
        result.audit_error = result.audit_error or exc


def _audit(
    writer: RecordWriter,
    ids: list[str],
    older_than: int,
    trigger: str,
    actor: str,
    decision_ids: list[str],
) -> None:
    for start in range(0, len(ids), AUDIT_IDS_PER_RECORD):
        writer.audit(
            action="prune",
            actor=actor,
            target=None,
            inputs={
                "ids": ids[start : start + AUDIT_IDS_PER_RECORD],
                "older_than": older_than,
                "trigger": trigger,
            },
            result="deleted",
            decision_id=decision_ids,
        )


def run_retention(
    store: Store,
    writer: RecordWriter,
    *,
    retention: int | None,
    configured_queues: Collection[str],
    now: dt.datetime,
    monotonic: Callable[[], float] = gateway.monotonic,
    budget: float = BUDGET_SECONDS,
    batch_size: int = BATCH_SIZE,
) -> PruneResult:
    """The retention step of a pass: old `diag/` files, jobs older than `retention` (skipped
    when `null`), leftover job files, then queue records nobody uses; the first three share
    `budget` seconds. A failed audit append is left in `result.audit_error`."""
    started = monotonic()

    def out_of_time() -> bool:
        return monotonic() - started >= budget

    diag_removed = sweep_diag(store.paths, now, out_of_time)
    if retention is None:
        result = PruneResult()
    else:
        result = prune_jobs(
            store,
            writer,
            older_than=retention,
            trigger="retention",
            actor=RESERVED_ACTOR,
            decision_ids=[],
            now=now,
            monotonic=monotonic,
            budget=budget,
            batch_size=batch_size,
            started=started,
        )
    result.diag_removed = diag_removed
    result.orphans_removed = sweep_orphans(store, out_of_time=out_of_time)
    with store.write() as tx:
        result.queues_forgotten = forget_queues(tx, set(configured_queues))
    return result
