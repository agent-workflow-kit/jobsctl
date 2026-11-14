"""Start triggers (interface.md, Start triggers): the counter, the lease check, who spawns."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway, triggers
from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.leases import expiry
from jobsctl.store_db import Store
from jobsctl.store_ops import counter_get, take_lease
from jobsctl.timestamps import format_timestamp
from jobsctl.triggers import PASS_REQUESTED, request_pass, request_pass_from_command
from tests.helpers import run_cli
from tests.manual_helpers import (
    HUMAN,
    add_job,
    config_file,
    make_held,
    make_starting,
    row,
    sql,
)
from tests.runner_fixtures import PassSpawns


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state") as s:
        yield s


def counter(store: Store) -> int:
    with store.write() as tx:
        return counter_get(tx, PASS_REQUESTED)


def hold_lease(store: Store, holder: str) -> None:
    now = dt.datetime.now(dt.UTC)
    assert take_lease(
        store, "runner", holder, now=format_timestamp(now), expires_at=expiry(now),
        is_alive=lambda _h: False,
    )  # fmt: skip


def test_a_request_without_a_holder_counts_and_spawns_one_pass(
    store: Store, trigger_spawns: PassSpawns
) -> None:
    request_pass(store, Config(), Path("/etc/jobsctl.yaml"))
    assert counter(store) == 1
    assert trigger_spawns == [
        {"state_dir": store.paths.root, "config_path": Path("/etc/jobsctl.yaml"), "retried": 0}
    ]


def test_a_request_with_a_live_holder_only_counts(
    store: Store, trigger_spawns: PassSpawns, sleeper: str
) -> None:
    hold_lease(store, sleeper)
    assert request_pass(store, Config(), None) is None
    assert request_pass(store, Config(), None) is None
    assert counter(store) == 2 and trigger_spawns == []


def test_a_dead_holder_does_not_stop_the_spawn(store: Store, trigger_spawns: PassSpawns) -> None:
    hold_lease(store, "gone-boot:5:5")
    request_pass(store, Config(), None)
    assert len(trigger_spawns) == 1


def test_auto_run_false_disables_the_trigger_entirely(
    store: Store, trigger_spawns: PassSpawns
) -> None:
    assert request_pass(store, Config(auto_run=False), None) is None
    assert counter(store) == 0 and trigger_spawns == []


def test_the_counter_and_the_lease_check_share_one_transaction(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real = triggers.holder_of

    def spy(row: Any, host: Any, now: Any) -> Any:
        seen.append(store.conn.in_transaction)
        return real(row, host, now)

    monkeypatch.setattr(triggers, "holder_of", spy)
    request_pass(store, Config(), None)
    assert seen == [True]


@pytest.mark.real_trigger
def test_the_real_spawn_is_a_detached_run_with_the_hand_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str], str]] = []

    class Proc:
        pid = 1

    def fake(argv: list[str], *, env: dict[str, str], cwd: str) -> Proc:
        calls.append((argv, env, cwd))
        return Proc()

    monkeypatch.setattr(gateway, "spawn_detached", fake)
    monkeypatch.setenv("JOBSCTL_SECRET", "x")
    request_pass(store, Config(), Path("/cfg/c.yaml"))
    ((argv, env, cwd),) = calls
    assert argv[1:] == [
        "-I", "-m", "jobsctl", "--config", "/cfg/c.yaml", "run", "--quiet", "--triggered", "0"
    ]  # fmt: skip
    assert cwd == str(store.paths.root)
    assert not any(k.startswith("JOBSCTL_") for k in env)


# -- from a command ------------------------------------------------------------------------------


def test_a_command_trigger_never_fails_the_command(
    store: Store, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*a: Any, **k: Any) -> Any:
        raise JobsctlError("busy", "state.db is busy")

    monkeypatch.setattr(triggers, "request_pass", busy)
    request_pass_from_command(Config(), None)
    assert "could not request a pass (state.db is busy)" in capsys.readouterr().err
    request_pass_from_command(Config(), None, quiet=True)
    assert capsys.readouterr().err == ""

    def no_exec(*a: Any, **k: Any) -> Any:
        raise OSError("no exec")

    monkeypatch.setattr(triggers, "request_pass", no_exec)
    request_pass_from_command(Config(), None)
    assert "OSError" in capsys.readouterr().err


def test_a_command_trigger_with_auto_run_false_opens_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("opened the store")

    monkeypatch.setattr(triggers.Store, "open", boom)
    request_pass_from_command(Config(auto_run=False), None)


def test_a_locked_database_only_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], trigger_spawns: PassSpawns
) -> None:
    with Store.open() as first:
        pass
    blocker = sqlite3.connect(first.paths.db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        request_pass_from_command(Config.model_validate({"busy_timeout": "1s"}), None)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert "could not request a pass" in capsys.readouterr().err
    assert trigger_spawns == []


# -- the commands that trigger -------------------------------------------------------------------


def test_add_triggers_unless_no_start_dry_run_or_a_repeat(
    capsys: pytest.CaptureFixture[str], trigger_spawns: PassSpawns
) -> None:
    base = ("add", "--cwd", "/w", "--unique-key", "k1", "--", "x")
    assert run_cli(capsys, *base).code == 0
    assert len(trigger_spawns) == 1
    assert run_cli(capsys, *base).code == 0  # the unique key hit: nothing was created
    assert len(trigger_spawns) == 1
    assert run_cli(capsys, "add", "--no-start", "--cwd", "/w", "--", "x").code == 0
    assert run_cli(capsys, "add", "--dry-run", "--cwd", "/w", "--", "x").code == 0
    assert len(trigger_spawns) == 1


def test_add_with_auto_run_false_triggers_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, trigger_spawns: PassSpawns
) -> None:
    cfg = config_file(tmp_path, auto_run=False)
    assert run_cli(capsys, "--config", cfg, "add", "--cwd", "/w", "--", "x").code == 0
    assert trigger_spawns == []


def test_the_pass_is_handed_the_absolute_config(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, trigger_spawns: PassSpawns,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    cfg = config_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert run_cli(capsys, "--config", "config.yaml", "add", "--cwd", "/w", "--", "x").code == 0
    assert trigger_spawns[0]["config_path"] == Path(cfg)


def test_release_triggers_only_when_it_changed_something(
    capsys: pytest.CaptureFixture[str], trigger_spawns: PassSpawns
) -> None:
    job = add_job(capsys)
    assert run_cli(capsys, "hold", job).code == 0
    assert trigger_spawns == []  # holding frees nothing to start
    assert run_cli(capsys, "release", job).code == 0
    assert len(trigger_spawns) == 1
    assert run_cli(capsys, "release", job).code == 0  # already queued: unchanged
    assert run_cli(capsys, "release", job, "--dry-run").code == 0
    assert len(trigger_spawns) == 1


def test_a_refused_release_does_not_trigger(
    capsys: pytest.CaptureFixture[str], trigger_spawns: PassSpawns
) -> None:
    job = add_job(capsys)
    make_held(job, "exit_13")
    assert run_cli(capsys, "release", job).code != 0
    assert trigger_spawns == []


def test_cancel_triggers_only_for_a_starting_job(
    capsys: pytest.CaptureFixture[str], trigger_spawns: PassSpawns
) -> None:
    queued, held, starting = add_job(capsys), add_job(capsys), add_job(capsys)
    make_held(held, "manual")
    make_starting(starting)
    assert run_cli(capsys, "cancel", queued).code == 0
    assert run_cli(capsys, "cancel", held).code == 0
    assert trigger_spawns == []
    assert run_cli(capsys, "cancel", starting, "--dry-run").code == 0
    assert trigger_spawns == [] and row(starting)["status"] == "starting"
    assert run_cli(capsys, "cancel", starting).code == 0
    assert len(trigger_spawns) == 1 and row(starting)["status"] == "canceled"
    assert run_cli(capsys, "cancel", starting).code == 0  # already canceled: unchanged
    assert len(trigger_spawns) == 1


def test_a_selector_cancel_that_ends_a_starting_job_triggers(
    capsys: pytest.CaptureFixture[str], trigger_spawns: PassSpawns
) -> None:
    starting = add_job(capsys, "--queue", "pool")
    make_starting(starting)
    sql("UPDATE jobs SET queue = 'pool' WHERE id = ?", starting)
    assert run_cli(capsys, "cancel", "--queue", "pool", "--all", *HUMAN).code == 0
    assert len(trigger_spawns) == 1


def test_a_failing_trigger_leaves_the_released_job_released(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = add_job(capsys)
    assert run_cli(capsys, "hold", job).code == 0

    def broken(*a: Any, **k: Any) -> Any:
        raise OSError("fork failed")

    monkeypatch.setattr("jobsctl.triggers.spawn_pass", broken)
    res = run_cli(capsys, "release", job)
    assert res.code == 0 and "could not request a pass" in res.err
    assert row(job)["status"] == "queued"
