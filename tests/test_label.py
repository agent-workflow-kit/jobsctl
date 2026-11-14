"""`jobsctl label add` and `label remove`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jobsctl.commands import label as label_module
from jobsctl.env_file import load_env_file
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.helpers import Result, run_cli
from tests.view_helpers import (
    ENDED,
    check_golden,
    freeze_clock,
    running_attempt,
    seed_finished,
    seed_job,
)

pytestmark = pytest.mark.usefixtures("validated_records")
SECRET = "s3cr3t-VALUE-9f2"
JOB = "jb-aaaaaa22"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    freeze_clock(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    real = load_env_file
    monkeypatch.setattr(
        label_module, "load_env_file", lambda path: real(path, check_ancestors_from=tmp_path)
    )


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> Result:
    return run_cli(capsys, "--json", *argv)


def change(res: Result) -> dict[str, Any]:
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/change.json", doc)
    return doc


def stored(job_id: str = JOB) -> list[str]:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    try:
        rows = conn.execute("SELECT key, value FROM job_labels WHERE job_id = ?", [job_id])
        return sorted(f"{k}={v}" for k, v in rows)
    finally:
        conn.close()


def log_records() -> list[dict[str, Any]]:
    path = resolve_state_dir() / "log.jsonl"
    return validate_record_file(path, "log.json") if path.exists() else []


def label_records() -> list[dict[str, Any]]:
    return [r for r in log_records() if r["event"] == "label"]


def secret_config(tmp_path: Path) -> str:
    env = tmp_path / "secrets.env"
    env.write_text(f"API_TOKEN={SECRET}\n", encoding="utf-8")
    env.chmod(0o600)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"env_file": str(env)}), encoding="utf-8")
    return str(cfg)


def test_add_and_remove_change_the_set_and_print_sorted_labels(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job(JOB, labels=[("b", "2")])
    doc = change(run(capsys, "label", "add", JOB, "z=1", "a=9", "a=1"))
    assert doc["changed"] is True and doc["dry_run"] is False
    assert doc["job"]["labels"] == ["a=1", "a=9", "b=2", "z=1"]
    assert stored() == ["a=1", "a=9", "b=2", "z=1"]
    doc = change(run(capsys, "label", "remove", JOB, "a=9", "b=2"))
    assert doc["changed"] is True and doc["job"]["labels"] == ["a=1", "z=1"]
    assert stored() == ["a=1", "z=1"]


def test_noops_report_changed_false_and_log_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB, labels=[("a", "1")])
    assert change(run(capsys, "label", "add", JOB, "a=1", "a=1"))["changed"] is False
    assert change(run(capsys, "label", "remove", JOB, "a=2", "zz=1"))["changed"] is False
    assert stored() == ["a=1"] and label_records() == []


def test_a_partly_new_add_changes_and_logs_one_record(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB, labels=[("a", "1")])
    res = run(
        capsys,
        "label",
        "add",
        JOB,
        "a=1",
        "b=2",
        "--actor",
        "human:me",
        "--decision-id",
        "d1",
        "--decision-id",
        "d2",
    )
    assert change(res)["changed"] is True
    (record,) = label_records()
    assert record["job_id"] == JOB and record["queue"] == "q"
    assert record["actor"] == "human:me" and record["decision_ids"] == ["d1", "d2"]
    assert record["details"] == {"op": "add", "labels": ["b=2"]}  # only what changed


def test_remove_logs_what_was_removed(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB, labels=[("a", "1"), ("b", "2")])
    change(run(capsys, "label", "remove", JOB, "b=2", "c=3"))
    (record,) = label_records()
    assert record["details"] == {"op": "remove", "labels": ["b=2"]}


def test_any_status_can_be_labeled(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", status="running", attempt=1, attempts=[running_attempt()])
    seed_finished("jb-aaaaaa23")
    seed_job("jb-aaaaaa24", status="held", hold_reason="manual")
    seed_job("jb-aaaaaa25", status="canceled", reason="canceled", ended_at=ENDED)
    for job_id in ("jb-aaaaaa22", "jb-aaaaaa23", "jb-aaaaaa24", "jb-aaaaaa25"):
        assert change(run(capsys, "label", "add", job_id, "k=v"))["job"]["labels"] == ["k=v"]


def test_limit_of_32_labels_counts_the_resulting_set(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB, labels=[(f"k{i}", "v") for i in range(31)])
    res = run(capsys, "label", "add", JOB, "a=1", "b=2")  # 33
    assert res.code == 2
    err = res.envelope()["error"]
    assert err["code"] == "usage" and err["details"]["limit"] == 32
    assert len(stored()) == 31  # nothing applied
    assert change(run(capsys, "label", "add", JOB, "k0=v", "a=1"))["job"]["labels"].__len__() == 32
    assert run(capsys, "label", "add", JOB, "b=2").code == 2  # the 33rd
    assert change(run(capsys, "label", "add", JOB, "a=1"))["changed"] is False  # existing pair
    doc = change(run(capsys, "label", "remove", JOB, "a=1"))
    assert len(doc["job"]["labels"]) == 31


def test_a_request_of_more_than_32_distinct_labels_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job(JOB)
    res = run(capsys, "label", "add", JOB, *[f"k{i}=v" for i in range(33)])
    assert res.code == 2 and res.envelope()["error"]["details"]["limit"] == 32


def test_the_limit_applies_to_a_dry_run_too(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB, labels=[(f"k{i}", "v") for i in range(32)])
    assert run(capsys, "label", "add", JOB, "extra=1", "--dry-run").code == 2


def test_lookup_errors(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22")
    seed_job("jb-aaaaaa23")
    res = run(capsys, "label", "add", "jb-cccccc22", "a=1")
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"
    res = run(capsys, "label", "remove", "aaaa", "a=1")
    assert res.code == 21 and len(res.envelope()["error"]["candidates"]) == 2
    assert run(capsys, "label", "add", "jb-aaaaaa22", "bad key=1").code == 2
    assert run(capsys, "label", "add", "jb-aaaaaa22").code == 2


def test_prefix_ids_work(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB)
    assert change(run(capsys, "label", "add", "aaaa", "a=1"))["job"]["id"] == JOB


def test_dry_run_projects_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_job(JOB, labels=[("a", "1")])
    modes: list[OpenMode] = []
    real = Store.open.__func__  # type: ignore[attr-defined]

    def spy(cls: Any, *args: Any, **kw: Any) -> Any:
        modes.append(kw.get("mode", OpenMode.CLI))
        return real(cls, *args, **kw)

    monkeypatch.setattr(Store, "open", classmethod(spy))
    db = resolve_state_dir() / "state.db"
    before = db.stat().st_mtime_ns
    doc = change(run(capsys, "label", "add", JOB, "b=2", "--dry-run"))
    assert doc["changed"] is True and doc["dry_run"] is True
    assert doc["job"]["labels"] == ["a=1", "b=2"]
    doc = change(run(capsys, "label", "remove", JOB, "a=1", "--dry-run"))
    assert doc["job"]["labels"] == [] and doc["dry_run"] is True
    assert change(run(capsys, "label", "add", JOB, "a=1", "--dry-run"))["changed"] is False
    assert modes and all(m is OpenMode.READ_ONLY for m in modes)
    assert stored() == ["a=1"] and label_records() == []
    assert db.stat().st_mtime_ns == before


def test_dry_run_without_a_state_dir_is_not_found_and_creates_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run(capsys, "label", "add", JOB, "a=1", "--dry-run")
    assert res.code == 10 and not resolve_state_dir().exists()


def test_human_output_is_the_job_id(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(JOB)
    res = run_cli(capsys, "label", "add", JOB, "a=1")
    assert res.code == 0 and res.out == f"{JOB}\n"
    res = run_cli(capsys, "label", "add", JOB, "a=1")
    assert res.out == f"{JOB} (unchanged)\n"


def test_golden_change_document(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        JOB,
        labels=[("owner", "ticket-work")],
        argv=["echo"],
        cwd="/work",
        created_at="2026-03-10T10:00:00.000Z",
        enqueued_at="2026-03-10T10:00:00.000Z",
        status_changed_at="2026-03-10T10:00:00.000Z",
    )
    check_golden("label_add.json", change(run(capsys, "label", "add", JOB, "extra=1")))


# -- secrets ---------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["add", "remove"])
def test_a_secret_in_a_label_value_is_refused_naming_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, action: str
) -> None:
    cfg = secret_config(tmp_path)
    seed_job(JOB, labels=[("a", "1")])
    res = run(capsys, "--config", cfg, "label", action, JOB, f"note={SECRET}")
    assert res.code == 2 and SECRET not in res.out + res.err
    err = res.envelope()["error"]
    validate_against("cli/error.json", res.envelope())
    assert err["code"] == "usage"
    assert err["details"] == {"code": "secret_in_label", "label": "note"}
    assert stored() == ["a=1"] and label_records() == []


@pytest.mark.parametrize("action", ["add", "remove"])
def test_a_secret_in_a_label_key_is_refused_without_naming_it(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    cfg = tmp_path / "config.yaml"
    secret_key = "tokenabc12345"
    cfg.write_text(
        json.dumps({"mask_env": ["MYSECRET"], "env": {"inherit": ["MYSECRET"]}}), encoding="utf-8"
    )
    monkeypatch.setenv("MYSECRET", secret_key)
    seed_job(JOB)
    res = run(capsys, "--config", str(cfg), "label", action, JOB, f"{secret_key}=v")
    assert res.code == 2 and secret_key not in res.out + res.err
    err = res.envelope()["error"]
    assert err["details"] == {"code": "secret_in_label"}


def test_the_secret_check_precedes_the_lookup_and_the_dry_run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = secret_config(tmp_path)
    res = run(capsys, "--config", cfg, "label", "add", "jb-cccccc22", f"n={SECRET}", "--dry-run")
    assert res.code == 2 and res.envelope()["error"]["details"]["code"] == "secret_in_label"
