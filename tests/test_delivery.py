"""Hook delivery: commands, the outbox and at-least-once semantics (docs/interface.md, Delivery).

Hooks are real `/bin/sh -c` processes that record their stdin; the fake runner tests check
ordering and concurrency without processes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from jobsctl.config import Config
from jobsctl.deliver import Delivery
from jobsctl.delivery import Deliverer
from jobsctl.errors import JobsctlError
from jobsctl.hook_interrupt import InterruptGuard
from jobsctl.hook_run import HookResult
from jobsctl.job_lease import Lease
from jobsctl.shim import StopSignal
from jobsctl.triggers import DELIVER_REQUESTED
from tests.conftest import validate_against
from tests.delivery_helpers import fake_result, open_store, pending, ran, recorder
from tests.helpers import run_cli
from tests.manual_helpers import (
    add_job,
    config_file,
    log_records,
    make_held,
    outbox,
    sql,
)


def hold(capsys: pytest.CaptureFixture[str], cfg: str, job_id: str) -> Any:
    return run_cli(capsys, "--config", cfg, "--json", "hold", job_id)


def test_hold_delivers_its_post_held_event_synchronously(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": recorder(out)}]})
    job_id = add_job(capsys, config=cfg)
    assert hold(capsys, cfg, job_id).code == 0
    (file,) = ran(out)
    payload = json.loads(file.read_text())
    validate_against("hooks/post-held.json", payload)
    assert payload["event_id"] == outbox()[0]["event_id"]
    assert outbox()[0]["delivered_at"] is not None
    (record,) = log_records("hook_ran")
    assert (record["hook"], record["hook_id"], record["outcome"]) == ("post-held", "w", "allow")


def test_a_failing_hook_is_logged_and_never_retried(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cmd = recorder(out) + "; echo boom >&2; exit 3"
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": cmd}]})
    job_id = add_job(capsys, config=cfg)
    assert hold(capsys, cfg, job_id).code == 0  # the command's own outcome is unaffected
    assert outbox()[0]["delivered_at"] is not None
    (record,) = log_records("hook_ran")
    assert (record["outcome"], record["exit"], record["stderr"]) == ("failed", 3, "boom\\n")
    with open_store() as store:
        assert Deliverer(store, Config.model_validate({"hooks": {}})).has_pending(job_id) is False
    assert len(ran(out)) == 1


def test_the_hook_environment_has_no_home_unless_inherited(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    dump = tmp_path / "env.txt"
    cfg = config_file(
        tmp_path,
        env={"inherit": ["PATH"]},
        hooks={"post-held": [{"name": "w", "cmd": f"env > {dump}"}]},
    )
    assert os.environ.get("HOME")
    job_id = add_job(capsys, config=cfg)
    assert hold(capsys, cfg, job_id).code == 0
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines()}
    assert "HOME" not in names
    assert {"JOBSCTL_HOOK", "JOBSCTL_ID", "JOBSCTL_OBJECT_TYPE"} <= names


def test_a_live_lease_holder_keeps_the_events_for_itself(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": recorder(out)}]})
    job_id = add_job(capsys, config=cfg)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        from jobsctl.leases import expiry, identity_text
        from jobsctl.proc_host import default_host
        from jobsctl import gateway

        with open_store() as store:
            with store.write() as tx:
                tx.execute(
                    "INSERT INTO leases (name, holder_identity, expires_at) VALUES (?, ?, ?)",
                    [
                        f"job:{job_id}",
                        identity_text(default_host(), holder.pid),
                        expiry(gateway.utc_now()),
                    ],
                )
        assert hold(capsys, cfg, job_id).code == 0
        assert ran(out) == []
        assert outbox()[0]["delivered_at"] is None
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # the holder died: `_deliver` redelivers, with the same event id
    with open_store() as store:
        assert Delivery(store, Config.model_validate({"hooks": {"post-held": [
            {"name": "w", "cmd": recorder(out)}]}}), None).run() == 0
    (file,) = ran(out)
    assert json.loads(file.read_text())["event_id"] == outbox()[0]["event_id"]
    assert outbox()[0]["delivered_at"] is not None


def test_events_of_one_job_are_delivered_in_outbox_order(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    order = tmp_path / "order.txt"
    cmd = f"python3 -c \"import sys,json; print(json.load(sys.stdin)['hook'])\" >> {order}"
    hooks = {h: [{"name": "w", "cmd": cmd}] for h in ("post-held", "post-cancel")}
    cfg = config_file(tmp_path, hooks=hooks)
    job_id = add_job(capsys, config=cfg)
    assert hold(capsys, cfg, job_id).code == 0
    assert order.read_text().split() == ["post-held"]
    row = outbox()[0]
    second = dict(row["payload"], hook="post-cancel", event_id=f"{job_id}:post-cancel:99")
    sql(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES (?, ?, 'post-cancel', ?, 0, ?)",
        second["event_id"], job_id, json.dumps(second), row["created_at"],
    )
    third = dict(row["payload"], event_id=f"{job_id}:post-held:98")
    sql(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES (?, ?, 'post-held', ?, 0, ?)",
        third["event_id"], job_id, json.dumps(third), row["created_at"],
    )
    with open_store() as store:
        d = Deliverer(store, Config.model_validate({"hooks": hooks}))
        assert d.deliver_sync(job_id) is True
    assert order.read_text().split() == ["post-held", "post-cancel", "post-held"]


def test_an_interrupted_event_stays_in_the_outbox_and_keeps_its_id(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    started = tmp_path / "started"
    cmd = f"touch {started}; sleep 30"
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": cmd}]})
    job_id = add_job(capsys, config=cfg)

    def interrupt() -> None:
        for _ in range(500):
            if started.exists():
                break
            threading.Event().wait(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt)
    thread.start()
    res = hold(capsys, cfg, job_id)
    thread.join()
    assert res.code == 130
    assert res.envelope()["error"]["code"] == "interrupted"
    assert outbox()[0]["delivered_at"] is None
    first_id = outbox()[0]["event_id"]
    # delivered later by `_deliver`, with the same id
    good = {"hooks": {"post-held": [{"name": "w", "cmd": recorder(out)}]}}
    with open_store() as store:
        assert Delivery(store, Config.model_validate(good), None).run() == 0
    assert json.loads(ran(out)[0].read_text())["event_id"] == first_id


def test_a_signal_after_the_hook_ended_still_leaves_the_event_undelivered() -> None:
    guard = InterruptGuard()
    guard.signum = signal.SIGTERM
    with pytest.raises(JobsctlError) as err:
        guard.check(fake_result("post-held"))
    assert err.value.code == "internal"


def test_include_output_puts_the_masked_tail_into_that_entrys_payload_only(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    hooks = [
        {"name": "with", "cmd": recorder(out, "with"), "include_output": True},
        {"name": "without", "cmd": recorder(out, "without")},
    ]
    cfg = config_file(tmp_path, hooks={"post-finish": hooks})
    job_id = add_job(capsys, "--env", "A=b", config=cfg)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = ? WHERE job_id = ?", b"hello \x1b[31mred\x1b[0m\n", job_id)
    res = run_cli(capsys, "--config", cfg, "--json", "resolve", job_id, "--as", "done")
    assert res.code == 0, res.out + res.err
    with_payload = json.loads(ran(out, "with")[0].read_text())
    without_payload = json.loads(ran(out, "without")[0].read_text())
    validate_against("hooks/post-finish.json", with_payload)
    assert with_payload["output_available"] is True
    assert with_payload["output_tail"] == "hello red\n"
    assert without_payload["output_tail"] is None


def test_the_tail_is_masked_again_with_the_delivering_processes_masking_set(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cfg = config_file(
        tmp_path,
        mask_env=["SECRET_TOKEN"],
        env={"inherit": ["PATH", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "SECRET_TOKEN"]},
        hooks={"post-finish": [{"name": "w", "cmd": recorder(out), "include_output": True}]},
    )
    job_id = add_job(capsys, config=cfg)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = ? WHERE job_id = ?", b"token=s3cr3t-value-123 end\n", job_id)
    os.environ["SECRET_TOKEN"] = "s3cr3t-value-123"
    try:
        res = run_cli(capsys, "--config", cfg, "--json", "resolve", job_id, "--as", "done")
    finally:
        del os.environ["SECRET_TOKEN"]
    assert res.code == 0
    payload = json.loads(ran(out)[0].read_text())
    assert "s3cr3t" not in json.dumps(payload) and "***" in payload["output_tail"]


def test_entries_are_selected_at_delivery_with_the_redelivering_config(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": "true", "async": True}]})
    job_id = add_job(capsys, config=cfg)
    assert hold(capsys, cfg, job_id).code == 0
    assert outbox()[0]["async"] == 1
    # the config now has the entry as a synchronous one: an async event runs async entries only
    sync_cfg = Config.model_validate({"hooks": {"post-held": [{"name": "w", "cmd": recorder(out)}]}})
    with open_store() as store:
        assert Deliverer(store, sync_cfg).deliver_sync(job_id) is True
        assert len(pending(store)) == 1
    assert ran(out) == []
    # and with the hook gone nothing runs, the event counts as delivered
    with open_store() as store:
        assert Delivery(store, Config(), None).run() == 0
        assert pending(store) == []


def test_a_damaged_event_is_skipped_and_does_not_block_later_ones(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": recorder(out)}]})
    job_id = add_job(capsys, config=cfg)
    assert hold(capsys, cfg, job_id).code == 0
    good = outbox()[0]["payload"]
    for seq, bad in enumerate(("not json", "[]", json.dumps({**good, "labels": "x"}))):
        sql(
            "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
            "VALUES (?, ?, 'post-held', ?, 0, 'x')",
            f"{job_id}:post-held:bad{seq}", job_id, bad,
        )
    again = {**good, "event_id": f"{job_id}:post-held:77"}
    sql(
        "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
        "VALUES (?, ?, 'post-held', ?, 0, 'x')",
        again["event_id"], job_id, json.dumps(again),
    )
    with open_store() as store:
        d = Deliverer(store, Config.model_validate({"hooks": {"post-held": [
            {"name": "w", "cmd": recorder(out)}]}}))
        assert d.deliver_sync(job_id) is True
        assert pending(store) == []
    assert len(ran(out)) == 2  # the original (already delivered by hold) is not run again


# -- `_deliver`: async hooks, concurrency, the counter ----------------------------------------


def async_events(capsys: pytest.CaptureFixture[str], tmp_path: Path, n: int, per_job: int) -> list[str]:
    cfg = config_file(tmp_path, hooks={"post-held": [{"name": "w", "cmd": "true", "async": True}]})
    ids = []
    for _ in range(n):
        job_id = add_job(capsys, config=cfg)
        assert hold(capsys, cfg, job_id).code == 0
        ids.append(job_id)
        first = outbox()[-1]
        for k in range(per_job - 1):
            extra = {**first["payload"], "event_id": f"{job_id}:post-held:x{k}"}
            sql(
                "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
                "VALUES (?, ?, 'post-held', ?, 1, 'x')",
                extra["event_id"], job_id, json.dumps(extra),
            )
    return ids


class Probe:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.jobs: set[str] = set()
        self.overlap = False
        self.order: list[tuple[str, str]] = []

    def __call__(self, **kw: Any) -> HookResult:
        job = kw["env"]["JOBSCTL_ID"]
        event = json.loads(kw["payload"])["event_id"]
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            if job in self.jobs:
                self.overlap = True
            self.jobs.add(job)
            self.order.append((job, event))
        threading.Event().wait(0.05)
        with self.lock:
            self.live -= 1
            self.jobs.discard(job)
        return fake_result(kw["hook"], kw["hook_id"])


def test_async_hooks_run_at_most_n_at_once_one_per_job_in_order(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ids = async_events(capsys, tmp_path, 5, 3)
    cfg = Config.model_validate(
        {"hooks": {"post-held": [{"name": "w", "cmd": "true", "async": True}]}}
    )
    probe = Probe()
    with open_store() as store:
        d = Deliverer(store, cfg, runner=probe)
        assert Delivery(store, cfg, None, deliverer=d, max_async=3, wait_seconds=0.01).run() == 0
        assert pending(store) == []
        with store.write() as tx:
            assert tx.one("SELECT 1 FROM leases WHERE name = 'deliver'") is None
    assert 1 < probe.peak <= 3 and not probe.overlap
    for job_id in ids:
        seq = [e for j, e in probe.order if j == job_id]
        assert len(seq) == 3 and seq[0].startswith(f"{job_id}:post-held:") and seq[1:] == [
            f"{job_id}:post-held:x0", f"{job_id}:post-held:x1"]


def test_a_request_made_while_delivering_starts_another_round(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    first, second = async_events(capsys, tmp_path, 2, 1)
    sql("UPDATE hook_outbox SET delivered_at = 'x' WHERE job_id = ?", second)
    cfg = Config.model_validate(
        {"hooks": {"post-held": [{"name": "w", "cmd": "true", "async": True}]}}
    )
    calls: list[str] = []

    def runner(**kw: Any) -> HookResult:
        calls.append(kw["env"]["JOBSCTL_ID"])
        if len(calls) == 1:  # a new event and a request arrive during the first hook
            sql("UPDATE hook_outbox SET delivered_at = NULL WHERE job_id = ?", second)
            sql(
                "INSERT INTO counters (name, value) VALUES (?, 1) "
                "ON CONFLICT (name) DO UPDATE SET value = value + 1",
                DELIVER_REQUESTED,
            )
        return fake_result(kw["hook"], kw["hook_id"])

    with open_store() as store:
        d = Deliverer(store, cfg, runner=runner)
        assert Delivery(store, cfg, None, deliverer=d, wait_seconds=0.01).run() == 0
        assert pending(store) == []
    assert sorted(calls) == sorted([first, second])


def test_only_one_deliver_runs_under_the_lease(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    async_events(capsys, tmp_path, 1, 1)
    cfg = Config.model_validate(
        {"hooks": {"post-held": [{"name": "w", "cmd": "true", "async": True}]}}
    )
    with open_store() as store:
        # the test process holds the lease: a second `_deliver` (same process identity is
        # refreshed, so use a foreign live holder instead)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            from jobsctl import gateway
            from jobsctl.leases import expiry, identity_text
            from jobsctl.proc_host import default_host

            with store.write() as tx:
                tx.execute(
                    "INSERT INTO leases (name, holder_identity, expires_at) VALUES (?, ?, ?)",
                    ["deliver", identity_text(default_host(), holder.pid), expiry(gateway.utc_now())],
                )
            probe = Probe()
            d = Deliverer(store, cfg, runner=probe)
            assert Delivery(store, cfg, None, deliverer=d).run() == 0
            assert probe.order == [] and len(pending(store)) == 1
        finally:
            holder.kill()
            holder.wait(timeout=10)


def test_sigterm_stops_running_hooks_and_leaves_their_events(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    async_events(capsys, tmp_path, 1, 1)
    cfg = Config.model_validate(
        {"hooks": {"post-held": [{"name": "w", "cmd": "sleep 30", "async": True}]}}
    )
    stop = StopSignal()
    threading.Timer(0.5, stop.set).start()
    with open_store() as store:
        assert Delivery(store, cfg, None, stop=stop, wait_seconds=0.05).run() == 0
        assert len(pending(store)) == 1
        with store.write() as tx:
            assert tx.one("SELECT 1 FROM leases WHERE name = 'deliver'") is None


def test_a_dead_deliver_mid_hook_leaves_the_event_for_the_next_one(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    async_events(capsys, tmp_path, 1, 1)
    cfg = Config.model_validate(
        {"hooks": {"post-held": [{"name": "w", "cmd": "true", "async": True}]}}
    )

    def dies(**kw: Any) -> HookResult:
        raise KeyboardInterrupt  # the process is gone before the event is marked

    with open_store() as store:
        d = Deliverer(store, cfg, runner=dies)
        with pytest.raises(KeyboardInterrupt):
            Delivery(store, cfg, None, deliverer=d, wait_seconds=0.01).run()
        assert len(pending(store)) == 1
