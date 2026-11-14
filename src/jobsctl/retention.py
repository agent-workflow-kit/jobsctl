"""Retention and `prune` (interface.md, `prune` and Pass step 6).

Terminal jobs whose `ended_at` is before the cutoff are deleted in batches of 100 per
`BEGIN IMMEDIATE` transaction. Inside one transaction a batch is selected, its `job:<id>` lease
rows are deleted, its ids are inserted into `pruned_ids` and the jobs are deleted (the child
tables cascade), so an id is never free while it is gone, and a concurrent `add` or transition
waits for the write lock instead of interleaving. A job with an undelivered hook event is never
selected. After the commit (`Tx.after_commit`) the job's files in `shims/` are removed and the
audit record(s) are appended: never inside the transaction.

Filesystem safety: file removals are anchored in the state directory by file descriptors
(`O_NOFOLLOW` on the state directory and on `shims/` and `diag/` below it, same device, owned by
us) and use `unlink(name, dir_fd=...)`, which never follows a symlink. Names are built only from
ids that match the job-id grammar and from integer attempt numbers, so nothing can traverse.
Counters (`seq:*`, `warn:*`) are never touched; `audit.jsonl` and `log.jsonl` are never rotated.

TODO(#14): the runner pass calls `run_retention` at step 6 with its own clock.
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
_MAX_ATTEMPT_FILES: Final = 1000  # bound on result files tried per job (rows are untrusted)

_ID_GLOB = "jb-" + "[a-z2-7]" * 8
_ID_RE = re.compile(r"jb-[a-z2-7]{8}")
_TERMINAL_SQL = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)


@dataclass
class PruneResult:
    pruned: list[str] = field(default_factory=list)
    out_of_budget: bool = False  # stopped at the budget; more may remain
    diag_removed: int = 0
    queues_forgotten: list[str] = field(default_factory=list)


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
        " AND ended_at < ? AND id GLOB ?"
        " AND NOT EXISTS (SELECT 1 FROM hook_outbox o WHERE o.job_id = jobs.id"
        " AND o.delivered_at IS NULL)"
    )
    params: list[object] = [cutoff, _ID_GLOB]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row[0] for row in tx.all(sql, params)]


def delete_batch(tx: Tx, ids: list[str]) -> dict[str, list[int]]:
    """Reserve and delete `ids` in the open write transaction.

    Returns each job's attempt numbers, for removing its result files."""
    attempts: dict[str, list[int]] = {}
    for job_id in ids:
        found = {row[0] for row in tx.all("SELECT n FROM attempts WHERE job_id = ?", [job_id])}
        row = tx.one("SELECT attempt FROM jobs WHERE id = ?", [job_id])
        last = min(int(row[0]), _MAX_ATTEMPT_FILES) if row is not None else 0
        found.update(range(1, last + 1))
        attempts[job_id] = sorted(n for n in found if 1 <= n <= _MAX_ATTEMPT_FILES)
        tx.execute("INSERT OR IGNORE INTO pruned_ids (id) VALUES (?)", [job_id])
        tx.execute("DELETE FROM leases WHERE name = ?", [f"job:{job_id}"])
        tx.execute("DELETE FROM jobs WHERE id = ?", [job_id])
    return attempts


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


def remove_job_files(paths: StatePaths, attempts: dict[str, list[int]]) -> None:
    """Remove `shims/<id>.log` and `shims/<id>.<n>.result` of pruned jobs (best effort)."""
    with _anchored(paths, "shims") as fd:
        if fd is None:
            return
        for job_id, numbers in attempts.items():
            if not _ID_RE.fullmatch(job_id):
                continue
            for name in [f"{job_id}.log", *(f"{job_id}.{n}.result" for n in numbers)]:
                with contextlib.suppress(OSError):  # missing, or a directory: leave it
                    os.unlink(name, dir_fd=fd)


def sweep_diag(paths: StatePaths, now: dt.datetime) -> int:
    """Delete non-directory entries of `diag/` last modified more than 7 days ago; returns the
    count. A symlink is removed itself, never followed. Runs whatever `retention` says."""
    limit = now.timestamp() - DIAG_MAX_AGE_SECONDS
    removed = 0
    with _anchored(paths, "diag") as fd:
        if fd is None:
            return 0
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode) or st.st_mtime >= limit:
                    continue
                os.unlink(name, dir_fd=fd)
                removed += 1
            except OSError:
                continue
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
) -> PruneResult:
    """Delete old terminal jobs batch by batch, one audit record per batch (split at
    `AUDIT_IDS_PER_RECORD`). With a `budget` in seconds, no new batch starts once it is spent."""
    result = PruneResult()
    started = monotonic()
    while True:
        if budget is not None and monotonic() - started >= budget:
            result.out_of_budget = True
            break
        with store.write() as tx:
            ids = candidates(tx, now, older_than, batch_size)
            if not ids:
                break
            attempts = delete_batch(tx, ids)
            tx.after_commit(lambda attempts=attempts: remove_job_files(store.paths, attempts))
            tx.after_commit(
                lambda ids=ids: _audit(writer, ids, older_than, trigger, actor, decision_ids)
            )
        result.pruned.extend(ids)
    return result


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
    when `null`) within `budget` seconds, then queue records nobody uses."""
    diag_removed = sweep_diag(store.paths, now)
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
        )
    result.diag_removed = diag_removed
    with store.write() as tx:
        result.queues_forgotten = forget_queues(tx, set(configured_queues))
    return result
