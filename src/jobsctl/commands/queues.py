"""``jobsctl queues``: every known queue with its settings and counts (interface.md, queues)."""

from __future__ import annotations

import sys
from typing import Any

from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.order import counts_by_status, known_queues
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_schema import ALL_STATUSES


@command("queues")
def queues(ctx: Context) -> int:
    config = ctx.config
    rows: list[dict[str, Any]] = []
    with Store.open(busy_timeout=config.busy_timeout, mode=OpenMode.CLI) as store:
        with store.read() as tx:
            names = known_queues(tx, config)
            seen = {
                str(r["name"]): str(r["first_seen"]) for r in tx.all("SELECT * FROM queues_seen")
            }
            for name in names:
                s = config.queue(name)
                rows.append(
                    {
                        "queue": name,
                        "configured": name in config.queues,
                        "first_seen": seen.get(name),
                        "cap": s.cap,
                        "priority": s.priority,
                        "timeout": s.timeout,
                        "max_retries": s.max_retries,
                        "retry_delay": s.retry_delay,
                        "kill_grace": s.kill_grace,
                        "tail_bytes": s.tail_bytes,
                        "tail_lines": s.tail_lines,
                        "counts": counts_by_status(tx, name, ALL_STATUSES),
                    }
                )
    unmatched = [
        {
            "hook": u.hook,
            "hook_id": config.hooks.entries(u.hook)[u.index].identity,
            "queue": u.queue,
        }
        for u in config.unknown_queue_selectors(names)
    ]
    document = {"queues": rows, "unmatched_selectors": unmatched}
    if ctx.json:
        print_json(document)
    elif not ctx.quiet:
        for r in rows:
            counts = " ".join(f"{k}={v}" for k, v in r["counts"].items() if v)
            sys.stdout.write(f"{r['queue']}: cap={r['cap']} priority={r['priority']} {counts}\n")
    return 0
