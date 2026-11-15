"""Upgrade detection and the `run --loop` re-exec between passes (interface.md, Upgrades)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway, upgrade
from jobsctl import cli as cli_module
from jobsctl.config import Config
from jobsctl.leases import identity_text, parse_identity
from jobsctl.proc_host import default_host
from jobsctl.runner import AdoptedShim, PassResult, Runner
from jobsctl.store_db import OpenMode, Store
from jobsctl.upgrade import REEXEC_ENV, UpgradeWatch
from tests.helpers import run_cli
from tests.manual_helpers import config_file
from tests.runner_helpers import add_queued, job_row, put_starting


@pytest.fixture(autouse=True)
def no_diag_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "install", lambda path: None)


@pytest.fixture
def store() -> Store:
    with Store.open(mode=OpenMode.CLI) as opened:
        yield opened  # type: ignore[misc]


# -- installed_version -------------------------------------------------------------------------


def test_installed_version_reads_the_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert upgrade.installed_version() == metadata.version("jobsctl")

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    assert upgrade.installed_version() is None


# -- reexec_argv ---------------------------------------------------------------------------------


def test_reexec_argv_keeps_the_original_arguments_and_uses_the_running_interpreter() -> None:
    orig = ["python3", "-I", "-m", "jobsctl", "--config", "/c.yaml", "run", "--loop"]
    assert upgrade.reexec_argv(orig, "/abs/python") == [
        "/abs/python", "-I", "-m", "jobsctl", "--config", "/c.yaml", "run", "--loop",
    ]  # fmt: skip


def test_reexec_argv_rebuilds_an_isolated_module_run_without_orig_argv() -> None:
    assert upgrade.reexec_argv([], "/abs/python", ["run", "--loop"]) == [
        "/abs/python", "-I", "-m", "jobsctl", "run", "--loop",
    ]  # fmt: skip


# -- UpgradeWatch --------------------------------------------------------------------------------


def _bump_schema(store: Store, by: int = 1) -> None:
    with store.write() as tx:
        tx.execute(f"PRAGMA user_version = {store.known_version + by}")


def test_no_reason_while_nothing_changed(store: Store) -> None:
    assert UpgradeWatch({}).check(store) is None


def test_a_newer_schema_is_a_reason(store: Store) -> None:
    watch = UpgradeWatch({})
    _bump_schema(store)
    reason = watch.check(store)
    assert reason is not None and "newer" in reason


def test_a_changed_installed_version_is_a_reason(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.0.0")
    watch = UpgradeWatch({})
    assert watch.check(store) is None
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.1.0")
    reason = watch.check(store)
    assert reason is not None and "1.0.0" in reason and "1.1.0" in reason


def test_an_unreadable_installed_version_is_never_a_reason(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.0.0")
    watch = UpgradeWatch({})
    monkeypatch.setattr(upgrade, "installed_version", lambda: None)
    assert watch.check(store) is None


def test_the_reason_that_started_this_image_does_not_repeat(store: Store) -> None:
    _bump_schema(store)
    first = UpgradeWatch({}).check(store)
    assert first is not None
    env = {REEXEC_ENV: first}
    again = UpgradeWatch(env)
    assert REEXEC_ENV not in env  # never inherited by anything this process spawns
    assert again.check(store) is None  # the new image is no newer than the old one: no flapping
    _bump_schema(store, 2)  # a different reason is honored
    assert again.check(store) is not None


def test_a_failed_reexec_is_not_retried_for_the_same_reason(store: Store) -> None:
    watch = UpgradeWatch({})
    _bump_schema(store)
    reason = watch.check(store)
    assert reason is not None
    watch.failed(reason)
    assert watch.check(store) is None


def test_reexec_passes_the_environment_plus_the_reason_and_refuses_a_missing_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(gateway, "exec_replace", lambda argv, env: calls.append((argv, dict(env))))
    monkeypatch.setenv("FOO", "bar")
    UpgradeWatch().reexec("why")
    (argv, env), = calls
    assert argv[0] == sys.executable
    assert env["FOO"] == "bar" and env[REEXEC_ENV] == "why"
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    with pytest.raises(OSError):
        UpgradeWatch().reexec("why")
    assert len(calls) == 1


# -- run --loop ----------------------------------------------------------------------------------


class _Exec(BaseException):
    pass


def _stop_after(monkeypatch: pytest.MonkeyPatch, slices: int) -> list[float]:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == slices:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(gateway, "sleep", sleep)
    return sleeps


def test_loop_reexecs_between_passes_on_a_newer_schema(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    passes: list[int] = []
    real = Runner.run_pass

    def counted(self: Runner, **kwargs: Any) -> Any:
        passes.append(1)
        if len(passes) == 1:  # an upgrade happens while the first pass runs
            _bump_schema(self.store)
        return real(self, **kwargs)

    execs: list[tuple[list[str], dict[str, str]]] = []

    def fake_exec(argv: list[str], env: Any) -> None:
        execs.append((argv, dict(env)))
        raise _Exec  # a real exec never returns

    monkeypatch.setattr(Runner, "run_pass", counted)
    monkeypatch.setattr(gateway, "exec_replace", fake_exec)
    _stop_after(monkeypatch, 100)
    with pytest.raises(_Exec):
        run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert len(passes) == 1  # re-exec happens between passes, never inside one
    ((argv, env),) = execs
    assert "newer" in env[REEXEC_ENV]
    assert argv[0] == sys.executable


def test_loop_does_not_reexec_once_a_stop_was_requested(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    real = Runner.run_pass

    def pass_then_upgrade_and_stop(self: Runner, **kwargs: Any) -> Any:
        result = real(self, **kwargs)
        _bump_schema(self.store)
        os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(Runner, "run_pass", pass_then_upgrade_and_stop)
    monkeypatch.setattr(gateway, "sleep", lambda s: None)
    monkeypatch.setattr(gateway, "exec_replace", lambda a, e: pytest.fail("re-exec after stop"))
    res = run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert res.code == 0


def test_loop_keeps_running_when_the_exec_fails_and_does_not_retry_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    real = Runner.run_pass
    attempts: list[int] = []

    def upgrading(self: Runner, **kwargs: Any) -> Any:
        if not attempts:
            _bump_schema(self.store)
        return real(self, **kwargs)

    def failing(argv: list[str], env: Any) -> None:
        attempts.append(1)
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(Runner, "run_pass", upgrading)
    monkeypatch.setattr(gateway, "exec_replace", failing)
    _stop_after(monkeypatch, 3 * 5 + 2)  # three further iterations
    res = run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert res.code == 0
    assert len(attempts) == 1
    assert "re-exec failed" in res.err and len(res.out.splitlines()) >= 3


def test_loop_reexecs_on_a_changed_installed_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    current = {"v": "1.0.0"}
    monkeypatch.setattr(upgrade, "installed_version", lambda: current["v"])
    real = Runner.run_pass

    def pass_then_install(self: Runner, **kwargs: Any) -> Any:
        current["v"] = "2.0.0"
        return real(self, **kwargs)

    seen: list[str] = []

    def fake_exec(argv: list[str], env: Any) -> None:
        seen.append(env[REEXEC_ENV])
        raise _Exec

    monkeypatch.setattr(Runner, "run_pass", pass_then_install)
    monkeypatch.setattr(gateway, "exec_replace", fake_exec)
    monkeypatch.setattr(gateway, "sleep", lambda s: None)
    with pytest.raises(_Exec):
        run_cli(capsys, "--config", cfg, "--json", "run", "--loop")
    assert seen == ["installed version 1.0.0 -> 2.0.0"]


def test_loop_does_not_reexec_without_an_upgrade(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    monkeypatch.setattr(gateway, "exec_replace", lambda a, e: pytest.fail("unexpected re-exec"))
    _stop_after(monkeypatch, 12)
    assert run_cli(capsys, "--config", cfg, "--json", "run", "--loop").code == 0


# -- adopting the shims of the previous image ---------------------------------------------------


def _child(tmp_path: Path, code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(tmp_path), start_new_session=True,
    )  # fmt: skip


def _wait_for(predicate: Any, what: str, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {what}")


def _adopting_runner(store: Store) -> Runner:
    return Runner(store, Config(), None)


def _starting_job(store: Store, job_id: str, identity: str, **over: Any) -> None:
    add_queued(store, job_id)
    put_starting(store, job_id, shim_identity=identity, **over)


def test_an_adopted_unconfirmed_shim_that_exits_is_requeued_by_its_pid(
    store: Store, tmp_path: Path
) -> None:
    child = _child(tmp_path, "import time; time.sleep(1)")
    try:
        _starting_job(store, "jb-aaaaaaaa", identity_text(default_host(), child.pid))
        runner = _adopting_runner(store)
        runner.adopt_shims()
        (adopted,) = runner.children
        assert isinstance(adopted.popen, AdoptedShim) and adopted.popen.pid == child.pid
        result = PassResult()
        _wait_for(lambda: (runner.reap(result), not runner.children)[1], "the adopted shim")
        assert child.returncode is None  # reaped by the runner's waitpid, not by Popen
        assert result.reconciled == [
            {"job_id": "jb-aaaaaaaa", "finding": "claim_orphaned", "action": "requeued"}
        ]
        assert job_row(store, "jb-aaaaaaaa")["status"] == "queued"
    finally:
        try:
            child.wait(timeout=20)
        except Exception:
            pass


def test_a_shim_that_is_not_our_child_is_dropped_without_touching_its_attempt(
    store: Store, tmp_path: Path
) -> None:
    code = (
        "import subprocess, sys;"
        "p = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(3)'],"
        " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
        " stderr=subprocess.DEVNULL);"
        "print(p.pid, flush=True)"
    )
    middle = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    pid = int(middle.stdout)  # a grandchild: it outlives its parent and is nobody's child of ours
    _starting_job(store, "jb-aaaaaaaa", identity_text(default_host(), pid))
    runner = _adopting_runner(store)
    runner.adopt_shims()
    (adopted,) = runner.children
    result = PassResult()
    _wait_for(lambda: (runner.reap(result), not runner.children)[1], "the drop")
    assert result.reconciled == []  # waitpid said "not a child": nothing is concluded
    assert job_row(store, "jb-aaaaaaaa")["status"] == "starting"
    assert isinstance(adopted.popen, AdoptedShim) and not adopted.popen.ours


def test_a_recorded_identity_that_no_longer_matches_is_not_adopted(
    store: Store, tmp_path: Path
) -> None:
    child = _child(tmp_path, "import time; time.sleep(1)")
    try:
        ident = parse_identity(identity_text(default_host(), child.pid))
        assert ident is not None
        stale = f"{ident.boot_id}:{ident.pid}:{ident.start + 1}"  # the PID was reused
        _starting_job(store, "jb-aaaaaaaa", stale)
        _starting_job(store, "jb-bbbbbbbb", "not an identity")
        runner = _adopting_runner(store)
        runner.adopt_shims()
        assert runner.children == []
    finally:
        child.wait(timeout=20)


def test_a_confirmed_adopted_shim_that_exits_leaves_its_attempt_to_reconcile(
    store: Store, tmp_path: Path
) -> None:
    child = _child(tmp_path, "pass")
    try:
        _starting_job(
            store, "jb-aaaaaaaa", identity_text(default_host(), child.pid),
            shim_confirmed_at="2026-01-01T00:00:00.000Z",
        )  # fmt: skip
        runner = _adopting_runner(store)
        runner.adopt_shims()
        _wait_for(lambda: (runner.reap(PassResult()), not runner.children)[1], "the reap")
        assert job_row(store, "jb-aaaaaaaa")["status"] == "starting"
    finally:
        child.wait(timeout=20)
