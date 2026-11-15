"""``jobsctl move``, ``pin``, ``unpin`` and ``priority`` (interface.md, Order within a queue).

Each is one `BEGIN IMMEDIATE` transaction that resolves the ids, checks the states (20), applies
the change and builds the job object; the `reorder` log record is appended after the commit.
`--dry-run` runs the same decisions in a read-only transaction on a read-only connection and
writes nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jobsctl import gateway
from jobsctl.checks import run_checks
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import load_env_file
from jobsctl.errors import JobsctlError, refused
from jobsctl.ids import resolve_job_id
from jobsctl.job_view import JobRow, job_object, job_row
from jobsctl.log_writer import RecordWriter
from jobsctl.order import (
    LIVE,
    Placement,
    apply_placement,
    plan_move_edge,
    plan_move_relative,
    projected_rank,
)
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.store_dir import resolve_state_dir
from jobsctl.timestamps import format_timestamp

# What a change decides: `None` placement and no field updates mean "nothing to change".
Change = tuple[Placement | None, dict[str, Any]]


@dataclass
class Outcome:
    changed: bool
    job: dict[str, Any]
    queue: str
    job_id: str
    details: dict[str, Any]


def _resolve(tx: Tx, text: str) -> str:
    return resolve_job_id(
        text,
        lambda prefix: [
            str(r["id"])
            for r in tx.all("SELECT id FROM jobs WHERE substr(id, 1, ?) = ?", [len(prefix), prefix])
        ],
    )


def _require_live(row: sqlite3.Row, what: str) -> None:
    if row["status"] not in LIVE:
        raise refused("not_queued", f"{what} {row['id']} is {row['status']}, not queued or held")


def _fetch(tx: Tx, job_id: str) -> sqlite3.Row:
    row = job_row(tx, job_id)
    assert row is not None
    return row


def _decide_move(
    tx: Tx, args: Any, write: bool, id_text: str
) -> tuple[str, Callable[[], Placement | None], dict[str, Any]]:
    """Resolve and check a `move`; returns the job id, a planner and the log details."""
    job_id = _resolve(tx, id_text)
    other_text = args.before or args.after
    other_id = _resolve(tx, other_text) if other_text else None

    def check() -> None:
        row = _fetch(tx, job_id)
        _require_live(row, "job")
        if other_id is not None:
            other = _fetch(tx, other_id)
            _require_live(other, "job")
            if other["queue"] != row["queue"]:
                raise refused(
                    "other_queue", f"{other_id} is in queue {other['queue']}, not {row['queue']}"
                )

    run_checks(state=check)
    if other_id is not None:
        if other_id == job_id:
            return job_id, lambda: None, {"op": "move"}
        before = args.before is not None
        return (
            job_id,
            lambda: plan_move_relative(
                tx, job_id, other_id, before=before, overlay=None if write else {}
            ),
            {"op": "move", "to": "before" if before else "after", "other": other_id},
        )
    top = bool(args.top)
    return (
        job_id,
        lambda: plan_move_edge(tx, job_id, top=top, overlay=None if write else {}),
        {"op": "move", "to": "top" if top else "bottom"},
    )


def _decide_flag(
    tx: Tx, command_key: str, args: Any, id_text: str
) -> tuple[str, Callable[[], Placement | None], dict[str, Any]]:
    """`pin`, `unpin` and `priority` keep the position key and change only the band."""
    job_id = _resolve(tx, id_text)
    row = _fetch(tx, job_id)
    _require_live(row, "job")
    if command_key == "priority":
        new_pinned, new_priority = bool(row["pinned"]), int(args.value)
        details: dict[str, Any] = {"op": "priority", "priority": new_priority}
    else:
        new_pinned, new_priority = command_key == "pin", int(row["priority"])
        details = {"op": command_key}
    same = (bool(row["pinned"]), int(row["priority"])) == (new_pinned, new_priority)
    return (
        job_id,
        lambda: None if same else Placement(str(row["position"]), new_pinned, new_priority),
        details,
    )


def _run(ctx: Context, tx: Tx, write: bool) -> Outcome:
    args = ctx.args
    key = ctx.command
    if key == "move":
        job_id, plan, details = _decide_move(tx, args, write, args.id)
    else:
        job_id, plan, details = _decide_flag(tx, key, args, args.id)
    row: JobRow = _fetch(tx, job_id)
    placement = plan()
    changed = placement is not None
    if placement is not None:
        if write:
            apply_placement(tx, job_id, placement)
            row = _fetch(tx, job_id)
        else:  # the projection: a plain dict with the new values, ranked among the others
            projected = dict(row)
            projected.update(
                position=placement.position,
                pinned=int(placement.pinned),
                priority=placement.priority,
            )
            row = projected
    now = format_timestamp(gateway.utc_now())
    unavailable = False
    if row["pass_env"] != "[]":
        unavailable = load_env_file(ctx.config.env_file).status == "unavailable"
    job = job_object(tx, row, ctx.config, env_file_unavailable=unavailable, now=now)
    if placement is not None and placement.renumbered and job["rank"] is not None:
        job["rank"] = projected_rank(tx, row, placement.renumbered)  # dry run, queue renumbered
    return Outcome(changed, job, str(row["queue"]), job_id, details)


def _dry_run(ctx: Context) -> Outcome:
    db = resolve_state_dir() / "state.db"
    if os.path.lexists(db):
        with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
            if store.schema_version > 0:
                with store.read() as tx:
                    return _run(ctx, tx, write=False)
    raise JobsctlError("not_found", f"no job matches '{ctx.args.id}'")


def _write(ctx: Context) -> Outcome:
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        writer = RecordWriter(store)
        with store.write() as tx:
            out = _run(ctx, tx, write=True)
            if out.changed:

                def log() -> None:
                    try:
                        writer.log(
                            "reorder",
                            fields={
                                "job_id": out.job_id,
                                "queue": out.queue,
                                "actor": ctx.actor,
                                "decision_ids": list(ctx.args.decision_id),
                                "message": f"{out.details['op']} {out.job_id}",
                                "details": out.details,
                            },
                        )
                    except Exception as exc:  # the change is committed: do not fail the command
                        sys.stderr.write(
                            f"jobsctl: warning: the change was stored but appending to "
                            f"log.jsonl failed ({type(exc).__name__})\n"
                        )

                tx.after_commit(log)
        return out


def _reorder(ctx: Context) -> int:
    out = _dry_run(ctx) if ctx.args.dry_run else _write(ctx)
    if ctx.json:
        print_json({"changed": out.changed, "dry_run": bool(ctx.args.dry_run), "job": out.job})
    elif not ctx.quiet:
        sys.stdout.write(f"{out.job_id}{'' if out.changed else ' (unchanged)'}\n")
    return 0


for _key in ("move", "pin", "unpin", "priority"):
    command(_key)(_reorder)
