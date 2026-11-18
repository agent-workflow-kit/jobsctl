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


def _holds_state_db(fd_dir: Path, errors: list[OSError] | None = None) -> bool:
    """Whether a descriptor under `fd_dir` (/proc/<pid>/fd) points at state.db. The directory is
    unreadable (EACCES) while the child execs, and gone when it exits: both mean "not yet"
    here (the caller's `poll()` assertion catches an exit), never an error (#88). Each such
    error is appended to `errors` when given, so a final failure can name its cause."""
    try:
        fds = list(fd_dir.iterdir())
    except OSError as exc:
        if errors is not None:
            errors.append(exc)
        return False
    for fd in fds:
        try:
            if os.readlink(fd).endswith("state.db"):
                return True
        except OSError:
            continue
    return False


def _await_polling(proc: subprocess.Popen[str]) -> None:
    """Block (bounded) until the process holds state.db open: its handlers are installed before
    the database is opened, so from then on a signal is handled, not fatal."""
    deadline = time.monotonic() + READY
    fd_dir = Path(f"/proc/{proc.pid}/fd")
    errors: list[OSError] = []
    while time.monotonic() < deadline:
        assert proc.poll() is None, "the wait ended before it was polling"
        if _holds_state_db(fd_dir, errors):
            return
        time.sleep(0.02)
    last = repr(errors[-1]) if errors else "none"
    raise AssertionError(f"the wait never opened the database (last /proc error: {last})")


def _finish(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect the wait's output. A 30 s `TimeoutExpired` here is usually a symptom: the test body
    raised before it ended the wait (the first exception in the log is the cause, see #88)."""
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


@pytest.mark.parametrize("exc", [PermissionError, FileNotFoundError, ProcessLookupError])
def test_the_readiness_probe_treats_an_unreadable_fd_directory_as_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: type[OSError]
) -> None:
    # /proc/<pid>/fd is unreadable (EACCES) while a child execs, and gone once it exits (#88).
    def refuse(self: Path) -> Any:
        raise exc(13, "refused", str(self))

    monkeypatch.setattr(Path, "iterdir", refuse)
    assert _holds_state_db(tmp_path) is False


def test_the_readiness_probe_sees_a_descriptor_on_state_db(tmp_path: Path) -> None:
    fd_dir = tmp_path / "fd"
    fd_dir.mkdir()
    (fd_dir / "3").symlink_to(tmp_path / "other.txt")
    assert _holds_state_db(fd_dir) is False
    (fd_dir / "4").symlink_to(tmp_path / "state.db")
    assert _holds_state_db(fd_dir) is True


def test_the_polling_wait_retries_after_a_transient_unreadable_fd_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_iterdir = Path.iterdir
    calls: list[Path] = []

    def flaky(self: Path) -> Any:
        calls.append(self)
        if len(calls) == 1:
            raise PermissionError(13, "refused", str(self))
        return real_iterdir(self)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys, time; f = open(sys.argv[1], 'w'); time.sleep(60)",
            str(tmp_path / "state.db"),
        ],
        stdin=subprocess.DEVNULL,
    )
    try:
        monkeypatch.setattr(Path, "iterdir", flaky)
        _await_polling(holder)  # type: ignore[arg-type]
    finally:
        monkeypatch.undo()
        holder.terminate()  # the exact child this test spawned
        holder.wait(timeout=10)
    assert len(calls) >= 2


def test_a_wait_that_never_opens_the_database_reports_the_last_proc_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self: Path) -> Any:
        raise PermissionError(13, "refused", str(self))

    class Stub:
        pid = os.getpid()

        def poll(self) -> None:
            return None

    monkeypatch.setattr(Path, "iterdir", refuse)
    monkeypatch.setattr(sys.modules[__name__], "READY", 0.2)
    with pytest.raises(AssertionError, match=r"last /proc error: PermissionError"):
        _await_polling(Stub())  # type: ignore[arg-type]


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
