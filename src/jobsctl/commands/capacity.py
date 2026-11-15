"""``jobsctl capacity [--queue Q]`` (interface.md, Capacity)."""

from __future__ import annotations

import sys
from typing import Any

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.order import (
    global_capacity,
    known_queues,
    load_snapshot,
    queue_capacity,
)
from jobsctl.store_db import OpenMode, Store
from jobsctl.timestamps import format_timestamp


def _row(c: Any) -> dict[str, Any]:
    return {
        "queue": c.queue,
        "known": c.known,
        "cap": c.cap,
        "running": c.running,
        "queued": c.queued,
        "free": c.free,
        "effective_free": c.effective_free,
    }


@command("capacity")
def capacity(ctx: Context) -> int:
    config = ctx.config
    now = format_timestamp(gateway.utc_now())
    with Store.open(busy_timeout=config.busy_timeout, mode=OpenMode.CLI) as store:
        with store.read() as tx:
            snapshot = load_snapshot(tx, config)
            known = known_queues(tx, config)
    wanted = [ctx.args.queue] if ctx.args.queue else known
    queues = [
        _row(
            queue_capacity(
                snapshot, name, known=name in known, default_cap=config.queue(name).cap, now=now
            )
        )
        for name in wanted
    ]
    g = global_capacity(snapshot, now=now)
    document = {
        "queues": queues,
        "global": {"cap": g.cap, "running": g.running, "queued": g.queued, "free": g.free},
    }
    if ctx.json:
        print_json(document)
    elif not ctx.quiet:
        for q in queues:
            sys.stdout.write(
                f"{q['queue']}: cap={q['cap']} running={q['running']} queued={q['queued']} "
                f"free={q['free']} effective_free={q['effective_free']}\n"
            )
        sys.stdout.write(
            f"global: cap={g.cap} running={g.running} queued={g.queued} free={g.free}\n"
        )
    return 0
