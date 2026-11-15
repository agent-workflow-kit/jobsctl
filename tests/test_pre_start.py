"""The `pre-start` hooks in the shim (interface.md, Start sequence step 3 and Hooks)."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import triggers
from jobsctl.config import Config
from jobsctl.hook_run import HookResult, HookTuning
from jobsctl.store_db import OpenMode, Store
from tests.conftest import validate_against
from tests.delivery_helpers import fake_result, pending
from tests.shim_helpers import CLAIM, JOB, attempt, job, log_records, make_shim, seed_claimed
from tests.test_shim_delivery import put_event

FAST = HookTuning(term_grace=0.3, kill_wait=1.0, drain=0.5, stop_interval=0.1)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(state_dir: Any, config: Any, path: Any, command: str, args: Any = ()) -> Any:
        seen.append(command)
        return None

    monkeypatch.setattr(triggers, "spawn_internal", fake)
    return seen


def config(*pre: dict[str, Any], **hooks: Any) -> Config:
    return Config.model_validate({"hooks": {"pre-start": list(pre), **hooks}})


def sh(name: str, cmd: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "cmd": cmd, **extra}


def marker_job(tmp_path: Path) -> tuple[list[str], Path]:
    marker = tmp_path / "job-ran"
    return ["sh", "-c", f"touch {marker}"], marker


def set_override(store: Store, *ids: str) -> None:
    with store.write() as tx:
        tx.execute("UPDATE jobs SET override_hooks = ? WHERE id = ?", [json.dumps(ids), JOB])


def messages(store: Store) -> list[dict[str, Any]]:
    return json.loads(attempt(store)["hook_messages"])


def test_no_pre_start_hook_starts_the_job(store: Store, tmp_path: Path) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    assert make_shim(store).run() == 0
    assert marker.exists() and job(store)["status"] == "done"


def test_allowing_hooks_run_in_order_record_decision_ids_and_the_job_starts(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    trace = tmp_path / "trace"
    seed_claimed(store, argv)
    cfg = config(
        sh("a", f'echo a >> {trace}; echo \'{{"decision_id": ["d1", "d2"]}}\''),
        sh("b", f'echo b >> {trace}; echo \'{{"decision_id": "d1"}}\''),
    )
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert trace.read_text().split() == ["a", "b"]
    assert marker.exists() and job(store)["status"] == "done"
    assert json.loads(attempt(store)["decision_ids"]) == ["d1", "d2"]
    ran = [r for r in log_records("hook_ran") if r["hook"] == "pre-start"]
    assert [(r["hook_id"], r["outcome"]) for r in ran] == [("a", "allow"), ("b", "allow")]


def test_a_veto_cancels_the_job_with_vetoed_by_and_messages_and_stops_the_loop(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    after = tmp_path / "after"
    trace = tmp_path / "post"
    seed_claimed(store, argv)
    cmd = f"python3 -c \"import sys,json; print(json.load(sys.stdin)['hook'])\" >> {trace}"
    cfg = config(
        sh("gate", "echo 'no way' >&2; echo '{\"decision_id\": \"dv\"}'; exit 1"),
        sh("later", f"touch {after}"),
        **{"post-cancel": [sh("pc", cmd)]},
    )
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "vetoed")
    assert row["ended_at"] is not None and row["hold_reason"] is None
    att = attempt(store)
    assert (att["status"], att["reason"], att["vetoed_by"]) == ("canceled", "vetoed", "gate")
    assert messages(store) == [{"hook_id": "gate", "outcome": "veto", "message": "no way\n"}]
    assert json.loads(att["decision_ids"]) == ["dv"]
    assert json.loads(att["escalated_hooks"]) == []
    assert not marker.exists() and not after.exists()
    assert trace.read_text().split() == ["post-cancel"]  # delivered by the shim
    assert pending(store) == []
    (record,) = [r for r in log_records("transition") if r["to"] == "canceled"]
    assert (record["from"], record["reason"]) == ("starting", "vetoed")


def test_an_escalation_holds_the_job_runs_the_other_hooks_and_records_who(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    second = tmp_path / "second"
    seed_claimed(store, argv)
    cfg = config(
        sh("ask", "echo 'need a human' >&2; echo '{\"decision_id\": \"de\"}'; exit 13"),
        sh("also", f"touch {second}"),
    )
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    row = job(store)
    assert (row["status"], row["hold_reason"]) == ("held", "escalated")
    assert row["override_hooks"] == "[]" and row["not_before"] is None
    att = attempt(store)
    assert (att["status"], att["hold_reason"]) == ("held", "escalated")
    assert att["ended_at"] is not None and att["vetoed_by"] is None
    assert json.loads(att["escalated_hooks"]) == ["ask"]
    assert messages(store) == [
        {"hook_id": "ask", "outcome": "escalate", "message": "need a human\n"}
    ]
    assert json.loads(att["decision_ids"]) == ["de"]
    assert second.exists() and not marker.exists()
    ran = {r["hook_id"]: r["outcome"] for r in log_records("hook_ran")}
    assert ran == {"ask": "escalate", "also": "allow"}


def test_a_veto_wins_over_an_earlier_escalation_and_both_are_recorded(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    cfg = config(sh("ask", "echo e >&2; exit 13"), sh("gate", "echo v >&2; exit 2"))
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "vetoed")
    att = attempt(store)
    assert att["vetoed_by"] == "gate" and json.loads(att["escalated_hooks"]) == ["ask"]
    assert [(m["hook_id"], m["outcome"]) for m in messages(store)] == [
        ("ask", "escalate"),
        ("gate", "veto"),
    ]
    assert not marker.exists()


def test_a_timeout_vetoes_but_hook_ran_keeps_the_timeout_outcome(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    cfg = config(sh("slow", "echo hurry >&2; sleep 30", timeout="1s"))
    started = time.monotonic()
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert time.monotonic() - started < 10
    assert (job(store)["status"], job(store)["reason"]) == ("canceled", "vetoed")
    (m,) = messages(store)
    assert (m["hook_id"], m["outcome"]) == ("slow", "veto")
    (ran,) = log_records("hook_ran")
    assert ran["outcome"] == "timeout"
    assert not marker.exists()


def test_a_hook_killed_by_a_signal_vetoes(store: Store, tmp_path: Path) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    cfg = config(sh("boom", "kill -9 $$"))
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("canceled", "vetoed")
    assert attempt(store)["vetoed_by"] == "boom" and not marker.exists()


def test_a_hook_that_cannot_be_started_vetoes(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    shim = make_shim(store, config(sh("nostart", "true")), hook_tuning=FAST)
    from jobsctl import gateway

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "no such directory")

    monkeypatch.setattr(gateway, "spawn", refuse)
    assert shim.run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("canceled", "vetoed")
    assert "could not be started" in messages(store)[0]["message"]
    assert not marker.exists()


def test_a_hook_that_outlives_sigkill_is_an_internal_error_and_the_job_is_not_started(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    stuck = fake_result("pre-start", "stuck")
    stuck = HookResult(**{**stuck.__dict__, "outcome": "failed", "unkillable": True})

    def runner(**kwargs: Any) -> HookResult:
        return stuck

    shim = make_shim(store, config(sh("stuck", "true")), hook_runner=runner)
    assert shim.run() == 1
    assert job(store)["status"] == "starting"  # reconcile's business; nothing was started
    assert not marker.exists()


# -- override_hooks ---------------------------------------------------------------------------


def test_override_hooks_run_but_their_exit_13_counts_as_allow_and_is_then_cleared(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    set_override(store, "ask")
    cfg = config(sh("ask", 'echo \'{"decision_id": "dd"}\'; exit 13'))
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert marker.exists() and job(store)["status"] == "done"
    assert job(store)["override_hooks"] == "[]"
    assert json.loads(attempt(store)["decision_ids"]) == ["dd"]  # the hook ran
    assert json.loads(attempt(store)["escalated_hooks"]) == [] and messages(store) == []


def test_a_hook_not_in_override_hooks_still_escalates(store: Store, tmp_path: Path) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    set_override(store, "ask")
    cfg = config(sh("ask", "exit 13"), sh("other", "exit 13"))
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert (job(store)["status"], job(store)["hold_reason"]) == ("held", "escalated")
    assert json.loads(attempt(store)["escalated_hooks"]) == ["other"]
    assert job(store)["override_hooks"] == "[]" and not marker.exists()


def test_a_veto_still_vetoes_under_override(store: Store, tmp_path: Path) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    set_override(store, "gate")
    assert make_shim(store, config(sh("gate", "exit 1")), hook_tuning=FAST).run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("canceled", "vetoed")
    assert job(store)["override_hooks"] == "[]" and not marker.exists()


def test_override_hooks_are_cleared_even_when_no_hook_is_configured(
    store: Store, tmp_path: Path
) -> None:
    argv, _ = marker_job(tmp_path)
    seed_claimed(store, argv)
    set_override(store, "gone")
    assert make_shim(store, config(), hook_tuning=FAST).run() == 0
    assert job(store)["override_hooks"] == "[]"


def test_override_hooks_survive_a_start_that_did_not_run_the_hooks(
    store: Store, tmp_path: Path
) -> None:
    """SIGTERM during the first hook: the attempt fails `terminated`, is requeued by automatic
    retry and keeps the override for the next start."""
    started = tmp_path / "started"
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv, overrides={"max_retries": 2})
    set_override(store, "ask")
    shim = make_shim(store, config(sh("ask", f"touch {started}; sleep 30")), hook_tuning=FAST)
    threading.Thread(target=lambda: (_wait_for(started), shim.request_stop())).start()
    assert shim.run() == 0
    row = job(store)
    assert (row["status"], row["auto_retries"]) == ("queued", 1)
    assert json.loads(row["override_hooks"]) == ["ask"]
    assert attempt(store)["reason"] == "terminated" and not marker.exists()


def test_a_terminal_failure_clears_the_override(store: Store, tmp_path: Path) -> None:
    seed_claimed(store, ["/nonexistent/binary"])
    set_override(store, "ask")
    assert make_shim(store, config(sh("ask", "true")), hook_tuning=FAST).run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "spawn_failed")
    assert job(store)["override_hooks"] == "[]"


# -- stopping -----------------------------------------------------------------------------------


def _wait_for(path: Path) -> None:
    for _ in range(500):
        if path.exists():
            return
        time.sleep(0.02)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_cancel_stops_the_running_hook_runs_no_more_and_delivers_post_cancel(
    store: Store, tmp_path: Path
) -> None:
    argv, marker = marker_job(tmp_path)
    started, second, trace = tmp_path / "started", tmp_path / "second", tmp_path / "trace"
    seed_claimed(store, argv)
    cmd = f"echo $$ > {started}; sleep 30"
    post = "python3 -c \"import sys,json; print(json.load(sys.stdin)['hook'])\" >> " + str(trace)
    cfg = config(
        sh("slow", cmd, timeout="1h"),
        sh("next", f"touch {second}"),
        **{"post-cancel": [sh("pc", post)]},
    )

    def cancel() -> None:
        _wait_for(started)
        with Store.open(mode=OpenMode.CLI) as other:
            with other.write() as tx:
                tx.execute(
                    "UPDATE jobs SET status='canceled', reason='canceled' WHERE id = ?", [JOB]
                )
                tx.execute("UPDATE attempts SET status='canceled' WHERE job_id = ?", [JOB])
            put_event(other, JOB, "post-cancel", 1)

    threading.Thread(target=cancel).start()
    began = time.monotonic()
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert time.monotonic() - began < 15
    pid = int(started.read_text())
    for _ in range(100):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid)
    assert not second.exists() and not marker.exists()
    assert job(store)["status"] == "canceled"
    assert trace.read_text().split() == ["post-cancel"]
    assert pending(store) == []


def test_sigterm_during_a_hook_ends_the_job_terminated(store: Store, tmp_path: Path) -> None:
    started = tmp_path / "started"
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    shim = make_shim(store, config(sh("slow", f"touch {started}; sleep 30")), hook_tuning=FAST)
    threading.Thread(target=lambda: (_wait_for(started), shim.request_stop())).start()
    assert shim.run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "terminated")
    assert attempt(store)["exit"] is None and not marker.exists()


# -- the payload and the environment --------------------------------------------------------


def test_the_hook_payload_and_environment(store: Store, tmp_path: Path) -> None:
    argv, _ = marker_job(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    seed_claimed(store, argv)
    with store.write() as tx:
        tx.execute("INSERT INTO job_labels (job_id, key, value) VALUES (?, 'team', 'x')", [JOB])
    cmd = f"cat > {out}/$JOBSCTL_HOOK.json; env > {out}/env"
    cfg = config(
        sh("other-queue", "exit 1", queues=["elsewhere"]),
        sh("capture", cmd, labels=["team=x"]),
    )
    cfg = cfg.model_copy(update={"env": cfg.env.model_copy(update={"inherit": ["PATH"]})})
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    payload = json.loads((out / "pre-start.json").read_text())
    validate_against("hooks/pre-start.json", payload)
    assert payload["event_id"] == f"{CLAIM}:pre-start:1"  # its place in the configured list
    assert payload["previous_status"] == "queued" and payload["repo"] is None
    assert payload["job"]["status"] == "starting" and payload["attempt"]["status"] == "starting"
    assert payload["labels"] == ["team=x"]
    env = dict(line.split("=", 1) for line in (out / "env").read_text().splitlines() if "=" in line)
    assert env["JOBSCTL_HOOK"] == "pre-start" and env["JOBSCTL_ID"] == JOB
    assert "HOME" not in env  # the internal HOME never reaches a hook


def test_a_queue_that_no_hook_selects_runs_none(store: Store, tmp_path: Path) -> None:
    argv, marker = marker_job(tmp_path)
    seed_claimed(store, argv)
    cfg = config(sh("only-x", "exit 1", queues=["x"]), sh("not-q", "exit 1", exclude_queues=["q"]))
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert marker.exists()
    assert log_records("hook_ran") == []


def test_a_config_error_runs_no_pre_start_hook(store: Store, tmp_path: Path) -> None:
    from jobsctl.errors import JobsctlError

    seed_claimed(store, ["true"])
    shim = make_shim(store, config_error=JobsctlError("config_invalid", "bad"))
    assert shim.run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "spawn_failed")


def test_release_with_override_escalation_lets_the_next_start_through(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Escalate, release `--override-escalation` as a human, claim again: the escalating hook
    runs again, its exit 13 counts as allow, and the job runs."""
    from tests.helpers import run_cli
    from tests.store_helpers import attempt_values, insert_row

    argv, marker = marker_job(tmp_path)
    runs = tmp_path / "runs"
    seed_claimed(store, argv)
    cfg = config(sh("ask", f"echo x >> {runs}; exit 13"), sh("fine", "true"))
    assert make_shim(store, cfg, hook_tuning=FAST).run() == 0
    assert job(store)["status"] == "held"
    res = run_cli(capsys, "--json", "release", JOB, "--override-escalation", "--actor", "human:kim")
    assert res.code == 0, res.out + res.err
    assert json.loads(job(store)["override_hooks"]) == ["ask"]
    with store.write() as tx:  # the next pass claims it
        tx.execute("UPDATE jobs SET status='starting', attempt=2 WHERE id = ?", [JOB])
        insert_row(tx, "attempts", attempt_values(JOB, 2, "claim-bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                   claimed_by=attempt(store)["shim_identity"], shim_identity=attempt(store)["shim_identity"]))  # fmt: skip
    shim = make_shim(store, cfg, claim_id="claim-bbbbbbbbbbbbbbbbbbbbbbbbbbbb", hook_tuning=FAST)
    assert shim.run() == 0
    assert runs.read_text().split() == ["x", "x"]
    assert marker.exists() and job(store)["status"] == "done"
    assert job(store)["override_hooks"] == "[]"
