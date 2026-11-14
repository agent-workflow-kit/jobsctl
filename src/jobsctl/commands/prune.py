"""``jobsctl prune --older-than DURATION [--dry-run]`` (interface.md, prune)."""

from __future__ import annotations

import os
import sys

from jobsctl import gateway, retention
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir


def _dry_run(ctx: Context, older_than: int) -> list[str]:
    """The same selection with nothing written; a missing or empty database prunes nothing."""
    if not os.path.lexists(resolve_state_dir() / "state.db"):
        return []
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
        if store.schema_version == 0:
            return []
        with store.read() as tx:
            return retention.candidates(tx, gateway.utc_now(), older_than, None)


@command("prune")
def prune(ctx: Context) -> int:
    args = ctx.args
    if args.dry_run:
        pruned = _dry_run(ctx, args.older_than)
    else:
        with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
            now = gateway.utc_now()
            result = retention.prune_jobs(
                store,
                RecordWriter(store),
                older_than=args.older_than,
                trigger="command",
                actor=ctx.actor,
                decision_ids=list(args.decision_id),
                now=now,
            )
            with store.write() as tx:
                retention.forget_queues(tx, set(ctx.config.queues))
            pruned = result.pruned
    if ctx.json:
        print_json({"dry_run": args.dry_run, "pruned": pruned})
    elif not ctx.quiet:
        for job_id in pruned:
            sys.stdout.write(job_id + "\n")
        sys.stdout.flush()
    return 0
