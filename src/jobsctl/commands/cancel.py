"""``jobsctl cancel (<id> | <selectors> [--all]) [--force] [--wait-timeout DURATION]``
(interface.md, cancel, Selectors).

A running job is refused (`live`) without `--force`. With `--force` (human-only) its request is
committed and audited, the verified shim is woken with SIGTERM and the command waits
(`jobsctl.cancel_force`).
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import nullcontext

from jobsctl import cancel_force
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.commands.wait import interrupts_as_keyboard_interrupt
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


def _force_running(
    ctx: Context, op: Op, row: sqlite3.Row, targets: list[cancel_force.ForceTarget]
) -> Applied:
    """`cancel --force` of a running job: state checks passed, so the human check comes next
    (before anything is written), then the Reconcile rules, then the request."""
    ctx.require_human("cancel --force")
    row = cancel_force.apply_reconcile(op, row)
    if row["status"] != "running":  # reconcile finalized it (TODO(#22)): nothing to request
        return unchanged(op, row)
    return cancel_force.request_force(op, row, targets)


def _cancel_one(
    ctx: Context, op: Op, row: sqlite3.Row, targets: list[cancel_force.ForceTarget]
) -> Applied:
    status = row["status"]
    if status in TERMINAL:
        return unchanged(op, row)
    if status == "running":
        if not ctx.args.force:
            raise refused("live", f"{row['id']} is running; cancel --force stops it")
        return _force_running(ctx, op, row, targets)
    return cancel_held_or_queued_or_starting(op, row)  # --force has no effect on these


def _by_selectors(
    ctx: Context, op: Op, targets: list[cancel_force.ForceTarget]
) -> tuple[list[Applied], list[dict[str, str]]]:
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
        done.append(_cancel_one(ctx, op, row, targets))
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
        if d.job["status"] == "running":  # a force request, the job has not ended (yet)
            state = "force requested" if d.changed else "force already requested"
        else:
            state = "canceled" if d.changed else "already " + d.job["status"]
        sys.stdout.write(f"{prefix}{sanitize(d.job['id'], inline=True)}: {state}\n")
    for item in refusals:
        sys.stderr.write(f"jobsctl: {item['job_id']}: refused ({item['reason']})\n")
    sys.stdout.flush()
    sys.stderr.flush()


def _run(
    ctx: Context, targets: list[cancel_force.ForceTarget]
) -> tuple[list[Applied], list[dict[str, str]]]:
    args = ctx.args

    def body(op: Op) -> tuple[list[Applied], list[dict[str, str]]]:
        if args.id is not None:
            return [_cancel_one(ctx, op, find_job(op.tx, args.id), targets)], []
        return _by_selectors(ctx, op, targets)

    return run_manual(ctx, body, load_env=lambda: load_env_file(ctx.config.env_file))


def _deliver_and_wait(
    ctx: Context, done: list[Applied], targets: list[cancel_force.ForceTarget]
) -> list[Applied]:
    """After the commit: wake each requested job's shim and wait (`--wait-timeout 0`: don't)."""
    for target in targets:
        kill = cancel_force.deliver_request(ctx, target)
        wait = ctx.args.wait_timeout
        if wait == 0:
            continue
        cancel_force.wait_until_ended(
            ctx, target, cancel_force.default_timeout(target) if wait is None else wait, kill=kill
        )
    refreshed = {t.job_id for t in targets}
    return [
        Applied(d.changed, cancel_force.fresh_job(ctx, d.job["id"]), d.previous_status)
        if d.job["id"] in refreshed
        else d
        for d in done
    ]


@command("cancel")
def cancel_command(ctx: Context) -> int:
    args = ctx.args
    targets: list[cancel_force.ForceTarget] = []
    refusals: list[dict[str, str]] = []
    done: list[Applied] = []
    try:
        with interrupts_as_keyboard_interrupt() if args.force else nullcontext():
            done, refusals = _run(ctx, targets)
            if targets and not args.dry_run:
                done = _deliver_and_wait(ctx, done, targets)
    except KeyboardInterrupt:
        # the request is committed exactly when its after-commit callback ran
        if targets:
            raise cancel_force.cancel_pending(targets[0].job_id, "was interrupted") from None
        raise JobsctlError("interrupted", "interrupted") from None
    if not args.dry_run and any(d.changed and d.previous_status == "starting" for d in done):
        request_pass_from_command(ctx.config, ctx.config_path, quiet=ctx.quiet)  # a slot is free
    _print(ctx, done, refusals)
    if args.id is None and refusals and not any(d.changed for d in done):
        return int(ExitCode.REFUSED)  # nothing canceled, something refused (selectors, --all)
    return 0
