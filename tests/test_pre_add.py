"""The `pre-add` hook (interface.md, Hooks and `add`): real hook scripts run by `add` before the
job is stored. A veto exits 11 (`vetoed`), an escalation exits 13 (`needs_human`), both store
nothing; a failing, timed-out or killed hook vetoes (fail closed)."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway, pre_add
from jobsctl.commands import add as add_module
from jobsctl.env_file import load_env_file
from jobsctl.hook_run import HookResult
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.helpers import Result, run_cli
from tests.hook_real import HookTracker, recorded_pids, running_pids, tracker_fixture

pytestmark = pytest.mark.usefixtures("validated_records")

SECRET = "s3cr3t-VALUE-9f2"


@pytest.fixture(autouse=True)
def trusted_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = load_env_file
    monkeypatch.setattr(
        add_module, "load_env_file", lambda path: real(path, check_ancestors_from=tmp_path)
    )
    from jobsctl import delivery

    monkeypatch.setattr(
        delivery, "load_env_file", lambda path: real(path, check_ancestors_from=tmp_path)
    )


@pytest.fixture(autouse=True)
def clean_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> Any:
    yield from tracker_fixture(monkeypatch)


def script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / f"{name}.sh"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def config(tmp_path: Path, *hooks: dict[str, Any], **keys: Any) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps({"hooks": {"pre-add": list(hooks)}, **keys}), encoding="utf-8")
    return str(path)


def add(
    capsys: pytest.CaptureFixture[str], cfg: str, *flags: str, argv: tuple[str, ...] = ("true",)
) -> Result:
    return run_cli(
        capsys, "--json", "--config", cfg, "add", "--no-start", *flags, "--cwd", "/work", "--",
        *argv,
    )  # fmt: skip


def jobs() -> list[sqlite3.Row]:
    db = resolve_state_dir() / "state.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM jobs"))
    finally:
        conn.close()


def queues_seen() -> list[str]:
    db = resolve_state_dir() / "state.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM queues_seen")]
    finally:
        conn.close()


def hook_records() -> list[dict[str, Any]]:
    path = resolve_state_dir() / "log.jsonl"
    if not path.exists():
        return []
    return [r for r in validate_record_file(path, "log.json") if r["event"] == "hook_ran"]


def error(res: Result) -> dict[str, Any]:
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    return doc["error"]


# -- allow --------------------------------------------------------------------------------------


def test_allow_stores_the_job_and_the_payload_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    seen = tmp_path / "seen"
    seen.mkdir()
    hook = script(
        tmp_path,
        "allow",
        f"cat > {seen}/payload.json\npwd > {seen}/cwd\n"
        f'echo "$JOBSCTL_HOOK $JOBSCTL_ID $JOBSCTL_OBJECT_TYPE" > {seen}/env\nexit 0\n',
    )
    res = add(
        capsys,
        config(tmp_path, {"name": "gate", "cmd": hook}),
        "--label", "ticket=T1", "--env", "A=1", "--unique-key", "k1", "--priority", "5", "--top",
        "--decision-id", "d1",
        argv=("echo", "hi"),
    )  # fmt: skip
    assert res.code == 0, res.out + res.err
    stored = jobs()
    assert len(stored) == 1
    payload = json.loads((seen / "payload.json").read_text())
    validate_against("hooks/pre-add.json", payload)
    assert payload["hook"] == "pre-add" and payload["object_id"] == stored[0]["id"]
    assert payload["labels"] == ["ticket=T1"]
    assert payload["job"]["argv"] == ["echo", "hi"]
    assert payload["job"]["env_names"] == ["A"] and "env" not in payload["job"]
    assert payload["job"]["unique_key"] == "k1" and payload["job"]["priority"] == 5
    assert payload["job"]["top"] is True and payload["job"]["decision_ids"] == ["d1"]
    assert payload["previous_status"] is None and payload["attempt"] is None
    assert "1" not in json.dumps(payload["job"]["env_names"])
    state_dir = resolve_state_dir()
    assert (seen / "cwd").read_text().strip() == str(state_dir)
    assert (seen / "env").read_text().split() == ["pre-add", stored[0]["id"], "job"]
    (record,) = hook_records()
    assert record["hook"] == "pre-add" and record["outcome"] == "allow"
    assert record["hook_id"] == "gate" and record["job_id"] == stored[0]["id"]


def test_garbage_stdout_and_stderr_are_ignored_on_allow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hook = script(tmp_path, "g", "printf '\\377\\000not json{{'; echo noise >&2; exit 0\n")
    res = add(capsys, config(tmp_path, {"cmd": hook}))
    assert res.code == 0, res.out + res.err
    assert len(jobs()) == 1
    assert hook_records()[0]["decision_ids"] == []


def test_decision_ids_are_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hook = script(tmp_path, "d", 'echo \'{"decision_id": ["dec-1", "dec-2"]}\'\n')
    assert add(capsys, config(tmp_path, {"cmd": hook})).code == 0
    assert hook_records()[0]["decision_ids"] == ["dec-1", "dec-2"]


# -- veto ---------------------------------------------------------------------------------------


def test_veto_exits_11_stores_nothing_and_reports_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hook = script(tmp_path, "veto", "echo 'not on fridays' >&2\nexit 1\n")
    res = add(capsys, config(tmp_path, {"name": "gate", "cmd": hook}), "--queue", "fresh")
    assert res.code == 11, res.out + res.err
    err = error(res)
    assert err["code"] == "vetoed" and err["details"]["hook_id"] == "gate"
    assert "not on fridays" in err["message"] and "gate" in err["message"]
    assert jobs() == [] and queues_seen() == []
    (record,) = hook_records()
    assert record["outcome"] == "veto" and record["exit"] == 1
    assert "not on fridays" in record["stderr"]


def test_veto_human_mode_prints_the_message_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hook = script(tmp_path, "veto", "echo 'nope' >&2\nexit 3\n")
    res = run_cli(
        capsys, "--config", config(tmp_path, {"cmd": hook}), "add", "--no-start", "--", "true"
    )
    assert res.code == 11 and res.out == "" and "nope" in res.err


def test_a_veto_stops_later_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "second-ran"
    first = script(tmp_path, "first", "exit 2\n")
    second = script(tmp_path, "second", f"touch {marker}\n")
    res = add(capsys, config(tmp_path, {"name": "a", "cmd": first}, {"name": "b", "cmd": second}))
    assert res.code == 11 and not marker.exists() and jobs() == []


@pytest.mark.parametrize(
    ("body", "what"),
    [
        ("kill -9 $$\n", "killed by a signal"),
        ("exit 127\n", "exit 127"),
        ("exit 255\n", "exit 255"),
        ("echo oops >&2; exit 14\n", "exit 14"),
    ],
)
def test_a_failing_or_crashing_hook_vetoes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tracker: HookTracker,
    body: str,
    what: str,
) -> None:
    res = add(capsys, config(tmp_path, {"cmd": script(tmp_path, "c", body)}))
    assert res.code == 11, what + res.out + res.err
    assert error(res)["code"] == "vetoed" and jobs() == []


def test_a_timeout_vetoes_and_the_group_is_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hook = script(tmp_path, "slow", "exec sleep 60\n")
    res = add(capsys, config(tmp_path, {"cmd": hook, "timeout": "1s"}))
    assert res.code == 11, res.out + res.err
    assert jobs() == []
    assert hook_records()[0]["outcome"] == "timeout"
    assert tracker.last.popen.returncode is not None  # reaped: the leader is gone


# -- escalate -----------------------------------------------------------------------------------


def test_escalation_exits_13_needs_human_and_stores_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "second-ran"
    first = script(tmp_path, "first", "echo 'ask a human' >&2\nexit 13\n")
    second = script(tmp_path, "second", f"touch {marker}\n")
    res = add(capsys, config(tmp_path, {"name": "a", "cmd": first}, {"name": "b", "cmd": second}))
    assert res.code == 13, res.out + res.err
    err = error(res)
    assert err["code"] == "needs_human" and err["details"]["hook_ids"] == ["a"]
    assert "ask a human" in err["message"]
    assert marker.exists()  # the remaining hooks still run
    assert jobs() == []
    assert [r["outcome"] for r in hook_records()] == ["escalate", "allow"]


def test_a_veto_beats_an_escalation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    first = script(tmp_path, "first", "exit 13\n")
    second = script(tmp_path, "second", "exit 1\n")
    res = add(capsys, config(tmp_path, {"name": "a", "cmd": first}, {"name": "b", "cmd": second}))
    assert res.code == 11 and error(res)["details"]["hook_id"] == "b"
    assert jobs() == []


# -- selection, ordering with the other decisions ---------------------------------------------


def test_only_selected_hooks_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    veto = script(tmp_path, "veto", "exit 1\n")
    cfg = config(
        tmp_path,
        {"name": "other-queue", "cmd": veto, "queues": ["elsewhere"]},
        {"name": "excluded", "cmd": veto, "exclude_queues": ["default"]},
        {"name": "other-label", "cmd": veto, "labels": ["kind=x"]},
    )
    assert add(capsys, cfg, "--label", "kind=y").code == 0
    assert len(jobs()) == 1
    assert add(capsys, cfg, "--label", "kind=x").code == 11


def test_a_unique_key_hit_runs_no_hook_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "ran"
    hook = script(tmp_path, "count", f"echo x >> {marker}\nexit 0\n")
    first = add(capsys, config(tmp_path, {"cmd": hook}), "--unique-key", "k")
    assert first.code == 0 and marker.read_text() == "x\n"
    veto = script(tmp_path, "veto", "exit 1\n")
    again = add(capsys, config(tmp_path, {"cmd": veto}), "--unique-key", "k")
    assert again.code == 0, again.out + again.err  # idempotent: the existing job, no hook
    doc = again.envelope()
    assert doc["created"] is False and doc["job"]["id"] == jobs()[0]["id"]
    assert len(jobs()) == 1
    assert len(hook_records()) == 1


def test_a_unique_key_that_frees_up_before_the_write_still_runs_the_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    """A veto cannot be skipped because a holder existed earlier: the hook runs whenever a job
    would actually be created."""
    veto = script(tmp_path, "veto", "exit 1\n")
    first = add(capsys, config(tmp_path), "--unique-key", "k")
    assert first.code == 0
    job_id = jobs()[0]["id"]
    assert run_cli(capsys, "--json", "cancel", job_id).code == 0  # the key is free again
    res = add(capsys, config(tmp_path, {"cmd": veto}), "--unique-key", "k")
    assert res.code == 11 and len(jobs()) == 1


def test_dry_run_runs_no_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "ran"
    hook = script(tmp_path, "h", f"touch {marker}\nexit 1\n")
    res = add(capsys, config(tmp_path, {"cmd": hook}), "--dry-run")
    assert res.code == 0 and res.envelope()["dry_run"] is True
    assert not marker.exists() and jobs() == [] and tracker.spawned == []


def test_secret_refusals_come_before_the_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "ran"
    hook = script(tmp_path, "h", f"touch {marker}\n")
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"API_TOKEN={SECRET}\n")
    env_file.chmod(0o600)
    cfg = config(tmp_path, {"cmd": hook}, env_file=str(env_file))
    res = add(capsys, cfg, argv=("echo", SECRET))
    assert res.code == 2 and not marker.exists() and tracker.spawned == []


def test_strict_queues_refusal_comes_before_the_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "ran"
    hook = script(tmp_path, "h", f"touch {marker}\n")
    res = add(capsys, config(tmp_path, {"cmd": hook}, strict_queues=True), "--queue", "nope")
    assert res.code == 10 and not marker.exists()


# -- secrets ------------------------------------------------------------------------------------


def test_the_payload_never_holds_a_secret_or_an_env_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"API_TOKEN={SECRET}\n")
    env_file.chmod(0o600)
    out = tmp_path / "payload.json"
    hook = script(tmp_path, "h", f"cat > {out}\nenv > {out}.env\n")
    cfg = config(tmp_path, {"cmd": hook}, env_file=str(env_file), mask_env=["API_TOKEN"])
    res = add(
        capsys, cfg, "--env", "PLAIN=visible-value", "--pass-env", "API_TOKEN",
        "--label", "a=b",
    )  # fmt: skip
    assert res.code == 0, res.out + res.err
    text = out.read_text()
    assert SECRET not in text and "visible-value" not in text
    payload = json.loads(text)
    assert payload["job"]["env_names"] == ["PLAIN"]
    assert payload["job"]["pass_env"] == ["API_TOKEN"]
    # the hook environment follows the existing hook rule: env_file values are in it
    assert f"API_TOKEN={SECRET}" in Path(f"{out}.env").read_text()


def test_the_hook_environment_honours_hooks_strip_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"API_TOKEN={SECRET}\n")
    env_file.chmod(0o600)
    out = tmp_path / "env"
    hook = script(tmp_path, "h", f"env > {out}\n")
    cfg = config(tmp_path, {"cmd": hook}, env_file=str(env_file), hooks_strip_env=["API_TOKEN"])
    assert add(capsys, cfg).code == 0
    assert "API_TOKEN" not in out.read_text()


def test_a_secret_in_hook_stderr_is_masked_in_the_error_and_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"API_TOKEN={SECRET}\n")
    env_file.chmod(0o600)
    hook = script(tmp_path, "h", 'echo "denied for $API_TOKEN" >&2\nexit 1\n')
    cfg = config(tmp_path, {"cmd": hook}, env_file=str(env_file))
    res = add(capsys, cfg)
    assert res.code == 11
    assert SECRET not in res.out + res.err
    assert SECRET not in (resolve_state_dir() / "log.jsonl").read_text()


def test_shell_metacharacters_in_job_data_never_reach_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    marker = tmp_path / "injected"
    out = tmp_path / "args"
    hook = script(tmp_path, "h", f'echo "$#" > {out}\ncat > /dev/null\n')
    nasty = f"$(touch {marker});`touch {marker}`"
    res = add(capsys, config(tmp_path, {"cmd": hook}), "--label", "k=v", argv=("echo", nasty))
    assert res.code == 0 and not marker.exists() and out.read_text().strip() == "0"


# -- stopped hooks ------------------------------------------------------------------------------


class _Fake:
    def __init__(self, **over: Any) -> None:
        self.over = over

    def __call__(self, **kw: Any) -> HookResult:
        fields: dict[str, Any] = dict(
            hook=kw["hook"], hook_id=kw["hook_id"], outcome="failed", exit=None, signal=None,
            decision_ids=(), warnings=(), stderr="", message="", stdout_truncated=False,
            stderr_truncated=False, leftovers=0, stopped=None, start_error=None,
            signals_sent=(), unkillable=False, duration=0.0,
        )  # fmt: skip
        fields.update(self.over)
        return HookResult(**fields)


@pytest.mark.parametrize("over", [{"stopped": "SIGTERM"}, {"unkillable": True}])
def test_a_stopped_or_unkillable_hook_stores_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    over: dict[str, Any],
) -> None:
    real = pre_add.Deliverer

    class Fake(real):  # type: ignore[valid-type, misc]
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.runner = _Fake(**over)

    monkeypatch.setattr(pre_add, "Deliverer", Fake)
    res = add(capsys, config(tmp_path, {"cmd": "true"}))
    assert res.code == 1, res.out + res.err
    assert error(res)["code"] == "internal" and jobs() == []


def test_a_hook_that_cannot_start_vetoes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pre_add.Deliverer

    class Fake(real):  # type: ignore[valid-type, misc]
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.runner = _Fake(start_error="ENOENT", message="hook could not be started")

    monkeypatch.setattr(pre_add, "Deliverer", Fake)
    res = add(capsys, config(tmp_path, {"cmd": "true"}))
    assert res.code == 11 and jobs() == []


# -- config -------------------------------------------------------------------------------------


def test_async_and_include_output_are_unknown_keys_for_pre_add(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    res = add(capsys, config(tmp_path, {"cmd": "true", "async": True}))
    assert res.code == 2 and error(res)["code"] == "config_invalid"


def test_no_hook_configured_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    res = add(capsys, config(tmp_path))
    assert res.code == 0 and len(jobs()) == 1 and tracker.spawned == []


def test_a_taken_id_runs_the_hooks_again_with_a_fresh_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tracker: HookTracker,
) -> None:
    first = add(capsys, config(tmp_path))
    before = [dict(row) for row in jobs()]
    taken = before[0]["id"]
    ids = iter([taken, "jb-aaaaaaaa"])
    monkeypatch.setattr(add_module, "new_job_id", lambda: next(ids))
    log = tmp_path / "ids"
    hook = script(tmp_path, "h", f'echo "$JOBSCTL_ID" >> {log}\n')
    res = add(capsys, config(tmp_path, {"cmd": hook}))
    assert first.code == 0 and res.code == 0, res.out + res.err
    assert log.read_text().split() == [taken, "jb-aaaaaaaa"]
    assert {row["id"] for row in jobs()} == {taken, "jb-aaaaaaaa"}
    assert [dict(r) for r in jobs() if r["id"] == taken] == before  # the existing job is untouched
    assert [r["job_id"] for r in hook_records()] == [taken, "jb-aaaaaaaa"]  # one record per run


# -- id rounds ----------------------------------------------------------------------------------


def test_every_round_after_the_last_hook_run_still_tries_the_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tracker: HookTracker,
) -> None:
    assert add(capsys, config(tmp_path)).code == 0
    taken = jobs()[0]["id"]
    draws = iter([taken] * (add_module.MAX_HOOK_ROUNDS - 1) + ["jb-bbbbbbbb"])
    monkeypatch.setattr(add_module, "new_job_id", lambda: next(draws))
    log = tmp_path / "runs"
    hook = script(tmp_path, "h", f"echo x >> {log}\n")
    res = add(capsys, config(tmp_path, {"cmd": hook}))
    assert res.code == 0, res.out + res.err
    assert "jb-bbbbbbbb" in {row["id"] for row in jobs()}
    assert len(log.read_text().split()) == add_module.MAX_HOOK_ROUNDS


def test_an_id_that_is_always_taken_ends_in_internal_after_a_bounded_number_of_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tracker: HookTracker,
) -> None:
    assert add(capsys, config(tmp_path)).code == 0
    taken = jobs()[0]["id"]
    monkeypatch.setattr(add_module, "new_job_id", lambda: taken)
    log = tmp_path / "runs"
    hook = script(tmp_path, "h", f"echo x >> {log}\n")
    res = add(capsys, config(tmp_path, {"cmd": hook}))
    assert res.code == 1 and error(res)["code"] == "internal"
    assert len(log.read_text().split()) == add_module.MAX_HOOK_ROUNDS
    assert len(jobs()) == 1


# -- the unique key re-checked at the write -----------------------------------------------------


def inner_add(key: str, marker: Path) -> str:
    """Hook body: the first run adds a job with `key` through a real, hook-less `jobsctl add`."""
    env = " ".join(
        f"{name}={os.environ[name]}"
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")
    )
    return (
        f"if [ ! -e {marker} ]; then touch {marker}; "
        f"env {env} LC_ALL=C {sys.executable} -m jobsctl --json add --no-start "
        f"--unique-key {key} --cwd /work -- true >/dev/null || exit 1; fi\n"
    )


def test_a_job_with_the_key_created_during_the_hooks_is_reused_not_duplicated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hook = script(tmp_path, "racer", inner_add("k", tmp_path / "once"))
    res = add(capsys, config(tmp_path, {"cmd": hook}), "--unique-key", "k")
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    assert doc["created"] is False
    stored = jobs()
    assert len(stored) == 1 and doc["job"]["id"] == stored[0]["id"]


def test_the_key_is_rechecked_when_a_taken_id_runs_the_hooks_again(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tracker: HookTracker,
) -> None:
    assert add(capsys, config(tmp_path)).code == 0
    taken = jobs()[0]["id"]
    ids = iter([taken, "jb-cccccccc"])
    monkeypatch.setattr(add_module, "new_job_id", lambda: next(ids))
    hook = script(tmp_path, "racer", inner_add("k", tmp_path / "once"))
    res = add(capsys, config(tmp_path, {"cmd": hook}), "--unique-key", "k")
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    assert doc["created"] is False
    stored = jobs()
    assert len(stored) == 2 and doc["job"]["id"] != taken
    assert "jb-cccccccc" not in {row["id"] for row in stored}


# -- the clock, hook ids, interrupts, escalation size -------------------------------------------


def test_created_at_is_taken_after_the_hooks_ran(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    start = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    later = start + dt.timedelta(hours=1)
    monkeypatch.setattr(gateway, "utc_now", lambda: later if marker.exists() else start)
    hook = script(tmp_path, "h", f"touch {marker}\n")
    assert add(capsys, config(tmp_path, {"cmd": hook})).code == 0
    assert jobs()[0]["created_at"] == "2030-01-01T01:00:00.000Z"


def test_a_hook_id_with_escapes_and_newlines_is_sanitized_in_the_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    cmd = "# \x1b[31mred\x07 \nexit 1"  # an unnamed hook's identity is its cmd
    res = add(capsys, config(tmp_path, {"cmd": cmd}))
    assert res.code == 11, res.out + res.err
    err = error(res)
    for text in (err["details"]["hook_id"], err["message"]):
        assert "\x1b" not in text and "\x07" not in text and "\n" not in text
    assert "red" in err["details"]["hook_id"]


def test_sigint_during_a_hook_stops_it_and_stores_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    pidfile = tmp_path / "pid"
    hook = script(tmp_path, "slow", f"echo $$ > {pidfile}\nexec sleep 60\n")

    def fire() -> None:
        end = time.monotonic() + 20
        while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text().strip()):
            time.sleep(0.02)
        if pidfile.exists() and pidfile.read_text().strip():
            os.kill(os.getpid(), signal.SIGINT)  # this process: the add; the hook is running
        # else: no signal; the exit-code assertion below fails cleanly

    thread = threading.Thread(target=fire)
    thread.start()
    try:
        res = add(capsys, config(tmp_path, {"cmd": hook}))
    finally:
        thread.join()
    assert res.code == 130, res.out + res.err
    assert error(res)["code"] == "interrupted" and jobs() == [] and queues_seen() == []
    assert len(hook_records()) == 1
    assert tracker.last.popen.returncode is not None  # the leader was stopped and reaped
    assert running_pids(recorded_pids(pidfile)) == []  # and its group is gone


def test_several_escalations_are_joined_in_order_and_capped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    hooks = [
        {
            "name": n,
            "cmd": script(tmp_path, n, f"head -c 3000 /dev/zero | tr '\\0' {c} >&2\nexit 13\n"),
        }
        for n, c in (("a", "A"), ("b", "B"), ("c", "C"))
    ]
    res = add(capsys, config(tmp_path, *hooks))
    assert res.code == 13, res.out + res.err
    err = error(res)
    assert err["details"]["hook_ids"] == ["a", "b", "c"]
    assert err["message"].startswith("the pre-add hook a, b, c escalated")
    reasons = err["message"].split(": ", 1)[1]
    assert len(reasons.encode()) <= 4096
    assert reasons.startswith("A") and "B" in reasons and "C" not in reasons  # cut, in order
    assert jobs() == []


# -- decision ids that hold a secret ------------------------------------------------------------


def test_a_decision_id_holding_a_secret_is_dropped_and_never_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"API_TOKEN={SECRET}\n")
    env_file.chmod(0o600)
    seen = tmp_path / "seen_token"
    body = f'printf %s "$API_TOKEN" > {seen}\n' + (
        'echo "{\\"decision_id\\": [\\"$API_TOKEN\\", \\"ok1\\"]}"\n'
    )
    cfg = config(
        tmp_path, {"cmd": script(tmp_path, "h", body)}, env_file=str(env_file),
        mask_env=["API_TOKEN"],
    )  # fmt: skip
    res = add(capsys, cfg)
    assert res.code == 0, res.out + res.err
    assert seen.read_text() == SECRET  # the secret really reached the hook
    log_text = (resolve_state_dir() / "log.jsonl").read_text()
    assert SECRET not in log_text and "***" in log_text  # dropped, echoed masked
    assert hook_records()[0]["decision_ids"] == ["ok1"]


def test_the_joined_escalation_message_is_cut_by_bytes_not_characters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    body = "printf '\\303\\251%.0s' $(seq 1 1500) >&2\nexit 13\n"  # 3000 bytes of "é"
    hooks = [{"name": n, "cmd": script(tmp_path, n, body)} for n in ("a", "b")]
    res = add(capsys, config(tmp_path, *hooks))
    assert res.code == 13, res.out + res.err
    reasons = error(res)["message"].split(": ", 1)[1]
    assert 4000 < len(reasons.encode()) <= 4096


def test_a_long_unnamed_cmd_is_cut_in_the_veto_and_escalation_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tracker: HookTracker
) -> None:
    pad = "é" * 2000  # multi-byte, so the cut must stay UTF-8 safe
    veto = add(capsys, config(tmp_path, {"cmd": f"exit 1 # {pad}"}))
    assert veto.code == 11, veto.out + veto.err
    err = error(veto)
    assert len(err["details"]["hook_id"].encode()) <= 256
    assert len(err["message"].encode()) < 512
    esc = add(capsys, config(tmp_path, {"cmd": f"exit 13 # {pad}"}))
    assert esc.code == 13, esc.out + esc.err
    err = error(esc)
    assert all(len(i.encode()) <= 256 for i in err["details"]["hook_ids"])
    assert len(err["message"].encode()) < 512
    assert jobs() == []


@pytest.mark.parametrize(
    ("seam", "signum", "code"),
    [
        ("after_last_check", signal.SIGINT, 130),
        ("at_release", signal.SIGINT, 130),
        ("at_release", signal.SIGTERM, 1),
    ],
)
def test_a_signal_after_the_last_hook_check_still_ends_the_add_interrupted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    signum: int,
    code: int,
) -> None:
    # The guard's installed handler, called as the OS would call it: the signal is recorded.
    def arrives() -> None:
        handler = signal.getsignal(signum)
        assert callable(handler)
        handler(signum, None)

    if seam == "after_last_check":  # the one hook's check is done; the loop reads `vetoes` next
        real_vetoes = HookResult.vetoes

        def vetoes(self: HookResult) -> bool:
            monkeypatch.setattr(HookResult, "vetoes", real_vetoes)  # once
            arrives()
            return bool(real_vetoes.fget(self))  # type: ignore[union-attr]

        monkeypatch.setattr(HookResult, "vetoes", property(vetoes))
    else:  # the signal lands as the guard is released, after the loop's last read
        real_guard = pre_add.InterruptGuard

        class AtRelease(real_guard):  # type: ignore[valid-type, misc]
            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                arrives()
                super().__exit__(exc_type, exc, tb)  # type: ignore[arg-type]

        monkeypatch.setattr(pre_add, "InterruptGuard", AtRelease)

    res = add(capsys, config(tmp_path, {"cmd": "true"}))
    assert res.code == code, res.out + res.err
    assert error(res)["code"] == ("interrupted" if signum == signal.SIGINT else "internal")
    assert jobs() == [] and queues_seen() == []
