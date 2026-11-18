"""`wait` and `add --wait` as real processes: signals, ignored signals and real exit codes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from jobsctl.store_dir import resolve_state_dir
from tests.capabilities import HAS_PROC
from tests.conftest import validate_against
from tests.helpers import run_cli
from tests.manual_helpers import add_job, config_file, make_done, row
from tests.shim_helpers import JOB
from tests.shim_real import RealShims

READY = 30.0


def _spawn_wait(
    job_id: str, tmp_path: Path, *extra: str, ignore_hup: bool = False
) -> subprocess.Popen[str]:
    cfg = config_file(tmp_path, auto_run=False)

    def preexec() -> None:
        if ignore_hup:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)

    return subprocess.Popen(
        [sys.executable, "-I", "-m", "jobsctl", "--config", cfg, "--json", "wait", job_id, *extra],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=preexec if ignore_hup else None,  # noqa: PLW1509
    )


def _await_polling(proc: subprocess.Popen[str]) -> None:
    """Block (bounded) until the process holds state.db open: its handlers are installed before
    the database is opened, so from then on a signal is handled, not fatal."""
    if not HAS_PROC:
        pytest.skip("needs /proc/<pid>/fd (Linux)")
    deadline = time.monotonic() + READY
    fd_dir = Path(f"/proc/{proc.pid}/fd")
    while time.monotonic() < deadline:
        assert proc.poll() is None, "the wait ended before it was polling"
        for fd in fd_dir.iterdir():
            try:
                if os.readlink(fd).endswith("state.db"):
                    return
            except OSError:
                continue
        time.sleep(0.02)
    raise AssertionError("the wait never opened the database")


def _finish(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()  # the exact child this test spawned
            proc.wait(timeout=10)


@pytest.mark.parametrize(
    "sigs",
    [
        [signal.SIGINT],
        [signal.SIGTERM],
        [signal.SIGHUP],
        [signal.SIGTERM, signal.SIGTERM],
        [signal.SIGINT, signal.SIGTERM, signal.SIGHUP],
        [signal.SIGHUP, signal.SIGINT, signal.SIGINT, signal.SIGTERM],
    ],
    ids=lambda sigs: "+".join(s.name for s in sigs),
)
def test_a_waiting_process_that_gets_signals_prints_the_job_and_exits_130(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sigs: list[signal.Signals]
) -> None:
    job_id = add_job(capsys)
    proc = _spawn_wait(job_id, tmp_path)
    try:
        _await_polling(proc)
        for sig in sigs:  # back to back: the later ones must not break the first one's handling
            proc.send_signal(sig)
    finally:
        out, err = _finish(proc)
    assert proc.returncode == 130, out + err
    doc = json.loads(out)
    validate_against("cli/show.json", doc)
    assert doc["id"] == job_id and doc["status"] == "queued"
    assert "Traceback" not in err
    assert row(job_id)["status"] == "queued"


def test_a_sighup_ignored_at_start_does_not_end_the_wait(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job_id = add_job(capsys)
    proc = _spawn_wait(job_id, tmp_path, ignore_hup=True)
    try:
        _await_polling(proc)
        proc.send_signal(signal.SIGHUP)
        make_done(job_id)
    finally:
        out, err = _finish(proc)
    assert proc.returncode == 0, out + err
    assert json.loads(out)["status"] == "done"


def test_a_real_wait_sees_the_job_end(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    job_id = add_job(capsys)
    proc = _spawn_wait(job_id, tmp_path)
    try:
        _await_polling(proc)
        make_done(job_id)
    finally:
        out, err = _finish(proc)
    assert proc.returncode == 0, out + err
    assert json.loads(out)["status"] == "done"


# -- exit codes of jobs a real shim ran -----------------------------------------------------


@pytest.mark.parametrize(
    ("script", "status", "code"),
    [
        ("exit 0", "done", 0),
        ("exit 3", "failed", 12),
        ("exit 13", "held", 13),
    ],
)
def test_wait_reports_the_outcome_of_a_job_a_real_shim_ran(
    real_shims: RealShims, capsys: pytest.CaptureFixture[str], script: str, status: str, code: int
) -> None:
    popen = real_shims.seed_and_spawn(["sh", "-c", f"echo out-of-the-job; {script}"])
    real_shims.wait_status(status)
    assert popen.wait(timeout=60) == 0
    res = run_cli(capsys, "--json", "wait", JOB)
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    validate_against("cli/show.json", doc)
    assert doc["status"] == status and doc["output"]["tail"] == "out-of-the-job\n"


def test_wait_exits_20_for_a_canceled_job(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job_id = add_job(capsys)
    assert run_cli(capsys, "--json", "cancel", job_id).code == 0
    res = run_cli(capsys, "--json", "wait", job_id)
    assert res.code == 20 and res.envelope()["status"] == "canceled"


@pytest.mark.real_trigger
def test_add_wait_runs_a_job_end_to_end_through_a_real_pass_and_shim(
    real_shims: RealShims, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    real_shims.configure(tmp_path / "cfg", auto_run=True)
    res = run_cli(
        capsys,
        "--config",
        str(real_shims.config_path),
        "--json",
        "add",
        "--wait",
        "--wait-timeout",
        "90s",
        "--cwd",
        "/",
        "--",
        "sh",
        "-c",
        "echo from-add-wait",
    )
    assert res.code == 0, res.out + res.err
    doc: dict[str, Any] = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["waited"] is True and doc["job"]["status"] == "done"
    assert doc["job"]["output"]["tail"] == "from-add-wait\n"
    assert (resolve_state_dir() / "state.db").exists()
