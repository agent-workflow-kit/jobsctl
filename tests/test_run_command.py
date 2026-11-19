"""`jobsctl run` and `run --loop` through the command line (interface.md, run)."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from jobsctl import cli as cli_module
from jobsctl import gateway
from jobsctl.commands import run as run_module
from jobsctl.errors import JobsctlError
from jobsctl.leases import expiry
from jobsctl.runner import Runner
from jobsctl.store_db import Store
from jobsctl.store_ops import take_lease
from jobsctl.timestamps import format_timestamp
from tests.conftest import validate_against
from tests.helpers import run_cli
from tests.manual_helpers import add_job, config_file, row


@pytest.fixture(autouse=True)
def no_diag_redirect(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """`install` would replace this process's stdout; record the path it was given instead."""
    paths: list[Path] = []
    monkeypatch.setattr(cli_module, "install", lambda path: paths.append(path))
    return paths


def lines(out: str) -> list[dict[str, Any]]:
    docs = [json.loads(line) for line in out.splitlines()]
    for doc in docs:
        validate_against("cli/run.json", doc)
    return docs


def test_an_empty_pass_prints_the_documented_object(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "run")
    assert res.code == 0
    assert lines(res.out) == [
        {"already_running": False, "holder": None, "started": [], "reconciled": [], "warnings": []}
    ]


def test_a_pass_claims_up_to_the_cap_and_lists_the_started_jobs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, queues={"default": {"cap": 2}}, auto_run=False)
    ids = [add_job(capsys, config=cfg) for _ in range(3)]
    res = run_cli(capsys, "--config", cfg, "--json", "run")
    (doc,) = lines(res.out)
    assert len(doc["started"]) == 2 and set(doc["started"]) <= set(ids)
    assert sorted(row(i)["status"] for i in ids) == ["queued", "starting", "starting"]


def test_max_starts_limits_the_claims(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = config_file(tmp_path, default_cap=None, auto_run=False)
    for _ in range(3):
        add_job(capsys, config=cfg)
    (doc,) = lines(run_cli(capsys, "--config", cfg, "--json", "run", "--max-starts", "1").out)
    assert len(doc["started"]) == 1


def test_a_live_holder_prints_already_running_and_exits_zero(
    capsys: pytest.CaptureFixture[str], sleeper: str
) -> None:
    add_job(capsys)
    with Store.open() as store:
        now = dt.datetime.now(dt.UTC)
        assert take_lease(
            store, "runner", sleeper, now=format_timestamp(now), expires_at=expiry(now),
            is_alive=lambda _h: False,
        )  # fmt: skip
    res = run_cli(capsys, "--json", "run")
    (doc,) = lines(res.out)
    assert res.code == 0 and doc["already_running"] is True
    assert doc["holder"]["pid"] == int(sleeper.split(":")[1]) and doc["started"] == []
    human = run_cli(capsys, "run")
    assert human.code == 0 and "already running" in human.out


def test_human_output_lists_started_jobs_and_quiet_prints_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, default_cap=None, auto_run=False)
    first, second = add_job(capsys, config=cfg), add_job(capsys, config=cfg)
    res = run_cli(capsys, "--config", cfg, "run", "--max-starts", "1")
    assert res.out.strip() in (f"started {first}", f"started {second}")
    quiet = run_cli(capsys, "--config", cfg, "--quiet", "run")
    assert quiet.code == 0 and quiet.out == ""


def test_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "run", "--interval", "5s").code == 2
    assert run_cli(capsys, "run", "--loop", "--triggered", "0").code == 2


# -- busy and the triggered back-off -------------------------------------------------------------


@pytest.fixture
def respawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    monkeypatch.setattr(gateway, "sleep", lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(
        run_module, "spawn_pass", lambda *a, **k: events.append(("spawn", k.get("retried")))
    )
    return events


def busy_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a: Any, **k: Any) -> Any:
        raise JobsctlError("busy", "state.db is busy")

    monkeypatch.setattr(run_module.Store, "open", fail)


def test_a_busy_pass_exits_one_without_a_respawn_unless_triggered(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    respawns: list[tuple[str, Any]],
) -> None:  # fmt: skip
    busy_store(monkeypatch)
    res = run_cli(capsys, "--json", "run")
    assert res.code == 1 and res.envelope()["error"]["code"] == "busy"
    assert respawns == []


def test_a_triggered_busy_pass_respawns_once_after_two_seconds(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    respawns: list[tuple[str, Any]],
) -> None:  # fmt: skip
    busy_store(monkeypatch)
    assert run_cli(capsys, "run", "--quiet", "--triggered", "0").code == 1
    assert respawns == [("sleep", 2.0), ("spawn", 1)]
    respawns.clear()
    assert run_cli(capsys, "run", "--quiet", "--triggered", "1").code == 1
    assert respawns == []  # the second pass does not chain a third


def test_a_triggered_pass_busy_after_the_lease_does_not_respawn(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    respawns: list[tuple[str, Any]],
) -> None:  # fmt: skip
    def after_lease(self: Runner, **kwargs: Any) -> Any:
        self.lease_taken = True
        raise JobsctlError("busy", "state.db is busy")

    monkeypatch.setattr(Runner, "run_pass", after_lease)
    assert run_cli(capsys, "run", "--quiet", "--triggered", "0").code == 1
    assert respawns == []


def test_a_triggered_busy_pass_with_auto_run_false_does_not_respawn(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    respawns: list[tuple[str, Any]], tmp_path: Path,
) -> None:  # fmt: skip
    cfg = config_file(tmp_path, auto_run=False)
    busy_store(monkeypatch)
    assert run_cli(capsys, "--config", cfg, "run", "--quiet", "--triggered", "0").code == 1
    assert respawns == []


def test_a_triggered_pass_sends_its_diagnostics_to_its_own_file(
    capsys: pytest.CaptureFixture[str], no_diag_redirect: list[Path]
) -> None:
    run_cli(capsys, "--quiet", "run")
    assert no_diag_redirect == []
    run_cli(capsys, "--quiet", "run", "--triggered", "1")
    (path,) = no_diag_redirect
    assert path.parent.name == "diag" and path.name == f"run-{os.getpid()}.log"


def test_a_triggered_pass_installs_its_diagnostics_before_the_config_is_loaded(
    capsys: pytest.CaptureFixture[str], no_diag_redirect: list[Path], tmp_path: Path
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("{not: [valid", encoding="utf-8")
    res = run_cli(capsys, "--config", str(broken), "run", "--quiet", "--triggered", "0")
    assert res.code != 0
    (path,) = no_diag_redirect
    assert path.parent.name == "diag" and path.name == f"run-{os.getpid()}.log"


# -- --loop --------------------------------------------------------------------------------------


def test_loop_repeats_passes_prints_one_line_each_and_stops_on_sigterm(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, run_interval="7s")
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 35 + 35 + 3:  # two full 7 s intervals (0.2 s slices), then a signal
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(gateway, "sleep", sleep)
    before = signal.getsignal(signal.SIGTERM)
    res = run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert res.code == 0
    assert (
        len(lines(res.out)) == 3
    )  # passes at 0, 7 and 14 s; the signal came during the third wait
    assert sleeps[0] == pytest.approx(0.2)
    assert signal.getsignal(signal.SIGTERM) == before


def test_loop_interval_flag_overrides_the_config(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 7:  # 5 slices of 0.2 s make 1 s; the second pass follows
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(gateway, "sleep", sleep)
    res = run_cli(capsys, "--json", "run", "--loop", "--interval", "1s")
    assert res.code == 0 and len(lines(res.out)) == 2  # 1 s = 5 slices of 0.2 s


def test_loop_finishes_the_current_pass_when_signaled_during_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, default_cap=None, auto_run=False)
    first = add_job(capsys, config=cfg)
    real = Runner.run_pass

    def signaled(self: Runner, **kwargs: Any) -> Any:
        self.probe = lambda step: os.kill(os.getpid(), signal.SIGTERM) if step == "claim" else None
        return real(self, **kwargs)

    monkeypatch.setattr(Runner, "run_pass", signaled)
    monkeypatch.setattr(gateway, "sleep", lambda s: pytest.fail("slept after the signal"))
    res = run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    (doc,) = lines(res.out)
    assert res.code == 0 and doc["started"] == [first]  # the claim and spawn were finished


def test_loop_reaps_its_own_shim_children_each_iteration(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real shim confirms its identity and ends the job (its cwd is missing): the loop reaps
    the exited child by its handle, and a confirmed shim is never a `claim_orphaned` requeue."""
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    job = add_job(capsys, config=cfg)
    sleeps: list[float] = []
    timeouts: list[str] = []
    reaped: list[int] = []
    runners: list[Runner] = []
    real_reap = Runner.reap

    def counting_reap(self: Runner, result: Any) -> None:
        runners.append(self)
        real_reap(self, result)
        reaped.append(len(self.children))

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            # Wait until the shim (this process's child) has exited, however long a loaded host
            # takes to start it: the next pass must find it exited. Poll only the loop's own
            # handles, bounded so a shim that never ends fails the test instead of hanging it.
            deadline = time.monotonic() + 60
            while any(c.popen.poll() is None for c in runners[0].children):
                if time.monotonic() >= deadline:
                    # not an assert: the CLI turns an exception raised here into an
                    # internal-error result and hides its message; end the loop instead
                    timeouts.append("the shim did not exit within 60 s")
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)
        if len(sleeps) == 7:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(gateway, "sleep", sleep)
    monkeypatch.setattr(Runner, "reap", counting_reap)
    res = run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert timeouts == []
    assert res.code == 0, res.err
    docs = lines(res.out)
    assert docs[0]["started"] == [job]
    assert [f for d in docs[1:] for f in d["reconciled"]] == []
    assert reaped[-1] == 0  # the exited shim is no longer a child of the loop
    found = row(job)
    assert (found["status"], found["reason"], found["spawn_failures"]) == (
        "failed",
        "cwd_missing",
        0,
    )


def test_loop_keeps_the_last_config_when_a_reload_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = Path(config_file(tmp_path, run_interval="1s"))
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            cfg.write_text("{not: [valid", encoding="utf-8")
        if len(sleeps) == 7:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(gateway, "sleep", sleep)
    res = run_cli(capsys, "--config", str(cfg), "--json", "run", "--loop")
    assert res.code == 0 and len(lines(res.out)) == 2
    assert "config not reloaded" in res.err


@pytest.mark.parametrize("failure", ["busy", "lease_lost"])
def test_loop_survives_a_busy_database_or_a_lost_lease_in_one_pass(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    failure: str,
) -> None:  # fmt: skip
    from jobsctl.runner import LeaseLost

    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    real = Runner.run_pass
    calls: list[int] = []

    def flaky(self: Runner, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise JobsctlError("busy", "state.db is busy") if failure == "busy" else LeaseLost()
        return real(self, **kwargs)

    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 7:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(Runner, "run_pass", flaky)
    monkeypatch.setattr(gateway, "sleep", sleep)
    res = run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert res.code == 0 and len(calls) == 2  # the next interval ran another pass
    assert len(lines(res.out)) == 1 and "pass failed" in res.err


def test_loop_stops_on_any_other_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Runner, **kwargs: Any) -> Any:
        raise JobsctlError("config_invalid", "bad config")

    monkeypatch.setattr(Runner, "run_pass", broken)
    monkeypatch.setattr(gateway, "sleep", lambda s: pytest.fail("slept after a fatal error"))
    res = run_cli(capsys, "--json", "run", "--loop")
    assert res.code != 0 and res.envelope()["error"]["code"] == "config_invalid"
