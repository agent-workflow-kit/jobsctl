"""``jobsctl reconcile [--dry-run]`` (interface.md, `reconcile`): the identity checks, delivery and
retention of a pass without claiming jobs.

It takes the `runner` lease (a live holder: ``already_running``), applies the rules, requests
delivery, runs retention and, at release, spawns a pass if ``pass_requested`` changed. With
``--dry-run`` it takes no lease and writes nothing: a read-only connection evaluates the same
rules and lists the findings.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from jobsctl import gateway, retention
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import disable_dumpable
from jobsctl.hook_retry import stuck_findings
from jobsctl.leases import LEASE_COLUMNS, holder_of
from jobsctl.proc_host import default_host
from jobsctl.reconcile import Reconciler, run_rules
from jobsctl.runner import Runner
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from jobsctl.textsafe import sanitize
from jobsctl.triggers import redeliverable_jobs


def _warn(message: str) -> None:
    sys.stderr.write(f"jobsctl: warning: {sanitize(message, inline=True)}\n")
    sys.stderr.flush()


def _holder(holder: Any) -> dict[str, Any] | None:
    if holder is None or holder.pid is None:
        return None
    return {"pid": holder.pid, "since": holder.since}


def _dry_run(ctx: Context) -> dict[str, Any]:
    out: dict[str, Any] = {
        "already_running": False,
        "holder": None,
        "dry_run": True,
        "findings": [],
    }
    if not os.path.lexists(resolve_state_dir() / "state.db"):
        return out
    host = default_host()
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
        if store.schema_version == 0:
            return out
        holder = holder_of(store.query_one(LEASE_COLUMNS, ["runner"]), host, gateway.utc_now())
        if holder is not None and holder.pid is not None:
            out.update(already_running=True, holder=_holder(holder))
            return out
        reconciler = Reconciler(ctx.config, host, store.paths.shims, warn=_warn)
        applied = run_rules(
            reconciler,
            read=store.read,
            write=store.write,
            clock=gateway.utc_now,
            dry_run=True,
        )
        findings = [a.finding.as_json() for a in applied]
        with store.read() as tx:
            now = gateway.utc_now()
            # The real run spawns `_deliver` (which redelivers) only without a live holder.
            if holder_of(tx.one(LEASE_COLUMNS, ["deliver"]), host, now) is None:
                for job_id in redeliverable_jobs(tx, host, now):
                    findings.append(
                        {"job_id": job_id, "finding": "hooks_redelivered", "action": "redelivered"}
                    )
            findings.extend(stuck_findings(tx))
            if ctx.config.retention is not None:
                for job_id in retention.candidates(
                    tx, gateway.utc_now(), ctx.config.retention, None
                ):
                    findings.append({"job_id": job_id, "finding": "pruned", "action": "deleted"})
        out["findings"] = findings
    return out


def _real(ctx: Context) -> dict[str, Any]:
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        runner = Runner(store, ctx.config, ctx.config_path)
        result = runner.run_reconcile()
        return {
            "already_running": result.already_running,
            "holder": _holder(result.holder) if result.already_running else None,
            "dry_run": False,
            "findings": list(result.reconciled),
        }


@command("reconcile")
def reconcile(ctx: Context) -> int:
    disable_dumpable()
    out = _dry_run(ctx) if ctx.args.dry_run else _real(ctx)
    if ctx.json:
        print_json(out)
    elif not ctx.quiet:
        if out["already_running"]:
            sys.stdout.write(f"already running (pid {out['holder']['pid']})\n")
        prefix = "would: " if out["dry_run"] else ""
        for item in out["findings"]:
            sys.stdout.write(
                f"{prefix}{sanitize(item['job_id'], inline=True)}: "
                f"{item['finding']} ({item['action']})\n"
            )
        sys.stdout.flush()
    return 0
