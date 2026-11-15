"""``jobsctl add``: queues, unique keys, warnings, secret refusals, limits, dry run."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jobsctl.commands import add as add_module
from jobsctl.env_file import load_env_file
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.helpers import Result, run_cli

pytestmark = pytest.mark.usefixtures("validated_records")

SECRET = "s3cr3t-VALUE-9f2"


@pytest.fixture(autouse=True)
def trusted_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pytest's tmp dir sits under a world-writable /tmp: trust ancestors up to it."""
    real = load_env_file
    monkeypatch.setattr(
        add_module,
        "load_env_file",
        lambda path: real(path, check_ancestors_from=tmp_path),
    )


@pytest.fixture(autouse=True)
def clean_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH with nothing to drop, so no `path_entry_dropped` record depends on the machine."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def write_config(tmp_path: Path, **keys: Any) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(keys), encoding="utf-8")  # JSON is YAML
    return str(path)


def write_env_file(tmp_path: Path, text: str = f"API_TOKEN={SECRET}\nOTHER=plainvalue\n") -> str:
    path = tmp_path / "secrets.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def add(
    capsys: pytest.CaptureFixture[str], *flags: str, argv: tuple[str, ...] = ("true",)
) -> Result:
    return run_cli(capsys, "--json", "add", *flags, "--cwd", "/work", "--", *argv)


def ok(res: Result) -> dict[str, Any]:
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    return doc


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    conn.row_factory = sqlite3.Row
    return conn


def log_records(event: str | None = None) -> list[dict[str, Any]]:
    path = resolve_state_dir() / "log.jsonl"
    if not path.exists():
        return []
    records = validate_record_file(path, "log.json")
    return [r for r in records if event is None or r["event"] == event]


def table_dump() -> dict[str, list[tuple[Any, ...]]]:
    with db() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n}")] for n in sorted(names)}


# -- the happy path --------------------------------------------------------------------------


def test_add_stores_a_job_and_prints_the_job_object(capsys: pytest.CaptureFixture[str]) -> None:
    doc = ok(add(capsys, "--label", "b=2", "--label", "a=1", "--label", "a=1", argv=("echo", "é")))
    job = doc["job"]
    assert doc["created"] is True and doc["dry_run"] is False and doc["waited"] is False
    assert doc["queue_created"] is True and doc["warnings"] == []
    assert job["status"] == "queued" and job["queue"] == "default" and job["rank"] == 1
    assert job["argv"] == ["echo", "é"] and job["cwd"] == "/work"
    assert job["labels"] == ["a=1", "b=2"]
    assert job["effective"] == {"timeout": None, "max_retries": 0, "kill_grace": 30}
    assert job["overrides"] == {} and job["blocked_by"] is None and job["attempt"] == 0
    assert job["created_at"] == job["enqueued_at"] == job["status_changed_at"]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_human_output_is_only_the_job_id(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "add", "--cwd", "/work", "--", "true")
    assert res.code == 0 and res.err.startswith("jobsctl: warning: the queue default was created")
    assert res.out.startswith("jb-") and res.out.count("\n") == 1 and len(res.out.strip()) == 11


def test_overrides_env_actor_and_decision_ids_are_stored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = ok(
        add(
            capsys,
            "--timeout", "none", "--max-retries", "2", "--kill-grace", "5s",
            "--env", "MODE=fast", "--pass-env", "TOK", "--pass-env", "TOK",
            "--actor", "human:bob", "--decision-id", "d1", "--decision-id", "d2",
            "--priority", "-5", "--pin",
        )
    )  # fmt: skip
    job = doc["job"]
    assert job["overrides"] == {"timeout": "none", "max_retries": 2, "kill_grace": 5}
    assert job["effective"] == {"timeout": None, "max_retries": 2, "kill_grace": 5}
    assert job["env"] == {"MODE": "fast"} and job["pass_env"] == ["TOK"]
    assert job["actor"] == "human:bob" and job["decision_ids"] == ["d1", "d2"]
    assert job["priority"] == -5 and job["pinned"] is True
    assert [w["code"] for w in doc["warnings"]] == ["pass_env_undefined"]


def test_default_actor_without_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    assert ok(add(capsys))["job"]["actor"] == "auto:unknown"


def test_default_cwd_is_the_callers_directory_with_symlinks_kept(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    res = run_cli(capsys, "--json", "add", "--", "true")
    assert ok(res)["job"]["cwd"] == str(link)
    monkeypatch.setenv("PWD", "/elsewhere")  # a stale PWD is not trusted
    res = run_cli(capsys, "--json", "add", "--", "true")
    assert ok(res)["job"]["cwd"] == str(real)


@pytest.mark.parametrize(
    ("given", "stored"),
    [("/a/b/../c/./d", "/a/c/d"), ("//x//y", "/x/y"), ("/..", "/"), ("/a/", "/a")],
)
def test_cwd_is_normalized_lexically(
    capsys: pytest.CaptureFixture[str], given: str, stored: str
) -> None:
    res = run_cli(capsys, "--json", "add", "--cwd", given, "--", "true")
    assert ok(res)["job"]["cwd"] == stored


def test_a_relative_cwd_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "add", "--cwd", "rel/dir", "--", "true")
    assert res.code == 2 and not (resolve_state_dir() / "state.db").exists()


def test_cwd_that_is_not_utf8_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "add", "--cwd", "/bad\udcff", "--", "true")
    assert res.code == 2 and res.envelope()["error"]["code"] == "usage"
    assert "--cwd is not valid UTF-8" in res.envelope()["error"]["message"]
    assert not (resolve_state_dir() / "state.db").exists()


def test_a_default_cwd_that_is_not_utf8_does_not_blame_a_flag(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(add_module, "caller_cwd", lambda: "/bad\udcff")
    res = run_cli(capsys, "--json", "add", "--", "true")
    message = res.envelope()["error"]["message"]
    assert res.code == 2 and "the current directory is not valid UTF-8" in message
    assert "pass --cwd" in message and not message.startswith("--cwd")
    assert not (resolve_state_dir() / "state.db").exists()


def test_hostile_argv_round_trips(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ("run", "'; DROP TABLE jobs;--", "‮", "\udcff", "日本語", "x" * 5000, "")
    doc = ok(add(capsys, argv=argv))
    assert doc["job"]["argv"][:2] == ["run", "'; DROP TABLE jobs;--"]
    assert doc["job"]["argv"][4:] == ["日本語", "x" * 5000, ""]
    # TODO(#11): a lone surrogate prints as the literal text `\\xff`, the same as real
    # `\\xff` text. This pins the current form so a change to it is noticed.
    assert doc["job"]["argv"][3] == "\\xff"
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        stored = json.loads(conn.execute("SELECT argv FROM jobs").fetchone()[0])
    assert stored == list(argv)  # stored losslessly, the lone surrogate included


def test_wait_is_refused_until_wait_exists(capsys: pytest.CaptureFixture[str]) -> None:
    res = add(capsys, "--wait")
    assert res.code == 1 and not (resolve_state_dir() / "state.db").exists()


def test_wait_is_refused_after_the_usage_and_secret_checks(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = secret_cfg(tmp_path)
    res = add(capsys, "--wait", "--config", cfg, "--env", f"MODE={SECRET}")
    assert res.code == 2 and res.envelope()["error"]["details"]["code"] == "secret_in_env"
    assert add(capsys, "--wait", "--config", cfg, "--env", "A=1", "--env", "A=2").code == 2
    assert not (resolve_state_dir() / "state.db").exists()


def test_add_writes_no_audit_record_and_no_transition(capsys: pytest.CaptureFixture[str]) -> None:
    ok(add(capsys))
    assert not (resolve_state_dir() / "audit.jsonl").exists()
    assert log_records("transition") == []


# -- order -----------------------------------------------------------------------------------


def test_new_jobs_go_to_the_end_and_top_goes_first(capsys: pytest.CaptureFixture[str]) -> None:
    first = ok(add(capsys))["job"]
    second = ok(add(capsys))["job"]
    third = ok(add(capsys, "--top"))["job"]
    assert (first["rank"], second["rank"], third["rank"]) == (1, 2, 1)
    with db() as conn:
        ordered = [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY position")]
    assert ordered == [third["id"], first["id"], second["id"]]


def test_priority_and_pin_decide_before_position(capsys: pytest.CaptureFixture[str]) -> None:
    plain = ok(add(capsys))["job"]["id"]
    high = ok(add(capsys, "--priority", "10"))["job"]
    assert high["rank"] == 1
    pinned = ok(add(capsys, "--pin"))["job"]
    assert pinned["rank"] == 1
    final = ok(add(capsys, "--top", "--priority", "10"))["job"]  # top of the priority-10 band
    assert final["rank"] == 2
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE id = ?", [plain]).fetchone()[0] == 1


# -- queues ----------------------------------------------------------------------------------


def test_queue_creation_is_reported_once_and_logged_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = ok(add(capsys, "--queue", "typo-q"))
    second = ok(add(capsys, "--queue", "typo-q"))
    assert first["queue_created"] is True and second["queue_created"] is False
    created = log_records("queue_created")
    assert len(created) == 1
    assert created[0]["queue"] == "typo-q" and created[0]["level"] == "warn"
    assert created[0]["job_id"] == first["job"]["id"]


def test_the_human_queue_warning_names_the_queue(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "add", "--queue", "typo-q", "--cwd", "/work", "--", "true")
    assert res.code == 0 and "the queue typo-q was created on first use" in res.err


def test_a_configured_queue_is_not_a_creation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, queues={"work": {"cap": 3}})
    doc = ok(add(capsys, "--config", cfg, "--queue", "work"))
    assert doc["queue_created"] is False and log_records("queue_created") == []
    with db() as conn:  # still known afterwards
        assert conn.execute("SELECT name FROM queues_seen").fetchall()[0][0] == "work"


@pytest.mark.parametrize("name", ["UPPER", "-lead", "a" * 65, "sp ace", "dot.name", "é"])
def test_invalid_queue_names_are_usage_errors(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    res = add(capsys, "--queue", name)
    assert res.code == 2 and not (resolve_state_dir() / "state.db").exists()


def test_strict_queues_refuses_an_unknown_queue(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, strict_queues=True, queues={"work": {}})
    res = add(capsys, "--config", cfg, "--queue", "oops")
    assert res.code == 10 and res.envelope()["error"]["code"] == "unknown_queue"
    assert log_records() == []
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM queues_seen").fetchone()[0] == 0
    assert ok(add(capsys, "--config", cfg, "--queue", "work"))["queue_created"] is False
    assert add(capsys, "--config", cfg).code == 10  # `default` is not configured either


def test_blocked_by_reports_paused_queues(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, queues={"paused": {"cap": 0}})
    assert ok(add(capsys, "--config", cfg, "--queue", "paused"))["job"]["blocked_by"] == (
        "queue_paused"
    )
    cfg = write_config(tmp_path, global_cap=0)
    assert ok(add(capsys, "--config", cfg))["job"]["blocked_by"] == "global_paused"


def test_pass_env_with_an_unavailable_env_file_is_blocked(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = Path(write_env_file(tmp_path))
    path.chmod(0o644)  # group/other readable: unavailable
    cfg = write_config(tmp_path, env_file=str(path))
    doc = ok(add(capsys, "--config", cfg, "--pass-env", "API_TOKEN"))
    assert doc["job"]["blocked_by"] == "env_file_unavailable"


# -- unique keys -----------------------------------------------------------------------------


def test_a_unique_key_hit_is_decided_before_strict_queues(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, strict_queues=True, queues={"work": {}})
    first = ok(add(capsys, "--config", cfg, "--queue", "work", "--unique-key", "K1"))
    again = ok(add(capsys, "--config", cfg, "--queue", "unconfigured", "--unique-key", "K1"))
    assert again["created"] is False and again["job"]["id"] == first["job"]["id"]
    assert add(capsys, "--config", cfg, "--queue", "unconfigured").code == 10


def test_no_start_is_accepted_and_changes_nothing_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain = ok(add(capsys, "--label", "a=1"))
    started = ok(add(capsys, "--no-start", "--label", "a=1"))
    assert started["warnings"] == plain["warnings"] and started["created"] is True
    assert started["queue_created"] is False and started["dry_run"] is False
    volatile = {"id", "created_at", "enqueued_at", "status_changed_at", "rank"}
    assert {k: v for k, v in started["job"].items() if k not in volatile} == {
        k: v for k, v in plain["job"].items() if k not in volatile
    }
    assert started["job"]["rank"] == plain["job"]["rank"] + 1
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_a_unique_key_hit_creates_nothing_and_ignores_new_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = ok(add(capsys, "--unique-key", "K1", "--priority", "3", "--label", "a=1"))
    second = ok(
        add(
            capsys, "--unique-key", "K1", "--queue", "other", "--priority", "9",
            "--label", "b=2", "--pin", argv=("true",),
        )
    )  # fmt: skip
    assert second["created"] is False and second["queue_created"] is False
    assert second["job"]["id"] == first["job"]["id"]
    assert second["job"]["priority"] == 3 and second["job"]["labels"] == ["a=1"]
    assert second["job"]["queue"] == "default" and second["warnings"] == []
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert [r[0] for r in conn.execute("SELECT name FROM queues_seen")] == ["default"]
    assert log_records("unique_key_reused") == []  # same argv: not logged


def test_a_reuse_with_a_different_argv_is_logged_with_the_hash_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = ok(add(capsys, "--unique-key", "K1", argv=("old",)))
    ok(add(capsys, "--unique-key", "K1", argv=("new-command", "arg")))
    (record,) = log_records("unique_key_reused")
    assert record["job_id"] == first["job"]["id"]
    from jobsctl.log_records import argv_sha256

    assert record["details"]["argv_sha256"] == argv_sha256(["new-command", "arg"])
    assert "new-command" not in json.dumps(record)


def test_a_held_holder_warns(capsys: pytest.CaptureFixture[str]) -> None:
    first = ok(add(capsys, "--unique-key", "K1"))["job"]["id"]
    with db() as conn:
        conn.execute("UPDATE jobs SET status = 'held', hold_reason = 'manual'")
    doc = ok(add(capsys, "--unique-key", "K1"))
    assert doc["job"]["status"] == "held" and doc["job"]["rank"] is None
    assert doc["warnings"] == [
        {
            "code": "existing_job_held",
            "message": f"the job holding this unique key is held ({first})",
            "job_id": first,
        }
    ]


@pytest.mark.parametrize("status", ["done", "failed", "canceled"])
def test_a_terminal_holder_does_not_block(capsys: pytest.CaptureFixture[str], status: str) -> None:
    first = ok(add(capsys, "--unique-key", "K1"))["job"]["id"]
    with db() as conn:
        conn.execute("UPDATE jobs SET status = ?, ended_at = created_at", [status])
    second = ok(add(capsys, "--unique-key", "K1"))
    assert second["created"] is True and second["job"]["id"] != first


@pytest.mark.parametrize("status", ["queued", "starting", "running", "held"])
def test_every_non_terminal_status_holds_the_key(
    capsys: pytest.CaptureFixture[str], status: str
) -> None:
    first = ok(add(capsys, "--unique-key", "K1"))["job"]["id"]
    with db() as conn:
        if status in ("starting", "running"):
            conn.execute(
                "INSERT INTO attempts (job_id, n, claim_id, status, claimed_at, claimed_by,"
                " jobsctl_version) VALUES (?, 1, 'c1', ?, '2026-01-01T00:00:00.000Z', 'pass:1',"
                " '0.0.1')",
                [first, status],
            )
            conn.execute("UPDATE jobs SET attempt = 1")
            conn.execute("UPDATE attempts SET running_at = claimed_at, resolved_path = '/bin/true'")
        conn.execute(
            "UPDATE jobs SET status = ?, hold_reason = ?",
            [status, "manual" if status == "held" else None],
        )
    job = ok(add(capsys, "--unique-key", "K1"))["job"]
    assert job["id"] == first and job["status"] == status


@pytest.mark.parametrize("key", ["", "a b", "k" * 257, "é", "a;b", "a\nb"])
def test_invalid_unique_keys_are_usage_errors(capsys: pytest.CaptureFixture[str], key: str) -> None:
    assert add(capsys, "--unique-key", key).code == 2


def test_racing_adds_with_one_key_create_exactly_one_job(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
    code = (
        "import sys; from jobsctl.cli import main; "
        "sys.exit(main(['--json','add','--unique-key','RACE','--queue','race-q',"
        "'--cwd','/work','--','true']))"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", code],
            env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        for _ in range(8)
    ]  # fmt: skip
    launched = procs
    pgids = [p.pid for p in launched]  # each is its own session and group leader
    outputs: list[dict[str, Any]] = []
    try:
        for proc in launched:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outputs.append(json.loads(out))
    finally:
        for proc, pgid in zip(launched, pgids, strict=True):
            if proc.poll() is None:
                os.killpg(pgid, 9)
                proc.wait()
    for doc in outputs:
        validate_against("cli/add.json", doc)
    assert sum(d["created"] for d in outputs) == 1
    assert sum(d["queue_created"] for d in outputs) == 1
    assert len({d["job"]["id"] for d in outputs}) == 1
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert len(log_records("queue_created")) == 1


def test_racing_adds_without_a_key_get_distinct_ids_and_positions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
    }
    code = (
        "import sys; from jobsctl.cli import main; "
        "sys.exit(main(['--json','add','--cwd','/work','--','true']))"
    )
    launched = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", code],
            env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        for _ in range(8)
    ]  # fmt: skip
    try:
        for proc in launched:
            _, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
    finally:
        for proc in launched:
            if proc.poll() is None:
                os.killpg(proc.pid, 9)
                proc.wait()
    with db() as conn:
        rows = conn.execute("SELECT id, position FROM jobs").fetchall()
    assert len(rows) == 8 and len({r["id"] for r in rows}) == 8
    assert len({r["position"] for r in rows}) == 8


# -- ids -------------------------------------------------------------------------------------


def test_a_pruned_id_is_never_reused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ok(add(capsys))  # creates the database
    with db() as conn:
        conn.execute("INSERT INTO pruned_ids (id) VALUES ('jb-aaaaaaaa')")
    real = add_module.generate_job_id
    draws = iter([b"\x00" * 5, b"\x00" * 5, b"\x01" * 5])  # aaaaaaaa twice, then another

    monkeypatch.setattr(
        add_module,
        "generate_job_id",
        lambda taken: real(taken, random_bytes=lambda n: next(draws)),
    )
    doc = ok(add(capsys))
    assert doc["job"]["id"] != "jb-aaaaaaaa" and doc["job"]["id"].startswith("jb-")


def test_a_live_id_is_not_reused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ok(add(capsys))["job"]["id"]
    real = add_module.generate_job_id
    body = first[3:]
    import base64

    raw = base64.b32decode(body.upper())
    draws = iter([raw, b"\x02" * 5])
    monkeypatch.setattr(
        add_module,
        "generate_job_id",
        lambda taken: real(taken, random_bytes=lambda n: next(draws)),
    )
    assert ok(add(capsys))["job"]["id"] != first


# -- warnings --------------------------------------------------------------------------------


def test_pass_env_undefined_warns_in_output_and_log(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, env_file=write_env_file(tmp_path))
    doc = ok(add(capsys, "--config", cfg, "--pass-env", "API_TOKEN", "--pass-env", "MISSING_ONE"))
    assert [(w["code"], w["variable"]) for w in doc["warnings"]] == [
        ("pass_env_undefined", "MISSING_ONE")
    ]
    (record,) = log_records("pass_env_undefined")
    assert record["details"] == {"variable": "MISSING_ONE"} and record["job_id"]
    assert SECRET not in json.dumps(doc) + json.dumps(record)


def test_pass_env_message_states_the_actual_condition(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = Path(write_env_file(tmp_path))
    path.chmod(0o644)  # unavailable
    cfg = write_config(tmp_path, env_file=str(path))
    unavailable = ok(add(capsys, "--config", cfg, "--pass-env", "API_TOKEN"))["warnings"][0]
    assert "env_file is unavailable" in unavailable["message"]
    assert "not defined" not in unavailable["message"] and "fails" not in unavailable["message"]
    assert unavailable["code"] == "pass_env_undefined" and unavailable["variable"] == "API_TOKEN"
    absent = ok(add(capsys, "--pass-env", "ANY"))["warnings"][0]
    assert "no env_file is configured" in absent["message"]
    path.chmod(0o600)
    defined = ok(add(capsys, "--config", cfg, "--pass-env", "NOPE"))["warnings"][0]
    assert "not defined in env_file" in defined["message"]
    assert "fails" not in defined["message"]


def test_path_entry_dropped_is_logged_once_per_change_of_state(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:relative/bin:/bin")
    ok(add(capsys))
    ok(add(capsys))
    (record,) = log_records("path_entry_dropped")
    assert record["level"] == "warn"
    assert record["details"] == {"entries": [{"reason": "relative", "path": "relative/bin"}]}
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    ok(add(capsys))  # the condition cleared: nothing new, and it re-arms
    monkeypatch.setenv("PATH", "/usr/bin:relative/bin:/bin")
    ok(add(capsys))
    assert len(log_records("path_entry_dropped")) == 2


def test_dry_run_does_not_log_path_entry_dropped(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:relative/bin")
    ok(add(capsys, "--dry-run"))
    assert not resolve_state_dir().exists()


def test_pass_env_without_an_env_file_warns(capsys: pytest.CaptureFixture[str]) -> None:
    doc = ok(add(capsys, "--pass-env", "ANY"))
    assert doc["warnings"][0]["variable"] == "ANY"


def test_env_file_state_warnings_are_logged_once_per_change(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = Path(write_env_file(tmp_path, "GOOD=value-one\nbad line\nPATH=/x\n"))
    cfg = write_config(tmp_path, env_file=str(path))
    ok(add(capsys, "--config", cfg))
    ok(add(capsys, "--config", cfg))
    assert len(log_records("env_file_line_invalid")) == 1
    assert len(log_records("env_file_line_ignored")) == 1
    assert log_records("env_file_line_invalid")[0]["details"] == {"lines": [2]}
    path.write_text("GOOD=value-one\nPATH=/x\n", encoding="utf-8")
    ok(add(capsys, "--config", cfg))
    assert len(log_records("env_file_line_invalid")) == 1  # the condition cleared: nothing new
    path.write_text("GOOD=value-one\nbad line\nPATH=/x\n", encoding="utf-8")
    ok(add(capsys, "--config", cfg))
    assert len(log_records("env_file_line_invalid")) == 2  # re-armed
    path.chmod(0o666)
    ok(add(capsys, "--config", cfg))
    ok(add(capsys, "--config", cfg))
    assert len(log_records("env_file_unsafe")) == 1
    assert "value-one" not in (resolve_state_dir() / "log.jsonl").read_text()


# -- log failures after the commit -----------------------------------------------------------


def test_a_log_failure_after_the_commit_does_not_fail_the_add(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ok(add(capsys, "--unique-key", "seed"))
    log = resolve_state_dir() / "log.jsonl"
    log.unlink()
    log.mkdir()  # appends to log.jsonl now fail
    res = add(capsys, "--queue", "fresh-q", "--unique-key", "K2")
    doc = ok(res)
    assert doc["created"] is True
    with db() as conn:
        stored = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE unique_key = 'K2'")]
        assert conn.execute("SELECT 1 FROM queues_seen WHERE name = 'fresh-q'").fetchone()
    assert stored == [doc["job"]["id"]]
    assert "log.jsonl" in res.err and "the job was added" in res.err
    human = run_cli(capsys, "add", "--queue", "fresh-q2", "--cwd", "/work", "--", "true")
    assert human.code == 0 and human.out.startswith("jb-") and "log.jsonl" in human.err


# -- secrets ---------------------------------------------------------------------------------


def secret_cfg(tmp_path: Path) -> str:
    return write_config(tmp_path, env_file=write_env_file(tmp_path), mask_env=["MASKED_VAR"])


def assert_no_value(res: Result, *values: str) -> None:
    for value in values:
        assert value not in res.out and value not in res.err


def test_a_secret_in_argv_is_refused_by_index(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = secret_cfg(tmp_path)
    res = add(capsys, "--config", cfg, argv=("curl", "-H", f"Authorization: {SECRET}"))
    assert res.code == 2
    error = res.envelope()["error"]
    assert error["code"] == "usage"
    assert error["details"] == {"code": "secret_in_argv", "index": 2}
    validate_against("cli/error.json", res.envelope())
    assert_no_value(res, SECRET)
    assert not (resolve_state_dir() / "state.db").exists()
    assert not (resolve_state_dir() / "log.jsonl").exists()


def test_a_masked_value_from_mask_env_in_argv_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MASKED_VAR", "another-secret-77")
    cfg = write_config(tmp_path, mask_env=["MASKED_VAR"], env={"inherit": ["MASKED_VAR"]})
    res = add(capsys, "--config", cfg, argv=("x", "pre-another-secret-77-post"))
    assert res.code == 2 and res.envelope()["error"]["details"]["index"] == 1
    assert_no_value(res, "another-secret-77")


def test_a_short_value_is_not_a_secret(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, env_file=write_env_file(tmp_path, "PIN=abc1234\n"))  # 7 chars
    assert ok(add(capsys, "--config", cfg, argv=("echo", "abc1234")))["created"] is True


def test_a_secret_in_an_env_value_is_refused_by_variable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = secret_cfg(tmp_path)
    res = add(capsys, "--config", cfg, "--env", f"HEADER=Bearer {SECRET}")
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_env", "variable": "HEADER"}
    assert_no_value(res, SECRET)


@pytest.mark.parametrize("name", ["API_TOKEN", "OTHER", "MASKED_VAR"])
def test_an_env_name_in_env_file_or_mask_env_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, name: str
) -> None:
    cfg = secret_cfg(tmp_path)
    res = add(capsys, "--config", cfg, "--env", f"{name}=harmless")
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_env", "variable": name}
    assert "--pass-env" in res.envelope()["error"]["message"]
    assert_no_value(res, "harmless")


def test_a_clean_add_passes_the_secret_checks(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = secret_cfg(tmp_path)
    doc = ok(add(capsys, "--config", cfg, "--env", "MODE=fast", "--pass-env", "API_TOKEN"))
    assert doc["warnings"] == []


def test_duplicate_and_overlapping_env_names_are_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert add(capsys, "--env", "A=1", "--env", "A=2").code == 2
    assert add(capsys, "--env", "A=1", "--pass-env", "A").code == 2
    assert not (resolve_state_dir() / "state.db").exists()


def test_an_env_error_never_echoes_the_value(capsys: pytest.CaptureFixture[str]) -> None:
    for flags in (["--env", "JOBSCTL_X=topsecretvalue1"], ["--env", "1BAD=topsecretvalue1"],
                  ["--env", "NOEQUALS"], ["--pass-env", "NAME=topsecretvalue1"]):  # fmt: skip
        res = add(capsys, *flags)
        assert res.code == 2
        assert_no_value(res, "topsecretvalue1")


# -- limits ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "argv"),
    [
        ([], ()),
        ([], ("a\x00b",)),
        ([], ("x",) * 4097),
        ([], ("y" * 1024,) * 1025),
        (sum((["--label", f"k{i}=v"] for i in range(33)), []), ("x",)),
        (sum((["--env", f"V{i}=1"] for i in range(257)), []), ("x",)),
        (sum((["--pass-env", f"V{i}"] for i in range(257)), []), ("x",)),
        (["--env", "BIG=" + "x" * (128 * 1024 + 1)], ("x",)),
        (["--timeout", "0s"], ("x",)),
        (["--timeout", "banana"], ("x",)),
        (["--max-retries", "101"], ("x",)),
        (["--kill-grace", "none"], ("x",)),
        (["--priority", "1001"], ("x",)),
    ],
    ids=[
        "empty", "nul", "4097-args", "over-1MiB", "33-labels", "257-env", "257-pass-env",
        "env-value-128k", "zero-timeout", "bad-timeout", "retries-101", "kill-grace-none",
        "priority-1001",
    ],
)  # fmt: skip
def test_limits_and_invalid_values_exit_2_and_store_nothing(
    capsys: pytest.CaptureFixture[str], flags: list[str], argv: tuple[str, ...]
) -> None:
    res = run_cli(capsys, "--json", "add", *flags, "--cwd", "/work", "--", *argv)
    assert res.code == 2, res.out[:300]
    assert not (resolve_state_dir() / "state.db").exists()


def test_the_limits_themselves_are_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    flags: list[str] = []
    for i in range(32):
        flags += ["--label", f"k{i}=v"]
    for i in range(256):
        flags += ["--env", f"V{i}=1", "--pass-env", f"P{i}"]
    flags += ["--env", "BIG=" + "x" * (128 * 1024)]
    # 256 --env entries including BIG would be 257: drop one
    flags = [f for f in flags if f != "V0=1"]
    flags.pop(flags.index("--env"))
    doc = ok(add(capsys, *flags, argv=("y",) * 4096))
    assert len(doc["job"]["argv"]) == 4096 and len(doc["job"]["labels"]) == 32
    assert len(doc["job"]["pass_env"]) == 256 and len(doc["job"]["env"]) == 256


# -- dry run ---------------------------------------------------------------------------------


def test_dry_run_without_a_state_directory_creates_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = ok(add(capsys, "--dry-run", "--queue", "brand-new", "--pass-env", "NOPE"))
    assert doc["job"] is None and doc["created"] is False and doc["dry_run"] is True
    assert doc["queue_created"] is False
    assert [w["code"] for w in doc["warnings"]] == ["pass_env_undefined"]
    assert not resolve_state_dir().exists()


def test_dry_run_leaves_a_zero_byte_database_alone(capsys: pytest.CaptureFixture[str]) -> None:
    state = resolve_state_dir()
    state.mkdir(parents=True, mode=0o700)
    (state / "state.db").touch(mode=0o600)
    doc = ok(add(capsys, "--dry-run"))
    assert doc["job"] is None and doc["created"] is False
    assert (state / "state.db").stat().st_size == 0
    assert sorted(p.name for p in state.iterdir()) == ["state.db"]  # no shims, diag, wal


def test_dry_run_does_not_recreate_shims_or_diag(capsys: pytest.CaptureFixture[str]) -> None:
    ok(add(capsys, "--unique-key", "K1"))
    state = resolve_state_dir()
    (state / "shims").rmdir()
    (state / "diag").rmdir()
    doc = ok(add(capsys, "--dry-run", "--unique-key", "K1"))
    assert doc["job"] is not None
    assert not (state / "shims").exists() and not (state / "diag").exists()


def test_dry_run_works_on_a_read_only_database(capsys: pytest.CaptureFixture[str]) -> None:
    first = ok(add(capsys, "--unique-key", "K1"))["job"]
    db_path = resolve_state_dir() / "state.db"
    db_path.chmod(0o400)
    mode_before = db_path.stat().st_mode
    doc = ok(add(capsys, "--dry-run", "--unique-key", "K1"))
    assert doc["job"]["id"] == first["id"]
    assert db_path.stat().st_mode == mode_before


def test_dry_run_does_not_tighten_the_state_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ok(add(capsys))
    state = resolve_state_dir()
    state.chmod(0o750)
    ok(add(capsys, "--dry-run"))
    assert state.stat().st_mode & 0o777 == 0o750


def test_dry_run_refuses_an_older_schema_and_never_migrates(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl import store_db

    ok(add(capsys))
    path = resolve_state_dir() / "state.db"
    before = path.read_bytes()
    newer = (*store_db.MIGRATIONS, ("CREATE TABLE migrated_marker (x)",))
    monkeypatch.setattr(store_db, "MIGRATIONS", newer)
    res = add(capsys, "--dry-run")
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    assert "older" in res.envelope()["error"]["message"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store_db.MIGRATIONS) - 1
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migrated_marker'"
        ).fetchone()
    assert path.read_bytes() == before


def test_dry_run_leaves_the_database_and_files_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ok(add(capsys, "--unique-key", "K1"))
    before = table_dump()
    files = {p.name: p.read_bytes() for p in resolve_state_dir().glob("*.jsonl")}
    for flags in (
        ["--queue", "never-used", "--label", "a=1", "--top", "--pin"],
        ["--unique-key", "NEWKEY", "--pass-env", "NOPE"],
    ):
        doc = ok(add(capsys, "--dry-run", *flags))
        assert doc["job"] is None and doc["created"] is False and doc["queue_created"] is False
    assert table_dump() == before
    assert {p.name: p.read_bytes() for p in resolve_state_dir().glob("*.jsonl")} == files


def test_dry_run_on_a_key_hit_returns_the_existing_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = ok(add(capsys, "--unique-key", "K1", argv=("old",)))["job"]
    before = table_dump()
    files = {p.name: p.read_bytes() for p in resolve_state_dir().glob("*.jsonl")}
    doc = ok(add(capsys, "--dry-run", "--unique-key", "K1", argv=("new",)))
    assert doc["job"]["id"] == first["id"] and doc["created"] is False
    assert doc["dry_run"] is True
    assert table_dump() == before
    assert {p.name: p.read_bytes() for p in resolve_state_dir().glob("*.jsonl")} == files


def test_dry_run_still_applies_refusals(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, strict_queues=True)
    assert add(capsys, "--dry-run", "--config", cfg, "--queue", "nope").code == 10
    cfg = secret_cfg(tmp_path)
    assert add(capsys, "--dry-run", "--config", cfg, "--env", "API_TOKEN=x").code == 2
    assert not resolve_state_dir().exists()


def test_dry_run_with_wait_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert add(capsys, "--dry-run", "--wait").code == 2


def test_dry_run_human_output_prints_no_id_for_a_new_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "add", "--dry-run", "--cwd", "/work", "--", "true")
    assert res.code == 0 and res.out == ""
