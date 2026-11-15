"""A `job:<id>` or `deliver` lease held by a short-lived process (docs/interface.md, Leases).

Used by the commands that deliver synchronously (`hold`, `cancel`, `resolve`) and by `_deliver`:
:meth:`Lease.take` never waits (a live holder means "leave the events to it"), :meth:`tick`
renews every 15 seconds and :meth:`release` gives the lease up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from jobsctl import gateway
from jobsctl.errors import JobsctlError
from jobsctl.leases import RENEW_SECONDS, expiry, liveness_probe, own_identity
from jobsctl.proc_host import Host, default_host
from jobsctl.store_db import Store
from jobsctl.store_ops import lease_release, lease_renew, take_lease
from jobsctl.timestamps import format_timestamp


class Lease:
    def __init__(
        self,
        store: Store,
        name: str,
        host: Host,
        ident: str,
        clock: Callable[[], Any],
        monotonic: Callable[[], float],
    ) -> None:
        self._store = store
        self.name = name
        self._host = host
        self._ident = ident
        self._clock = clock
        self._mono = monotonic
        self._last = monotonic()
        self.held = True

    @classmethod
    def take(
        cls,
        store: Store,
        name: str,
        *,
        host: Host | None = None,
        clock: Callable[[], Any] = gateway.utc_now,
        monotonic: Callable[[], float] = gateway.monotonic,
    ) -> Lease | None:
        """The lease, or ``None`` when a live process holds it."""
        host = host or default_host()
        ident = own_identity(host)
        now = clock()
        won = take_lease(
            store,
            name,
            ident,
            now=format_timestamp(now),
            expires_at=expiry(now),
            is_alive=liveness_probe(host),
        )
        return cls(store, name, host, ident, clock, monotonic) if won else None

    def tick(self) -> None:
        """Renew when 15 seconds passed; a lease taken over is no longer ``held``."""
        if not self.held or self._mono() - self._last < RENEW_SECONDS:
            return
        try:
            with self._store.write() as tx:
                renewed = lease_renew(tx, self.name, self._ident, expiry(self._clock()))
        except (JobsctlError, sqlite3.Error):
            return  # a busy database: tried again at the next tick
        self._last = self._mono()
        if not renewed:
            self.held = False

    def release_in(self, tx: Any) -> bool:
        """Release inside the caller's write transaction (`_deliver` pairs it with its counter
        re-check)."""
        released = lease_release(tx, self.name, self._ident)
        self.held = False
        return bool(released)

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            with self._store.write() as tx:
                lease_release(tx, self.name, self._ident)
        except (JobsctlError, sqlite3.Error):
            pass  # it expires
