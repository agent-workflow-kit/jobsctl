"""``jobsctl prune --older-than DURATION [--dry-run]`` (interface.md, prune)."""

from __future__ import annotations

import os
import sys

from jobsctl import gateway, retention
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.errors import JobsctlError
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
    audit_error: Exception | None = None
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
            retention.sweep_orphans(store)
            pruned = result.pruned
            audit_error = result.audit_error
    if ctx.json:
        print_json({"dry_run": args.dry_run, "pruned": pruned})
    elif not ctx.quiet:
        for job_id in pruned:
            sys.stdout.write(job_id + "\n")
        sys.stdout.flush()
    if audit_error is not None:
        # The deletions stand and the ids are reserved; only the audit record is missing.
        if isinstance(audit_error, JobsctlError):
            raise audit_error
        raise JobsctlError(
            "internal", f"jobs were deleted but the audit record failed: {audit_error}"
        ) from audit_error
    return 0
