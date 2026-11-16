"""No lost delivery trigger: a request that lands while `_deliver` is finishing is never dropped
(docs/interface.md, Pass step 5: the counter re-check and the lease release are one transaction).

`triggers.spawn_internal` is replaced, so no process is started: a spawned `_deliver` is a thread
running `Delivery.run` on its own connection, with a fake hook runner.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Any

import pytest

from jobsctl import deliver, triggers
from jobsctl.config import Config
from jobsctl.deliver import Delivery
from jobsctl.delivery import Deliverer
from jobsctl.proc_host import default_host
from jobsctl.store_db import OpenMode, Store
from tests.delivery_helpers import fake_result, open_store, pending
from tests.manual_helpers import outbox, sql
from tests.test_delivery import async_events

CFG = Config.model_validate(
    {"auto_run": False, "hooks": {"post-held": [{"name": "w", "cmd": "true", "async": True}]}}
)
INSERT = (
    "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
    "VALUES (?, ?, 'post-held', ?, 1, '2026-01-01T00:00:00.000Z')"
)


def _runner(**kw: Any) -> Any:
    return fake_result(kw["hook"], kw["hook_id"])


def _add_event(store: Store, job_id: str, base: dict[str, Any], event_id: str) -> None:
    payload = {**base, "event_id": event_id}
    with store.write() as tx:
        tx.execute(INSERT, [event_id, job_id, json.dumps(payload)])


def test_a_request_between_the_last_scan_and_the_release_starts_another_round(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (job_id,) = async_events(capsys, tmp_path, 1, 1)
    base = outbox()[0]["payload"]
    sql("UPDATE hook_outbox SET delivered_at = '2000-01-01T00:00:00.000Z'")
    spawned: list[str] = []
    monkeypatch.setattr(triggers, "spawn_internal", lambda *a, **k: spawned.append("x"))

    def late_request() -> None:
        with Store.open(busy_timeout=10.0, mode=OpenMode.CLI) as other:
            _add_event(other, job_id, base, "late")
            triggers.request_delivery(
                other, CFG, None, host=default_host(), now=triggers.gateway.utc_now()
            )

    real_round = deliver.Delivery._round  # pyright: ignore[reportPrivateUsage]
    fired: list[int] = []

    def round_then_request(self: Delivery) -> None:
        real_round(self)
        if not fired:  # after the first round's last scan, before the lease is released
            fired.append(1)
            thread = threading.Thread(target=late_request)
            thread.start()
            thread.join(timeout=30)

    monkeypatch.setattr(deliver.Delivery, "_round", round_then_request)
    with open_store() as store:
        d = Deliverer(store, CFG, runner=_runner)
        assert Delivery(store, CFG, None, deliverer=d, wait_seconds=0.01).run() == 0
        assert pending(store) == []
        with store.write() as tx:
            assert tx.one("SELECT 1 FROM leases WHERE name = 'deliver'") is None
    assert spawned == []  # the live holder saw the request itself


def test_concurrent_requesters_and_releasing_deliverers_lose_no_event(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (job_id,) = async_events(capsys, tmp_path, 1, 1)
    base = outbox()[0]["payload"]
    sql("UPDATE hook_outbox SET delivered_at = '2000-01-01T00:00:00.000Z'")
    rng = random.Random(7)
    jitter = threading.Lock()

    def nap(top: float) -> None:
        with jitter:
            delay = rng.random() * top
        threading.Event().wait(delay)

    real_round = deliver.Delivery._round  # pyright: ignore[reportPrivateUsage]

    def slow_round(self: Delivery) -> None:
        real_round(self)
        nap(0.01)  # widens the window between the last scan and the release

    monkeypatch.setattr(deliver.Delivery, "_round", slow_round)
    threads: list[threading.Thread] = []
    guard = threading.Lock()

    def run_deliverer() -> None:
        with Store.open(busy_timeout=30.0, mode=OpenMode.CLI) as own:
            d = Deliverer(own, CFG, runner=_runner)
            Delivery(own, CFG, None, deliverer=d, wait_seconds=0.01, diag=lambda m: None).run()

    def fake_spawn(*args: Any, **kw: Any) -> None:
        thread = threading.Thread(target=run_deliverer)
        with guard:
            threads.append(thread)
        thread.start()

    monkeypatch.setattr(triggers, "spawn_internal", fake_spawn)
    errors: list[BaseException] = []

    def producer(k: int) -> None:
        try:
            with Store.open(busy_timeout=30.0, mode=OpenMode.CLI) as own:
                for i in range(8):
                    _add_event(own, job_id, base, f"race-{k}-{i}")
                    triggers.request_delivery(
                        own, CFG, None, host=default_host(), now=triggers.gateway.utc_now()
                    )
                    nap(0.004)
        except BaseException as exc:  # noqa: BLE001 - reported by the test thread
            errors.append(exc)

    producers = [threading.Thread(target=producer, args=(k,)) for k in range(3)]
    for t in producers:
        t.start()
    for t in producers:
        t.join(timeout=60)
    with guard:
        started = list(threads)
    for t in started:
        t.join(timeout=60)  # bounded: a deliverer that never ends fails the check below
    assert started and not errors
    assert not any(t.is_alive() for t in [*producers, *started])
    with open_store() as store:
        assert pending(store) == []  # nothing was left behind by a lost trigger
        delivered = store.query_one(
            "SELECT COUNT(*) AS n FROM hook_outbox WHERE delivered_at NOT LIKE '2000%'"
        )
        assert delivered is not None and delivered["n"] == 24
