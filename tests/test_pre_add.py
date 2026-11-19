"""The `pre-add` hook (interface.md, Hooks and `add`): real hook scripts run by `add` before the
job is stored. A veto exits 11 (`vetoed`), an escalation exits 13 (`needs_human`), both store
nothing; a failing, timed-out or killed hook vetoes (fail closed)."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from jobsctl import pre_add
from jobsctl.commands import add as add_module
from jobsctl.env_file import load_env_file
from jobsctl.hook_run import HookResult
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.hook_real import HookTracker, tracker_fixture
from tests.helpers import Result, run_cli

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
        f'cat > {seen}/payload.json\npwd > {seen}/cwd\n'
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
    res = add(capsys, config(tmp_path, {"cmd": hook, "timeout": 1}))
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
    res = add(
        capsys, config(tmp_path, {"name": "a", "cmd": first}, {"name": "b", "cmd": second})
    )
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
    res = add(
        capsys, config(tmp_path, {"name": "a", "cmd": first}, {"name": "b", "cmd": second})
    )
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
    cfg = config(tmp_path, {"cmd": hook})
    first = add(capsys, cfg, "--unique-key", "k")
    assert first.code == 0 and marker.read_text() == "x\n"
    veto = script(tmp_path, "veto", "exit 1\n")
    cfg2 = config(tmp_path, {"cmd": veto})
    again = add(capsys, cfg2, "--unique-key", "k")
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
    cfg = config(tmp_path, {"cmd": veto})
    first = add(capsys, config(tmp_path), "--unique-key", "k")
    assert first.code == 0
    job_id = jobs()[0]["id"]
    assert run_cli(capsys, "--json", "cancel", job_id).code == 0  # the key is free again
    res = add(capsys, cfg, "--unique-key", "k")
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
    hook = script(tmp_path, "h", f'echo "denied for $API_TOKEN" >&2\nexit 1\n')
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


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"stopped": "SIGTERM"}, 1),
        ({"unkillable": True}, 1),
    ],
)
def test_a_stopped_or_unkillable_hook_stores_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    over: dict[str, Any],
    code: int,
) -> None:
    real = pre_add.Deliverer

    class Fake(real):  # type: ignore[valid-type, misc]
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.runner = _Fake(**over)

    monkeypatch.setattr(pre_add, "Deliverer", Fake)
    res = add(capsys, config(tmp_path, {"cmd": "true"}))
    assert res.code == code, res.out + res.err
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
