"""``jobsctl retry`` and ``jobsctl resolve`` (interface.md, `retry`, `resolve`).

Neither has a human-only flag. A `retry` that changed something requests a pass after the commit
(a job returning to `queued` can start at once), like `release`.
"""

from __future__ import annotations

from jobsctl.cli_registry import Context, command
from jobsctl.commands.hold_release import _report  # pyright: ignore[reportPrivateUsage]
from jobsctl.env_file import load_env_file
from jobsctl.job_lookup import find_job
from jobsctl.note_file import read_note
from jobsctl.transitions_manual import Applied, Op, resolve, retry, run_manual
from jobsctl.triggers import request_pass_from_command


@command("retry")
def retry_command(ctx: Context) -> int:
    args = ctx.args
    flags: dict[str, object] = {}
    if args.timeout is not None:
        flags["timeout"] = args.timeout
    if args.max_retries is not None:
        flags["max_retries"] = args.max_retries
    if args.kill_grace is not None:
        flags["kill_grace"] = args.kill_grace

    def body(op: Op) -> Applied:
        return retry(op, find_job(op.tx, args.id), top=args.top, attempt=args.attempt, flags=flags)

    done = run_manual(ctx, body, load_env=lambda: load_env_file(ctx.config.env_file))
    if done.changed and not args.dry_run:
        request_pass_from_command(ctx.config, ctx.config_path, quiet=ctx.quiet)
    return _report(ctx, done, "queued")


@command("resolve")
def resolve_command(ctx: Context) -> int:
    args = ctx.args
    note = None if args.note_file is None else read_note(args.note_file)  # usage errors first

    def body(op: Op) -> Applied:
        return resolve(op, find_job(op.tx, args.id), as_=args.as_, note=note)

    done = run_manual(ctx, body, load_env=lambda: load_env_file(ctx.config.env_file))
    return _report(ctx, done, "resolved")
