"""The shim and `_kill` deliver their job's events while holding its lease (Delivery)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import triggers
from jobsctl.config import Config
from jobsctl.store_db import OpenMode, Store
from tests.delivery_helpers import pending
from tests.shim_helpers import JOB, job, make_shim, seed_claimed
from tests.store_helpers import insert_row


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Internal processes a shim would start (passes, `_deliver`) are recorded, not started."""
    seen: list[str] = []

    def fake(state_dir: Any, config: Any, path: Any, command: str, args: Any = ()) -> Any:
        seen.append(command)
        return None

    monkeypatch.setattr(triggers, "spawn_internal", fake)
    return seen


def hooks_config(trace: Path, *names: str, **extra: Any) -> Config:
    cmd = "python3 -c \"import sys,json; print(json.load(sys.stdin)['hook'])\" >> " + str(trace)
    hooks = {name: [{"name": "w", "cmd": cmd, **extra}] for name in names}
    return Config.model_validate({"hooks": hooks})


def put_event(store: Store, job_id: str, hook: str, number: int, *, is_async: bool = False) -> None:
    payload = {
        "tool": "jobsctl", "hook": hook, "event_id": f"{job_id}:{hook}:{number}",
        "object_type": "job", "object_id": job_id, "repo": None, "labels": [],
        "previous_status": "running", "job": {"queue": "q"}, "attempt": None,
    }  # fmt: skip
    with store.write() as tx:
        insert_row(
            tx,
            "hook_outbox",
            {
                "event_id": payload["event_id"], "job_id": job_id, "hook": hook,
                "payload": json.dumps(payload), "async": int(is_async), "created_at": "x",
            },
        )  # fmt: skip


def test_the_post_hooks_run_in_order_after_the_job_ends(store: Store, tmp_path: Path) -> None:
    trace = tmp_path / "trace"
    seed_claimed(store, ["true"])
    cfg = hooks_config(trace, "post-start", "post-finish")
    assert make_shim(store, cfg).run() == 0
    assert trace.read_text().split() == ["post-start", "post-finish"]
    assert pending(store) == []
    assert store.query_one("SELECT 1 FROM leases WHERE name = ?", [f"job:{JOB}"]) is None


def test_earlier_attempts_events_are_delivered_before_the_job_starts(
    store: Store, tmp_path: Path
) -> None:
    trace = tmp_path / "trace"
    job_trace = tmp_path / "job"
    seed_claimed(store, ["sh", "-c", f"cat {trace} > {job_trace}"])
    put_event(store, JOB, "post-fail", 1)
    cfg = hooks_config(trace, "post-fail")
    assert make_shim(store, cfg).run() == 0
    assert job_trace.read_text().split() == ["post-fail"]  # delivered before the job ran


def test_a_canceled_starting_job_gets_its_post_cancel_from_the_shim(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = tmp_path / "trace"
    seed_claimed(store, ["true"])
    shim = make_shim(store, hooks_config(trace, "post-cancel"))
    # the job is canceled after the shim confirmed (its lease is free: no command delivered)
    real_take = shim._take_lease  # pyright: ignore[reportPrivateUsage]

    def cancel_then_take() -> bool:
        with store.write() as tx:
            tx.execute(
                "UPDATE jobs SET status = 'canceled', reason = 'canceled' WHERE id = ?", [JOB]
            )
        put_event(store, JOB, "post-cancel", 1)
        return real_take()

    monkeypatch.setattr(shim, "_take_lease", cancel_then_take)
    assert shim.run() == 0
    assert trace.read_text().split() == ["post-cancel"]
    assert pending(store) == []


def test_a_failed_start_delivers_its_post_fail(store: Store, tmp_path: Path) -> None:
    trace = tmp_path / "trace"
    seed_claimed(store, ["/nonexistent/binary"])
    assert make_shim(store, hooks_config(trace, "post-fail")).run() == 0
    assert job(store)["status"] == "failed"
    assert trace.read_text().split() == ["post-fail"]


def test_async_events_are_left_to_deliver_and_delivery_is_requested(
    store: Store, tmp_path: Path, spawned: list[str]
) -> None:
    trace = tmp_path / "trace"
    seed_claimed(store, ["true"])
    cfg = hooks_config(trace, "post-start", "post-finish", **{"async": True})
    assert make_shim(store, cfg).run() == 0
    assert not trace.exists()  # a lease holder never runs an async hook
    assert len(pending(store, is_async=True)) == 2
    assert "_deliver" in spawned


def test_sigterm_during_a_post_hook_stops_it_and_leaves_the_event(
    store: Store, tmp_path: Path
) -> None:
    started = tmp_path / "started"
    seed_claimed(store, ["true"])
    put_event(store, JOB, "post-finish", 1)
    cmd = f"touch {started}; sleep 30"
    cfg = Config.model_validate({"hooks": {"post-finish": [{"name": "w", "cmd": cmd}]}})
    shim = make_shim(store, cfg)
    shim._lease_held = True  # pyright: ignore[reportPrivateUsage]
    threading.Thread(target=lambda: (_wait_for(started), shim.request_stop())).start()
    shim._deliver_events()  # pyright: ignore[reportPrivateUsage]
    assert len(pending(store)) == 1


def _wait_for(path: Path) -> None:
    for _ in range(500):
        if path.exists():
            return
        time.sleep(0.02)


def test_a_lease_held_elsewhere_is_never_delivered_over(store: Store, tmp_path: Path) -> None:
    """`deliver_sync` stops (and runs nothing) as soon as the lease is not ours any more."""
    from jobsctl.delivery import Deliverer

    trace = tmp_path / "trace"
    seed_claimed(store, ["true"])
    put_event(store, JOB, "post-held", 1)
    put_event(store, JOB, "post-held", 2)
    d = Deliverer(store, hooks_config(trace, "post-held"))
    flags = iter([True, False])  # lost after the first event
    assert d.deliver_sync(JOB, held=lambda: next(flags, False)) is False
    assert len(pending(store)) == 1


def test_kill_that_has_nothing_to_finalize_still_delivers_the_pending_events(
    store: Store, tmp_path: Path
) -> None:
    from jobsctl.kill import Killer

    trace = tmp_path / "trace"
    seed_claimed(store, ["true"])  # `starting`: no force request, nothing to kill
    put_event(store, JOB, "post-cancel", 1)
    cfg = hooks_config(trace, "post-cancel")
    assert Killer(store, cfg, None, JOB).run() == 0
    assert trace.read_text().split() == ["post-cancel"]
    assert pending(store) == []
    assert store.query_one("SELECT 1 FROM leases WHERE name = ?", [f"job:{JOB}"]) is None


def test_kill_leaves_events_to_a_live_lease_holder(store: Store, tmp_path: Path) -> None:
    import subprocess
    import sys

    from jobsctl import gateway
    from jobsctl.kill import Killer
    from jobsctl.leases import expiry, identity_text
    from jobsctl.proc_host import default_host

    trace = tmp_path / "trace"
    seed_claimed(store, ["true"])
    put_event(store, JOB, "post-cancel", 1)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with store.write() as tx:
            tx.execute(
                "INSERT INTO leases (name, holder_identity, expires_at) VALUES (?, ?, ?)",
                [
                    f"job:{JOB}",
                    identity_text(default_host(), holder.pid),
                    expiry(gateway.utc_now()),
                ],
            )
        assert Killer(store, hooks_config(trace, "post-cancel"), None, JOB).run() == 0
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert not trace.exists() and len(pending(store)) == 1


def test_a_config_error_never_delivers_events_with_an_empty_config(
    store: Store, tmp_path: Path
) -> None:
    """With the config unreadable the shim has no hooks to run: delivering would mark the
    earlier attempts' events delivered without running anything, so they stay for `_deliver`."""
    from jobsctl.errors import JobsctlError

    seed_claimed(store, ["true"])
    put_event(store, JOB, "post-fail", 1)
    shim = make_shim(store, config_error=JobsctlError("config_invalid", "bad config"))
    assert shim.run() == 0
    assert job(store)["status"] == "failed"  # spawn_failed
    assert f"{JOB}:post-fail:1" in [r["event_id"] for r in pending(store)]


def test_the_post_hooks_run_after_the_final_commit(store: Store, tmp_path: Path) -> None:
    """A `post-finish` hook sees the job already `done` (the event follows the commit)."""
    seen = tmp_path / "seen"
    db = store.paths.root / "state.db"
    probe = (
        f"import sqlite3; c=sqlite3.connect({str(db)!r}); "
        f"open({str(seen)!r},'a').write(c.execute('SELECT status FROM jobs').fetchone()[0]+'\\n')"
    )
    seed_claimed(store, ["true"])
    cfg = Config.model_validate(
        {"hooks": {"post-finish": [{"name": "w", "cmd": f'python3 -c "{probe}"'}]}}
    )
    assert make_shim(store, cfg).run() == 0
    assert seen.read_text().split() == ["done"]


def test_an_unexpected_delivery_error_never_stops_the_shim(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.delivery import Deliverer

    def boom(self: Deliverer, *args: Any, **kw: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(Deliverer, "deliver_sync", boom)
    seed_claimed(store, ["true"])
    assert make_shim(store).run() == 0
    assert job(store)["status"] == "done"
