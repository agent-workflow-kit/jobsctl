"""Identity text, the lease liveness probe, holder reporting and the recorded lease start."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl.leases import (
    Holder,
    expiry,
    format_identity,
    holder_of,
    liveness_probe,
    own_identity,
    parse_identity,
)
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Identity, identify
from jobsctl.store_db import Store
from jobsctl.store_ops import lease_release, lease_since, take_lease
from jobsctl.timestamps import format_timestamp

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state") as s:
        yield s


def test_identity_text_round_trips() -> None:
    ident = Identity("0f3c9a7e-1b2d-4c5e-8f90-123456789abc", 4242, 987654)
    assert parse_identity(format_identity(ident)) == ident


@pytest.mark.parametrize(
    "text",
    ["", "garbage", "a:b:c", "boot:0:1", "boot:1:-1", ":1:1", "boot:1", "boot:99999999999:1"],
)
def test_text_that_is_not_an_identity_is_refused(text: str) -> None:
    assert parse_identity(text) is None


def test_the_probe_tells_alive_dead_and_unknown() -> None:
    host = default_host()
    probe = liveness_probe(host)
    assert probe(own_identity(host)) is True
    me = identify(host, os.getpid())
    assert probe(format_identity(Identity(me.boot_id, me.pid, me.start + 1))) is False  # reused pid
    assert probe(format_identity(Identity("another-boot", me.pid, me.start))) is False
    assert probe("not an identity") is False  # no process can be alive under it


def test_taking_a_lease_records_its_start_and_a_refresh_keeps_it(store: Store) -> None:
    def take(holder: str, minute: int) -> bool:
        now = T0 + dt.timedelta(minutes=minute)
        return take_lease(
            store, "runner", holder, now=format_timestamp(now), expires_at=expiry(now),
            is_alive=lambda _h: False,
        )  # fmt: skip

    assert take("a:1:1", 0) and lease_since(store, "runner") == format_timestamp(T0)
    assert take("a:1:1", 5) and lease_since(store, "runner") == format_timestamp(T0)
    assert take("b:2:2", 9)  # the holder is dead (probe): taken over
    assert lease_since(store, "runner") == format_timestamp(T0 + dt.timedelta(minutes=9))
    with store.write() as tx:
        assert lease_release(tx, "runner", "b:2:2")
    assert lease_since(store, "runner") is None


def test_a_holder_without_a_recorded_start_reports_the_expiry(store: Store) -> None:
    host = default_host()
    with store.write() as tx:
        tx.execute(
            "INSERT INTO leases (name, holder_identity, expires_at) VALUES ('runner', ?, ?)",
            [own_identity(host), expiry(dt.datetime.now(dt.UTC))],
        )
    row = store.query_one(
        "SELECT holder_identity, expires_at, "
        "(SELECT value FROM counters WHERE name = 'lease_since:runner') AS since_ms FROM leases"
    )
    holder = holder_of(row, host, dt.datetime.now(dt.UTC))
    assert isinstance(holder, Holder) and holder.pid == os.getpid()
    assert holder.since == holder.expires_at


def test_stuck_means_five_expiries_without_a_renewal() -> None:
    last_renewed = T0
    holder = Holder("x", 1, "s", expiry(last_renewed))
    assert not holder.stuck(T0 + dt.timedelta(seconds=299))
    assert holder.stuck(T0 + dt.timedelta(seconds=301))
