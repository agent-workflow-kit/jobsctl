"""Start triggers and delivery requests (docs/interface.md, Start triggers and Pass step 5).

A trigger increments the ``pass_requested`` counter and checks the ``runner`` lease in one
transaction, and spawns a detached ``jobsctl run`` only if no live process holds the lease. The
lease holder's final counter comparison and its release are one transaction too
(:mod:`jobsctl.runner`), so requests coalesce and none is lost. ``auto_run: false`` disables every
trigger.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

from jobsctl import gateway
from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.leases import LEASE_COLUMNS, holder_of
from jobsctl.proc_host import Host, default_host
from jobsctl.spawn import spawn_internal
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.store_ops import counter_incr

PASS_REQUESTED = "pass_requested"
DELIVER_REQUESTED = "deliver_requested"


def spawn_pass(
    state_dir: Path, config: Config, config_path: Path | None, *, retried: int = 0
) -> subprocess.Popen[bytes]:
    """A detached triggered ``jobsctl run`` (`retried`: busy re-spawns so far, 0 or 1)."""
    return spawn_internal(
        state_dir, config, config_path, "run", ["--quiet", "--triggered", str(retried)]
    )


def request_pass(
    store: Store,
    config: Config,
    config_path: Path | None,
    *,
    host: Host | None = None,
    now: dt.datetime | None = None,
) -> subprocess.Popen[bytes] | None:
    """Request a pass. Returns the spawned pass (the caller may keep the handle to reap it),
    or `None` when triggers are off or a live lease holder will see the request."""
    if not config.auto_run:
        return None
    host = host or default_host()
    with store.write() as tx:
        counter_incr(tx, PASS_REQUESTED)
        holder = holder_of(tx.one(LEASE_COLUMNS, ["runner"]), host, now or gateway.utc_now())
    if holder is not None:
        return None
    return spawn_pass(store.paths.root, config, config_path)


def request_pass_from_command(
    config: Config, config_path: Path | None, *, quiet: bool = False
) -> None:
    """The start trigger of a CLI command whose own change is already committed. A trigger that
    cannot run (busy database, spawn failure) never fails the command: the committed change
    stands and cron or the next trigger starts the work; a warning goes to stderr."""
    if not config.auto_run:
        return
    try:
        with Store.open(busy_timeout=config.busy_timeout, mode=OpenMode.CLI) as store:
            request_pass(store, config, config_path)
    except (JobsctlError, OSError) as exc:
        if not quiet:
            reason = exc.message if isinstance(exc, JobsctlError) else type(exc).__name__
            sys.stderr.write(f"jobsctl: warning: could not request a pass ({reason})\n")
            sys.stderr.flush()


def redeliverable_jobs(tx: Tx, host: Host, now: dt.datetime) -> list[str]:
    """The jobs with undelivered synchronous events and no live `job:<id>` lease holder
    (`hooks_redelivered`), in id order."""
    jobs = []
    for row in tx.all(
        "SELECT DISTINCT job_id FROM hook_outbox WHERE delivered_at IS NULL AND async = 0 "
        "ORDER BY job_id"
    ):
        lease = tx.one(LEASE_COLUMNS, [f"job:{row['job_id']}"])
        if holder_of(lease, host, now) is None:
            jobs.append(str(row["job_id"]))
    return jobs


def has_undelivered(tx: Tx, host: Host, now: dt.datetime) -> bool:
    """Whether a pass should request delivery: an undelivered `async` event of any job, or an
    undelivered synchronous event of a job with no live `job:<id>` lease holder."""
    if tx.one("SELECT 1 FROM hook_outbox WHERE delivered_at IS NULL AND async = 1 LIMIT 1"):
        return True
    return bool(redeliverable_jobs(tx, host, now))


def request_delivery(
    store: Store,
    config: Config,
    config_path: Path | None,
    *,
    host: Host,
    now: dt.datetime,
) -> subprocess.Popen[bytes] | None:
    """Pass step 5: when there is something to deliver, increment the delivery counter and check
    the ``deliver`` lease in one transaction; spawn ``_deliver`` only without a live holder."""
    with store.write() as tx:
        if not has_undelivered(tx, host, now):
            return None
        counter_incr(tx, DELIVER_REQUESTED)
        holder = holder_of(tx.one(LEASE_COLUMNS, ["deliver"]), host, now)
    if holder is not None:
        return None
    return spawn_internal(store.paths.root, config, config_path, "_deliver")


def request_delivery_quietly(
    store: Store,
    config: Config,
    config_path: Path | None,
    *,
    host: Host | None = None,
    warn: bool = True,
) -> subprocess.Popen[bytes] | None:
    """Request delivery after this process left events for `_deliver` (`async` hooks). A request
    that cannot be made never fails the caller: the next pass requests delivery again."""
    if not config.auto_run:
        return None
    try:
        return request_delivery(
            store, config, config_path, host=host or default_host(), now=gateway.utc_now()
        )
    except (JobsctlError, OSError) as exc:
        if warn:
            reason = exc.message if isinstance(exc, JobsctlError) else type(exc).__name__
            sys.stderr.write(f"jobsctl: warning: could not request delivery ({reason})\n")
            sys.stderr.flush()
        return None
