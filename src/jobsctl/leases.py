"""Process identities as stored text, and the lease helpers the runner and triggers share.

A lease's ``holder_identity`` and an attempt's ``claimed_by`` and ``shim_identity`` hold
``<boot_id>:<pid>:<start>`` (docs/interface.md, Process identity). The liveness check behind
``take_lease`` is :func:`liveness_probe`: alive, dead (a different boot id included) or
inconclusive, which the lease rules turn into "taken only after expiry".
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass

from jobsctl.proc_host import Host
from jobsctl.proc_identity import Identity, Liveness, check_identity, identify
from jobsctl.store_db import Store
from jobsctl.store_ops import LEASE_TTL_SECONDS, IsAlive
from jobsctl.timestamps import format_timestamp, parse_timestamp

RENEW_SECONDS = 15
STUCK_FACTOR = 5  # a live holder that has not renewed for 5 x the expiry is `runner_stuck`


def format_identity(ident: Identity) -> str:
    return f"{ident.boot_id}:{ident.pid}:{ident.start}"


def parse_identity(text: str) -> Identity | None:
    """The identity in `text`, or `None` when it is not one."""
    boot, sep, rest = text.rpartition(":")
    boot, sep2, pid = boot.rpartition(":")
    if not (sep and sep2 and boot):
        return None
    try:
        return Identity(boot, int(pid), int(rest))
    except ValueError:
        return None


def identity_text(host: Host, pid: int) -> str:
    """The stored identity of the process now holding `pid`."""
    return format_identity(identify(host, pid))


def own_identity(host: Host) -> str:
    return identity_text(host, os.getpid())


def liveness_probe(host: Host) -> IsAlive:
    """The ``is_alive`` callable of ``take_lease`` over `host`."""

    def probe(holder: str) -> bool | None:
        ident = parse_identity(holder)
        if ident is None:
            return None  # not an identity this code wrote: decided by expiry
        state = check_identity(host, ident).state
        if state is Liveness.ALIVE:
            return True
        return False if state is Liveness.DEAD else None

    return probe


def expiry(now: dt.datetime) -> str:
    return format_timestamp(now + dt.timedelta(seconds=LEASE_TTL_SECONDS))


@dataclass(frozen=True)
class Holder:
    identity: str
    pid: int | None
    since: str  # when the holder took the lease
    expires_at: str

    def stuck(self, now: dt.datetime) -> bool:
        """Alive but not renewed for 5 x the expiry (renewals come every 15 s, so the last one
        was ``expires_at`` minus the TTL)."""
        limit = now - dt.timedelta(seconds=STUCK_FACTOR * LEASE_TTL_SECONDS)
        return format_timestamp(limit) > _last_renewal(self.expires_at)


def _last_renewal(expires_at: str) -> str:
    return format_timestamp(parse_timestamp(expires_at) - dt.timedelta(seconds=LEASE_TTL_SECONDS))


LEASE_COLUMNS = "SELECT holder_identity, expires_at, since FROM leases WHERE name = ?"


def live_holder(store: Store, name: str, host: Host, now: dt.datetime) -> Holder | None:
    """The holder of lease `name` when it counts as held: alive, or inconclusive and not yet
    expired. `None` when the lease is free (absent, dead holder, or expired and inconclusive)."""
    return holder_of(store.query_one(LEASE_COLUMNS, [name]), host, now)


def holder_of(row: sqlite3.Row | None, host: Host, now: dt.datetime) -> Holder | None:
    if row is None:
        return None
    ident_text, expires_at, since = row["holder_identity"], row["expires_at"], row["since"]
    alive = liveness_probe(host)(str(ident_text))
    if alive is False:
        return None
    if alive is None and str(expires_at) <= format_timestamp(now):
        return None
    ident = parse_identity(str(ident_text))
    return Holder(
        str(ident_text),
        None if ident is None else ident.pid,
        str(since or expires_at),
        str(expires_at),
    )
