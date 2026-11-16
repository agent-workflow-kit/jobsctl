"""``jobsctl list`` (interface.md, list).

One read transaction: a consistent snapshot of the rows, their ranks and their labels. Order
is decided in SQL: queue name, then queued jobs by place (pinned, priority, position, id), then
`starting`/`running`/`held` by `enqueued_at` and id, then terminal jobs by `ended_at` (newest
first) and id.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from typing import Any

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import load_env_file
from jobsctl.job_text import job_line
from jobsctl.job_view import job_object
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.store_schema import TERMINAL_STATUSES
from jobsctl.timestamps import format_timestamp

VIEW_WINDOW = dt.timedelta(hours=24)  # terminal jobs that ended longer ago are not listed

_GROUP = (
    "CASE WHEN status = 'queued' THEN 0 "
    "WHEN status IN ('starting', 'running', 'held') THEN 1 ELSE 2 END"
)


def _query(args: Any, now: dt.datetime) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if args.queue is not None:
        where.append("queue = ?")
        params.append(args.queue)
    if args.status:
        where.append(f"status IN ({', '.join('?' for _ in args.status)})")
        params.extend(args.status)
    for key, value in args.label or []:
        where.append(
            "EXISTS (SELECT 1 FROM job_labels l WHERE l.job_id = jobs.id AND l.key = ? "
            "AND l.value = ?)"
        )
        params.extend([key, value])
    if args.unique_key is not None:
        where.append("unique_key = ?")
        params.append(args.unique_key)
    if not args.all and not args.status:
        done = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)
        where.append(f"(status NOT IN ({done}) OR ended_at IS NULL OR ended_at >= ?)")
        params.append(format_timestamp(now - VIEW_WINDOW))
    sql = (
        f"SELECT * FROM (SELECT jobs.*, {_GROUP} AS grp FROM jobs"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + ") ORDER BY queue, grp,"
        " CASE WHEN grp = 0 THEN -pinned END, CASE WHEN grp = 0 THEN -priority END,"
        " CASE WHEN grp = 0 THEN position WHEN grp = 1 THEN enqueued_at END,"
        " CASE WHEN grp = 2 THEN ended_at END DESC, id"
    )
    if args.limit is not None:
        sql += " LIMIT ?"
        params.append(args.limit + 1)  # one more than asked: tells whether the list was cut
    return sql, params


def list_jobs(tx: Tx, ctx: Context, now: dt.datetime) -> tuple[list[dict[str, Any]], bool]:
    sql, params = _query(ctx.args, now)
    rows: list[sqlite3.Row] = tx.all(sql, params)
    limit = ctx.args.limit
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]
    unavailable = False
    if any(r["pass_env"] != "[]" for r in rows):
        unavailable = load_env_file(ctx.config.env_file).status == "unavailable"
    stamp = format_timestamp(now)
    jobs = [
        job_object(tx, r, ctx.config, env_file_unavailable=unavailable, now=stamp) for r in rows
    ]
    return jobs, truncated


@command("list")
def list_command(ctx: Context) -> int:
    now = gateway.utc_now()
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        with store.read() as tx:
            jobs, truncated = list_jobs(tx, ctx, now)
    if ctx.json:
        print_json({"jobs": jobs, "truncated": truncated})
    else:
        sys.stdout.write("".join(job_line(j) + "\n" for j in jobs))
        sys.stdout.flush()
    return 0
