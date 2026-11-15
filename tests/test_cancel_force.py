"""`cancel --force` of a running job (interface.md, cancel, Human-only flags, Signal sequence).

A "fake shim" is a real child process that records, at SIGTERM, what the database said about
the force request, and optionally ends the job the way a shim would. Every process a test
starts is a `Popen` this test owns and kills by its handle.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import cancel_force
from jobsctl.leases import format_identity, identity_text
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Identity
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.manual_helpers import (
    HUMAN,
    add_job,
    attempt_row,
    audit_records,
    log_records,
    make_done,
    make_held,
    make_running,
    make_starting,
    outbox,
    refusal,
    row,
    sql,
    table_dump,
)

pytestmark = pytest.mark.usefixtures("validated_records")

FAKE_SHIM = r"""
import signal, sqlite3, sys, time
db, job, log, finalize = sys.argv[1:5]

def on_term(signum, frame):
    conn = sqlite3.connect(db, timeout=30)
    found = conn.execute("SELECT force_requested_by FROM attempts WHERE job_id = ?", (job,))
    with open(log, "a") as f:
        f.write(str(found.fetchone()[0]) + "\n")
    if finalize == "1":
        conn.execute("UPDATE attempts SET status = 'canceled', reason = 'forced', "
                     "ended_at = '2026-01-01T00:00:09.000Z', leftovers = 0, "
                     "resolved_path = '/bin/x', "
                     "\"exit\" = '{\"kind\": \"unknown\", \"code\": null, \"signal\": null, "
                     "\"core\": false}' WHERE job_id = ?", (job,))
        conn.execute("UPDATE jobs SET status = 'canceled', reason = 'forced', "
                     "ended_at = '2026-01-01T00:00:09.000Z' WHERE id = ?", (job,))
        conn.commit()
        sys.exit(0)

signal.signal(signal.SIGTERM, on_term)
open(log + ".ready", "w").close()
while True:
    time.sleep(0.05)
"""


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cancel_force, "POLL_SECONDS", 0.05)


@pytest.fixture
def procs() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for popen in started:
        if popen.poll() is None:
            popen.kill()  # the exact child this test spawned
        popen.wait(timeout=30)


def cancel(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(capsys, "--json", "cancel", *flags)


def force(capsys: pytest.CaptureFixture[str], job_id: str, *flags: str) -> Result:
    actor = () if "--actor" in flags else HUMAN
    return cancel(capsys, job_id, "--force", *flags, *actor)


def doc_of(res: Result, *, code: int = 0) -> dict[str, Any]:
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    return doc


def error_of(res: Result, code: int, name: str) -> dict[str, Any]:
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    assert doc["error"]["code"] == name
    return doc["error"]


def start_fake_shim(
    procs: list[subprocess.Popen[bytes]], tmp_path: Path, job_id: str, *, finalize: bool
) -> tuple[subprocess.Popen[bytes], Path]:
    log = tmp_path / f"shim-{len(procs)}.log"
    popen = subprocess.Popen(
        [sys.executable, "-I", "-c", FAKE_SHIM, str(resolve_state_dir() / "state.db"), job_id,
         str(log), "1" if finalize else "0"],
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    procs.append(popen)
    ready = Path(str(log) + ".ready")
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert time.monotonic() < deadline, "the fake shim did not start"
        time.sleep(0.02)
    sql(
        "UPDATE attempts SET shim_identity = ? WHERE job_id = ?",
        identity_text(default_host(), popen.pid),
        job_id,
    )
    return popen, log


def running_job(capsys: pytest.CaptureFixture[str], *flags: str) -> str:
    job_id = add_job(capsys, *flags)
    make_running(job_id)
    sql("UPDATE attempts SET resolved_path = '/bin/x' WHERE job_id = ?", job_id)
    return job_id


def lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def wait_lines(log: Path, count: int) -> list[str]:
    deadline = time.monotonic() + 30
    while len(lines(log)) < count:
        assert time.monotonic() < deadline, "the fake shim was not signaled"
        time.sleep(0.02)
    return lines(log)


@pytest.fixture
def spawned_kills(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces the `_kill` spawn: records the job ids it would have started."""
    calls: list[str] = []
    monkeypatch.setattr(cancel_force, "spawn_kill", lambda ctx, job_id: calls.append(job_id))
    return calls


# -- the human gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        (),
        ("--yes",),
        ("--actor", "auto:ci"),
        ("--dry-run",),
        ("--dry-run", "--yes", "--actor", "auto:ci"),
    ],
)
def test_force_needs_a_human_and_acts_on_nothing(
    capsys: pytest.CaptureFixture[str], flags: tuple[str, ...], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=True)
    before = table_dump()
    error_of(cancel(capsys, job_id, "--force", *flags), 13, "needs_human")
    assert table_dump() == before and audit_records() == []
    time.sleep(0.2)
    assert lines(log) == []  # nothing was signaled


def test_force_with_a_unique_key_selector_needs_a_human_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = running_job(capsys, "--unique-key", "K1")
    for extra in ((), ("--all",), ("--dry-run",)):
        error_of(cancel(capsys, "--unique-key", "K1", "--force", *extra), 13, "needs_human")
    assert attempt_row(job_id)["force_requested_by"] is None and audit_records() == []


def test_the_state_refusals_come_before_the_human_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # no --force: refused (live), not needs_human
    job_id = running_job(capsys)
    refusal(cancel(capsys, job_id), "live")


def test_force_on_jobs_that_are_not_running_is_a_plain_cancel_without_a_human(
    capsys: pytest.CaptureFixture[str],
) -> None:
    queued = add_job(capsys)
    held = add_job(capsys)
    make_held(held, "escalated", ("g",))
    starting = add_job(capsys)
    make_starting(starting)
    finished = add_job(capsys)
    make_done(finished)
    for job_id in (queued, held, starting):
        doc = doc_of(cancel(capsys, job_id, "--force"))
        assert doc["jobs"][0]["status"] == "canceled" and doc["jobs"][0]["reason"] == "canceled"
        assert row(job_id)["status"] == "canceled"
    doc = doc_of(cancel(capsys, finished, "--force"))
    assert doc["changed"] is False and doc["jobs"][0]["status"] == "done"
    assert all(
        a["action"] != "cancel_force" for a in audit_records()
    )  # no force request on any of them


# -- the request: commit, then signal ------------------------------------------------------------


def test_the_request_is_committed_and_audited_before_the_shim_is_signaled(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys, "--unique-key", "K1")
    popen, log = start_fake_shim(procs, tmp_path, job_id, finalize=True)
    res = force(capsys, job_id, "--actor", "human:kim", "--decision-id", "d1")
    doc = doc_of(res)
    # the shim read the committed request when the SIGTERM arrived
    assert lines(log) == ["human:kim"]
    job = doc["jobs"][0]
    assert doc["changed"] is True and doc["dry_run"] is False and doc["refused"] == []
    assert job["status"] == "canceled" and job["reason"] == "forced"
    assert job["force_requested_by"] == "human:kim"
    (audit,) = audit_records()
    assert audit["action"] == "cancel_force" and audit["target"] == job_id
    assert audit["actor"] == "human:kim" and audit["decision_id"] == ["d1"]
    assert audit["result"] == "running" and audit["inputs"]["unique_key"] == "K1"
    assert audit["inputs"]["attempt"] == 1 and audit["inputs"]["queue"] == "default"
    assert popen.wait(timeout=30) == 0


def test_force_by_unique_key_alone_works(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys, "--unique-key", "K1")
    start_fake_shim(procs, tmp_path, job_id, finalize=True)
    doc = doc_of(cancel(capsys, "--unique-key", "K1", "--force", "--all", *HUMAN))
    assert [j["id"] for j in doc["jobs"]] == [job_id] and doc["jobs"][0]["reason"] == "forced"


def test_wait_timeout_zero_returns_at_once_with_the_job_still_running(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    doc = doc_of(force(capsys, job_id, "--wait-timeout", "0", *HUMAN))
    assert doc["changed"] is True
    assert doc["jobs"][0]["status"] == "running"
    assert doc["jobs"][0]["force_requested_by"] == "human:kim"
    assert wait_lines(log, 1) == ["human:kim"]  # the wake-up was sent
    assert row(job_id)["status"] == "running"


def test_a_wait_that_expires_exits_12_and_keeps_the_request(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    res = force(capsys, job_id, "--wait-timeout", "1s", *HUMAN)
    err = error_of(res, 12, "cancel_pending")
    assert err["details"] == {"job_id": job_id}
    assert attempt_row(job_id)["force_requested_by"] == "human:kim"
    assert row(job_id)["status"] == "running"
    assert len(audit_records()) == 1 and lines(log) == ["human:kim"]


def test_the_default_wait_is_twice_the_kill_grace_plus_ten_seconds(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = running_job(capsys)
    sql(
        "UPDATE attempts SET effective = ? WHERE job_id = ?",
        json.dumps({"timeout": None, "max_retries": 0, "kill_grace": 7}),
        job_id,
    )
    seen: list[int] = []

    def fake_wait(ctx: Any, target: Any, timeout: int, **kw: Any) -> None:
        seen.append(timeout)

    monkeypatch.setattr(cancel_force, "wait_until_ended", fake_wait)
    monkeypatch.setattr(cancel_force, "deliver_request", lambda *a, **k: None)
    force(capsys, job_id)
    force(capsys, job_id, "--wait-timeout", "5m")
    assert seen == [24, 300]


def test_a_repeated_request_is_idempotent_and_resends_the_wake_up(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    first = doc_of(force(capsys, job_id, "--wait-timeout", "0", "--actor", "human:kim"))
    assert first["changed"] is True
    wait_lines(log, 1)
    second = doc_of(force(capsys, job_id, "--wait-timeout", "0", "--actor", "human:lee"))
    assert second["changed"] is False
    assert second["jobs"][0]["force_requested_by"] == "human:kim"  # the first requester stays
    assert wait_lines(log, 2) == ["human:kim", "human:kim"]
    assert len(audit_records()) == 1
    assert attempt_row(job_id)["force_requested_by"] == "human:kim"


def test_a_force_request_on_a_job_that_ended_meanwhile_is_a_noop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    doc = doc_of(force(capsys, job_id))
    assert doc["changed"] is False and doc["jobs"][0]["status"] == "failed"
    assert attempt_row(job_id)["force_requested_by"] is None and audit_records() == []


def test_a_job_ending_during_the_wait_is_reported_in_its_final_state(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    start_fake_shim(procs, tmp_path, job_id, finalize=True)
    doc = doc_of(force(capsys, job_id))
    assert doc["jobs"][0]["status"] == "canceled" and doc["jobs"][0]["ended_at"] is not None


# -- dry run -------------------------------------------------------------------------------------


def test_dry_run_projects_the_request_and_writes_and_sends_nothing(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=True)
    before = table_dump()
    doc = doc_of(force(capsys, job_id, "--dry-run", "--actor", "human:kim"))
    assert doc["dry_run"] is True and doc["changed"] is True
    assert doc["jobs"][0]["status"] == "running"
    assert doc["jobs"][0]["force_requested_by"] == "human:kim"
    assert table_dump() == before and audit_records() == []
    time.sleep(0.2)
    assert lines(log) == []


def test_dry_run_of_an_already_requested_job_is_unchanged(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    doc_of(force(capsys, job_id, "--wait-timeout", "0", "--actor", "human:kim"))
    wait_lines(log, 1)
    before = table_dump()
    doc = doc_of(force(capsys, job_id, "--dry-run", "--actor", "human:lee"))
    assert doc["changed"] is False and doc["jobs"][0]["force_requested_by"] == "human:kim"
    assert table_dump() == before
    time.sleep(0.2)
    assert lines(log) == ["human:kim"]


def test_dry_run_with_no_database_finds_no_job(capsys: pytest.CaptureFixture[str]) -> None:
    res = cancel(capsys, "jb-aaaaaaaa", "--force", "--dry-run", *HUMAN)
    error_of(res, 10, "not_found")


# -- which process is signaled -------------------------------------------------------------------


def test_a_shim_identity_that_does_not_match_the_live_process_is_never_signaled(
    capsys: pytest.CaptureFixture[str],
    procs: list[Any],
    tmp_path: Path,
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys)
    bystander, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    real = identity_text(default_host(), bystander.pid).rsplit(":", 1)
    stale = f"{real[0]}:{int(real[1]) + 1}"  # same pid, a start time that is not the live one
    sql("UPDATE attempts SET shim_identity = ? WHERE job_id = ?", stale, job_id)
    res = force(capsys, job_id, "--wait-timeout", "0")
    doc_of(res)
    time.sleep(0.3)
    assert lines(log) == [] and bystander.poll() is None  # not signaled, still alive
    assert spawned_kills == [job_id]  # no live shim: a `_kill` takes over
    assert attempt_row(job_id)["force_requested_by"] == "human:kim"


def test_a_shim_identity_from_another_boot_is_never_signaled(
    capsys: pytest.CaptureFixture[str],
    procs: list[Any],
    tmp_path: Path,
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys)
    bystander, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    ident = Identity("0000-other-boot", bystander.pid, 1)
    sql("UPDATE attempts SET shim_identity = ? WHERE job_id = ?", format_identity(ident), job_id)
    doc_of(force(capsys, job_id, "--wait-timeout", "0"))
    time.sleep(0.3)
    assert lines(log) == [] and bystander.poll() is None and spawned_kills == [job_id]


@pytest.mark.parametrize("recorded", [None, "", "garbage", "a:b:c"])
def test_an_unusable_shim_identity_signals_nothing(
    capsys: pytest.CaptureFixture[str],
    procs: list[Any],
    tmp_path: Path,
    spawned_kills: list[str],
    recorded: str | None,
) -> None:
    job_id = running_job(capsys)
    bystander, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    sql("UPDATE attempts SET shim_identity = ? WHERE job_id = ?", recorded, job_id)
    doc_of(force(capsys, job_id, "--wait-timeout", "0"))
    time.sleep(0.2)
    assert lines(log) == [] and bystander.poll() is None and spawned_kills == [job_id]


def test_a_shim_lost_job_gets_a_kill_process_and_no_signal(
    capsys: pytest.CaptureFixture[str],
    procs: list[Any],
    tmp_path: Path,
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys)
    # the shim is alive by identity, but the job is marked lost: nothing is sent to it
    bystander, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    sql("UPDATE jobs SET shim_lost = 1 WHERE id = ?", job_id)
    doc_of(force(capsys, job_id, "--wait-timeout", "0"))
    time.sleep(0.2)
    assert lines(log) == [] and spawned_kills == [job_id]
    assert len(audit_records()) == 1


def test_a_live_kill_process_is_not_doubled(
    capsys: pytest.CaptureFixture[str],
    procs: list[Any],
    tmp_path: Path,
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys)
    sql("UPDATE jobs SET shim_lost = 1 WHERE id = ?", job_id)
    holder = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)"])
    procs.append(holder)
    sql(
        "INSERT INTO leases (name, holder_identity, expires_at) VALUES (?, ?, ?)",
        f"job:{job_id}",
        identity_text(default_host(), holder.pid),
        "2999-01-01T00:00:00.000Z",
    )
    doc_of(force(capsys, job_id, "--wait-timeout", "0"))
    assert spawned_kills == []  # a live process holds the lease: it is already being killed


def test_a_dead_kill_process_is_replaced_by_a_repeated_request(
    capsys: pytest.CaptureFixture[str], spawned_kills: list[str]
) -> None:
    job_id = running_job(capsys)
    sql("UPDATE jobs SET shim_lost = 1 WHERE id = ?", job_id)
    doc_of(force(capsys, job_id, "--wait-timeout", "0"))
    doc_of(force(capsys, job_id, "--wait-timeout", "0", "--actor", "human:lee"))
    assert spawned_kills == [job_id, job_id] and len(audit_records()) == 1


def test_a_gone_shim_job_that_ended_meanwhile_spawns_no_kill(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    spawned_kills: list[str],
) -> None:
    job_id = running_job(capsys)
    gone = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    text = identity_text(default_host(), gone.pid)  # (still a zombie: identity readable)
    gone.wait(timeout=30)
    sql("UPDATE attempts SET shim_identity = ? WHERE job_id = ?", text, job_id)
    real = cancel_force.signal_process

    def ends_first(*args: Any, **kwargs: Any) -> Any:
        sql("UPDATE jobs SET status = 'canceled', reason = 'forced' WHERE id = ?", job_id)
        return real(*args, **kwargs)

    monkeypatch.setattr(cancel_force, "signal_process", ends_first)
    assert force(capsys, job_id, "--wait-timeout", "0").code == 0
    assert spawned_kills == []


# -- audit not lost ------------------------------------------------------------------------------


def test_an_unwritable_audit_file_fails_before_anything_is_committed_or_signaled(
    capsys: pytest.CaptureFixture[str], procs: list[Any], tmp_path: Path
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=True)
    (resolve_state_dir() / "audit.jsonl").symlink_to(tmp_path / "elsewhere")
    error_of(force(capsys, job_id), 1, "internal")
    assert attempt_row(job_id)["force_requested_by"] is None
    time.sleep(0.2)
    assert lines(log) == [] and row(job_id)["status"] == "running"


# -- interruption --------------------------------------------------------------------------------


def test_ctrl_c_during_the_wait_keeps_the_request_and_reports_it(
    capsys: pytest.CaptureFixture[str],
    procs: list[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = running_job(capsys)
    _, log = start_fake_shim(procs, tmp_path, job_id, finalize=False)
    before = signal.getsignal(signal.SIGINT)

    def interrupt(seconds: float) -> None:
        os.kill(os.getpid(), signal.SIGINT)  # this test process: a real SIGINT

    monkeypatch.setattr(cancel_force.gateway, "sleep", interrupt)
    err = error_of(force(capsys, job_id, "--wait-timeout", "5m"), 12, "cancel_pending")
    assert err["details"] == {"job_id": job_id}
    assert attempt_row(job_id)["force_requested_by"] == "human:kim"
    assert row(job_id)["status"] == "running" and len(audit_records()) == 1
    assert signal.getsignal(signal.SIGINT) is before  # handlers restored
    assert lines(log) == ["human:kim"]


def test_an_interrupt_before_the_commit_is_not_reported_as_a_request(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = running_job(capsys)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cancel_force, "request_force", interrupted)
    res = force(capsys, job_id)
    assert res.code == 130 and res.envelope()["error"]["code"] == "interrupted"
    assert attempt_row(job_id)["force_requested_by"] is None


# -- whole flow with a real shim -----------------------------------------------------------------


def test_force_cancel_of_a_real_job_through_a_real_shim(
    capsys: pytest.CaptureFixture[str], real_shims: Any
) -> None:
    from tests.shim_helpers import JOB

    popen = real_shims.seed_and_spawn(["sleep", "600"], overrides={"kill_grace": 5})
    real_shims.wait_status("running")
    cfg = str(real_shims.config_path)
    res = run_cli(
        capsys, "--config", cfg, "--json", "cancel", JOB, "--force", "--wait-timeout", "2m",
        *HUMAN,
    )  # fmt: skip
    doc = doc_of(res)
    job = doc["jobs"][0]
    assert job["status"] == "canceled" and job["reason"] == "forced"
    assert job["force_requested_by"] == "human:kim"
    assert popen.wait(timeout=60) == 0
    ident = real_shims.job_identity()
    from jobsctl.proc_identity import Liveness, check_identity

    assert check_identity(default_host(), ident).state is not Liveness.ALIVE
    (audit,) = audit_records()
    assert audit["action"] == "cancel_force"
    assert any(r["to"] == "canceled" and r["reason"] == "forced" for r in log_records("transition"))
    assert outbox() == [] or all(e["hook"] == "post-cancel" for e in outbox())
