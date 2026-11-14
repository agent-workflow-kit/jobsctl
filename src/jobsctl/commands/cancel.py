"""``jobsctl cancel (<id> | <selectors> [--all]) [--force] [--wait-timeout DURATION]``
(interface.md, cancel, Selectors).

This module cancels jobs that are not running: `queued`, `held` and `starting` jobs, and the
no-ops. A running job is refused (`live`) without `--force`.
TODO(#20): `cancel --force` of a running job (reconcile first, the committed force request, the
signal sequence, `--wait-timeout`, exit 12 `cancel_pending`).
"""

from __future__ import annotations

import sqlite3
import sys

from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import load_env_file
from jobsctl.errors import ExitCode, JobsctlError, refused
from jobsctl.job_lookup import find_job, select_live_jobs
from jobsctl.textsafe import sanitize
from jobsctl.transitions_manual import (
    TERMINAL,
    Applied,
    Op,
    cancel_held_or_queued_or_starting,
    run_manual,
    unchanged,
)
from jobsctl.triggers import request_pass_from_command


def _force_running(ctx: Context, row: sqlite3.Row) -> Applied:
    """`cancel --force` of a running job: state checks passed, so the human check comes next."""
    ctx.require_human("cancel --force")
    # TODO(#20): commit the force request (audited) and run the signal sequence.
    raise JobsctlError("internal", "cancel --force of a running job is not available yet")


def _cancel_one(ctx: Context, op: Op, row: sqlite3.Row) -> Applied:
    status = row["status"]
    if status in TERMINAL:
        return unchanged(op, row)
    if status == "running":
        if not ctx.args.force:
            raise refused("live", f"{row['id']} is running; cancel --force stops it")
        return _force_running(ctx, row)
    return cancel_held_or_queued_or_starting(op, row)  # --force has no effect on these


def _by_selectors(ctx: Context, op: Op) -> tuple[list[Applied], list[dict[str, str]]]:
    args = ctx.args
    rows = select_live_jobs(
        op.tx, queue=args.queue, labels=args.label or (), unique_key=args.unique_key
    )
    if len(rows) > 1 and not args.all:
        raise JobsctlError(
            "ambiguous",
            f"the selectors match {len(rows)} jobs; narrow them or pass --all",
            candidates=[str(r["id"]) for r in rows],
        )
    done: list[Applied] = []
    refusals: list[dict[str, str]] = []
    for row in rows:
        if row["status"] == "running" and not args.force:
            if not args.all:  # a single match is handled like an id
                raise refused("live", f"{row['id']} is running; cancel --force stops it")
            refusals.append({"job_id": str(row["id"]), "reason": "live"})
            continue
        done.append(_cancel_one(ctx, op, row))
    return done, refusals


def _print(ctx: Context, done: list[Applied], refusals: list[dict[str, str]]) -> None:
    changed = any(d.changed for d in done)
    if ctx.json:
        print_json(
            {
                "changed": changed,
                "dry_run": ctx.args.dry_run,
                "jobs": [d.job for d in done],
                "refused": refusals,
            }
        )
        return
    if ctx.quiet:
        return
    prefix = "would " if ctx.args.dry_run else ""
    for d in done:
        state = "canceled" if d.changed else "already " + d.job["status"]
        sys.stdout.write(f"{prefix}{sanitize(d.job['id'], inline=True)}: {state}\n")
    for item in refusals:
        sys.stderr.write(f"jobsctl: {item['job_id']}: refused ({item['reason']})\n")
    sys.stdout.flush()
    sys.stderr.flush()


@command("cancel")
def cancel_command(ctx: Context) -> int:
    args = ctx.args

    def body(op: Op) -> tuple[list[Applied], list[dict[str, str]]]:
        if args.id is not None:
            return [_cancel_one(ctx, op, find_job(op.tx, args.id))], []
        return _by_selectors(ctx, op)

    done, refusals = run_manual(ctx, body, load_env=lambda: load_env_file(ctx.config.env_file))
    if not args.dry_run and any(d.changed and d.previous_status == "starting" for d in done):
        request_pass_from_command(ctx.config, ctx.config_path, quiet=ctx.quiet)  # a slot is free
    _print(ctx, done, refusals)
    if args.id is None and refusals and not any(d.changed for d in done):
        return int(ExitCode.REFUSED)  # nothing canceled, something refused (selectors, --all)
    return 0
