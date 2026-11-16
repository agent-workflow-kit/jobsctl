"""What `browse` shows, read in one bounded read transaction and already sanitized.

Every string that reaches the screen is built here from stored (untrusted) data and passes the
sanitizer of `textsafe` (the one `list` and `show` use), so the widgets only have to display it
as plain text, never as markup. Nothing here imports Textual; nothing writes to the database.
"""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from typing import Any, Final

from jobsctl import gateway
from jobsctl.cli_registry import Context
from jobsctl.commands.listing import list_jobs
from jobsctl.config import Config
from jobsctl.env_file import load_env_file
from jobsctl.job_detail import job_detail
from jobsctl.job_text import clean, show_text
from jobsctl.job_view import job_object
from jobsctl.order import (
    counts_by_status,
    global_capacity,
    known_queues,
    load_snapshot,
    queue_capacity,
)
from jobsctl.store_db import Store, Tx
from jobsctl.store_schema import ALL_STATUSES
from jobsctl.timestamps import format_timestamp

JOB_LIMIT: Final = 500  # jobs shown per queue; more is said in the status line
ARGV_WIDTH: Final = 100
QUEUE_COLUMNS: Final = ("QUEUE", "CAP", "RUN", "QUEUED", "HELD")
JOB_COLUMNS: Final = ("", "ID", "STATE", "RANK", "PRI", "COMMAND")


@dataclass(frozen=True)
class QueueItem:
    name: str  # as stored: the key; `cells` are what is shown
    cells: tuple[str, ...]


@dataclass(frozen=True)
class JobItem:
    id: str
    queue: str
    status: str
    hold_reason: str | None
    priority: int
    pinned: bool
    canceling: bool  # a force request is recorded and the job has not ended yet
    cells: tuple[str, ...]  # without the selection mark
    haystack: str  # lower-cased text the filter searches


@dataclass(frozen=True)
class BrowseData:
    queues: tuple[QueueItem, ...] = ()
    queue: str | None = None  # the selected queue (as stored)
    jobs: tuple[JobItem, ...] = ()
    truncated: bool = False
    detail_id: str | None = None
    detail: str = ""
    totals: str = ""
    note: str = ""  # e.g. "no state database yet"


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def state_text(job: dict[str, Any]) -> tuple[str, bool]:
    """The state column, and whether the job is being force-canceled."""
    state = clean(job["status"])
    reason = job["reason"] or job["hold_reason"]
    if reason:
        state += f"({clean(reason)})"
    canceling = job["force_requested_by"] is not None and job["status"] in ("starting", "running")
    if canceling:
        state += " canceling"
    return state, canceling


def job_item(job: dict[str, Any]) -> JobItem:
    state, canceling = state_text(job)
    command = _short(" ".join(shlex.quote(clean(a)) for a in job["argv"]), ARGV_WIDTH)
    rank = "" if job["rank"] is None else clean(job["rank"])
    priority = ("^" if job["pinned"] else "") + clean(job["priority"])
    cells = (clean(job["id"]), state, rank, priority, command)
    labels = " ".join(clean(label) for label in job["labels"])
    haystack = " ".join((*cells, clean(job["queue"]), labels)).lower()
    return JobItem(
        id=str(job["id"]),
        queue=str(job["queue"]),
        status=str(job["status"]),
        hold_reason=job["hold_reason"],
        priority=int(job["priority"]),
        pinned=bool(job["pinned"]),
        canceling=canceling,
        cells=cells,
        haystack=haystack,
    )


def _listing_context(config: Config, queue: str) -> Context:
    """A `list` context for `list_jobs`: the selected queue, the usual 24 hour window."""
    args = argparse.Namespace(
        queue=queue, status=None, label=None, unique_key=None, all=False, limit=JOB_LIMIT
    )
    return Context(
        args=args,
        command="list",
        json=False,
        verbose=False,
        quiet=True,
        config_path=None,
        config=config,
        stdin_isatty=False,
        me_name="",
        explicit_actor=None,
    )


def _env_unavailable(config: Config) -> bool:
    return load_env_file(config.env_file).status == "unavailable"


def _detail(tx: Tx, config: Config, job_id: str, now: str) -> str | None:
    row = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
    if row is None:
        return None
    job = job_detail(
        tx, row, config, env_file_unavailable=_env_unavailable(config), now=now, quiet=True
    )
    return show_text(job)


def load(
    store: Store,
    config: Config,
    *,
    queue: str | None,
    job_id: str | None,
    focus: str | None = None,
) -> BrowseData:
    """One consistent snapshot: the queues, the jobs of `queue` (the first queue when `None`, or
    the queue of `focus` when it names a job), the detail of `job_id`. `store` is read-only."""
    stamp = gateway.utc_now()
    now = format_timestamp(stamp)
    with store.read() as tx:
        names = known_queues(tx, config)
        focused = None if focus is None else tx.one("SELECT * FROM jobs WHERE id = ?", [focus])
        if focused is not None:
            queue, job_id = str(focused["queue"]), focus
        if queue not in names:
            queue = names[0] if names else None
        snapshot = load_snapshot(tx, config)
        items: list[QueueItem] = []
        for name in names:
            cap = queue_capacity(
                snapshot, name, known=True, default_cap=config.queue(name).cap, now=now
            )
            counts = counts_by_status(tx, name, ALL_STATUSES)
            items.append(
                QueueItem(
                    name,
                    (
                        clean(name),
                        "-" if cap.cap is None else str(cap.cap),
                        str(counts["starting"] + counts["running"]),
                        str(counts["queued"]),
                        str(counts["held"]),
                    ),
                )
            )
        total = global_capacity(snapshot, now=now)
        totals = (
            f"global: cap={'-' if total.cap is None else total.cap} running={total.running}"
            f" queued={total.queued}"
        )
        jobs: list[dict[str, Any]] = []
        truncated = False
        if queue is not None:
            jobs, truncated = list_jobs(tx, _listing_context(config, queue), stamp)
        if focused is not None and all(j["id"] != focus for j in jobs):
            # a focused job outside the window (an old terminal job) is still shown
            jobs.append(
                job_object(
                    tx, focused, config, env_file_unavailable=_env_unavailable(config), now=now
                )
            )
        shown = [job_item(j) for j in jobs]
        if job_id is not None and all(j.id != job_id for j in shown):
            job_id = None
        detail = None if job_id is None else _detail(tx, config, job_id, now)
    return BrowseData(
        queues=tuple(items),
        queue=queue,
        jobs=tuple(shown),
        truncated=truncated,
        detail_id=job_id if detail is not None else None,
        detail=detail or "",
        totals=totals,
    )


def detail_for(store: Store, config: Config, job_id: str) -> str | None:
    """The detail text of one job (its own short read transaction)."""
    now = format_timestamp(gateway.utc_now())
    with store.read() as tx:
        return _detail(tx, config, job_id, now)
