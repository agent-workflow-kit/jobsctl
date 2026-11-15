"""``jobsctl hold <id>`` and ``jobsctl release <id> [--override-escalation]`` (interface.md,
hold / release, Hold reasons).

The check order is the documented one: usage (already done), lookup (10, 21), state refusals
(20), then the human-only check (13), only when `--override-escalation` takes effect.
"""

from __future__ import annotations

import sys

from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import load_env_file
from jobsctl.job_lookup import find_job
from jobsctl.textsafe import sanitize
from jobsctl.transitions_manual import Applied, Op, hold, release, run_manual
from jobsctl.triggers import request_pass_from_command


def _report(ctx: Context, done: Applied, verb: str) -> int:
    if ctx.json:
        print_json({"changed": done.changed, "dry_run": ctx.args.dry_run, "job": done.job})
    elif not ctx.quiet:
        prefix = "would " if ctx.args.dry_run else ""
        state = verb if done.changed else "already " + done.job["status"]
        sys.stdout.write(f"{prefix}{sanitize(done.job['id'], inline=True)}: {state}\n")
        sys.stdout.flush()
    return 0


@command("hold")
def hold_command(ctx: Context) -> int:
    def body(op: Op) -> Applied:
        return hold(op, find_job(op.tx, ctx.args.id))

    done = run_manual(ctx, body, load_env=lambda: load_env_file(ctx.config.env_file))
    return _report(ctx, done, "held")


@command("release")
def release_command(ctx: Context) -> int:
    def body(op: Op) -> Applied:
        row = find_job(op.tx, ctx.args.id)
        return release(
            op,
            row,
            override=ctx.args.override_escalation,
            require_human=lambda: ctx.require_human("--override-escalation"),
        )

    done = run_manual(ctx, body, load_env=lambda: load_env_file(ctx.config.env_file))
    if done.changed and not ctx.args.dry_run:
        request_pass_from_command(ctx.config, ctx.config_path, quiet=ctx.quiet)
    return _report(ctx, done, "released")
