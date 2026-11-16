"""``cron install [--dry-run]`` and ``cron uninstall`` (interface.md, cron install)."""

from __future__ import annotations

import os
import sys

from jobsctl import cron
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.textsafe import sanitize


def _report(ctx: Context, result: cron.CronResult, *, removed: bool = False) -> int:
    if ctx.json:
        print_json(result.document())
        return 0
    for warning in result.warnings:
        sys.stderr.write(f"jobsctl: warning: {sanitize(warning['message'], inline=True)}\n")
    if not ctx.quiet:
        if result.block is not None:
            for line in result.block.split("\n"):
                sys.stdout.write(sanitize(line, inline=True) + "\n")
        elif removed:
            sys.stdout.write(
                "removed the jobsctl block\n" if result.changed else "no jobsctl block\n"
            )
        sys.stdout.flush()
    return 0


@command("cron install")
def cron_install(ctx: Context) -> int:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    result = cron.install(
        dry_run=ctx.args.dry_run,
        environ=os.environ,
        argv0=cron.main_argv0(),
        home=home,
    )
    return _report(ctx, result)


@command("cron uninstall")
def cron_uninstall(ctx: Context) -> int:
    return _report(ctx, cron.uninstall(), removed=True)
