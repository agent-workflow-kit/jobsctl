"""Synchronous delivery by a CLI command (docs/interface.md, Delivery; `hold`, `cancel`,
`resolve`).

After its transition is committed, a command takes the `job:<id>` lease **only if it is free**
(a live holder delivers the events itself), delivers the job's synchronous events inside an
:class:`InterruptGuard` and releases the lease. SIGINT, SIGHUP and SIGTERM stop a running hook,
leave its event undelivered (`_deliver` delivers it later) and end the command with `interrupted`
(130) or `internal` (1). `async` events are left to `_deliver`, which the command requests.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from jobsctl.cli_registry import Context
from jobsctl.config import Config
from jobsctl.delivery import Deliverer
from jobsctl.errors import JobsctlError
from jobsctl.hook_interrupt import InterruptGuard
from jobsctl.job_lease import Lease
from jobsctl.store_db import OpenMode, Store
from jobsctl.triggers import request_delivery_quietly


def deliver_after_commit(ctx: Context, job_ids: Iterable[str]) -> None:
    """Deliver the events the command's transitions left for ``job_ids``. Raises the
    `interrupted`/`internal` error of a signal received while hooks ran; any other failure to
    deliver is a warning (the change is committed; `_deliver` or the next holder delivers)."""
    ids = list(dict.fromkeys(job_ids))
    if not ids or ctx.args.dry_run:
        return
    try:
        with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
            _deliver_jobs(store, ctx.config, ctx.config_path, ids, quiet=ctx.quiet)
    except JobsctlError as exc:
        if exc.code in ("interrupted", "internal"):
            raise
        _warn(ctx.quiet, exc.message)
    except OSError as exc:
        _warn(ctx.quiet, type(exc).__name__)


def _deliver_jobs(
    store: Store, config: Config, config_path: Path | None, ids: list[str], *, quiet: bool
) -> None:
    deliverer = Deliverer(store, config)
    leftover_async = False
    with InterruptGuard() as guard:
        for job_id in ids:
            if deliverer.has_pending(job_id, is_async=False):
                lease = Lease.take(store, f"job:{job_id}")
                if lease is not None:  # else a live holder delivers
                    try:
                        deliverer.deliver_sync(
                            job_id,
                            stop=guard,
                            check=guard.check,
                            held=lambda lease=lease: lease.held,
                            tick=lease.tick,
                        )
                    finally:
                        lease.release()
                guard.check()
            leftover_async = leftover_async or deliverer.has_pending(job_id, is_async=True)
    if leftover_async:
        request_delivery_quietly(store, config, config_path, warn=not quiet)


def _warn(quiet: bool, reason: str) -> None:
    if not quiet:
        sys.stderr.write(
            f"jobsctl: warning: the change is committed but hook delivery failed ({reason})\n"
        )
        sys.stderr.flush()
