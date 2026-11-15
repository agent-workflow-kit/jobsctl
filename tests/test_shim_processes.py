"""The shim as a real detached process (design.md spikes A1, A4-A7, B4, C4; interface.md Shim)."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from jobsctl.config import Config
from jobsctl.config_files import load_config
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Liveness, check_identity
from jobsctl.runner import PassResult, Runner
from jobsctl.store_db import Store
from jobsctl.store_dir import resolve_state_dir
from tests.runner_helpers import add_queued, job_ids, make_runner
from tests.shim_helpers import JOB, shims_dir
from tests.shim_real import RealShims


def test_a_real_shim_runs_a_job_to_done_and_exits(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(["sh", "-c", "echo from-the-job; exit 0"])
    real_shims.wait_status("done")
    assert popen.wait(timeout=60) == 0
    att = real_shims.row("SELECT * FROM attempts WHERE job_id = ?", JOB)
    assert att["shim_confirmed_at"] and att["leftovers"] == 0
    assert list(shims_dir().glob("*.result*")) == []
    tail = real_shims.row("SELECT tail FROM outputs WHERE job_id = ?", JOB)
    assert bytes(tail["tail"]) == b"from-the-job\n"
    assert real_shims.row("SELECT 1 FROM leases WHERE name = ?", f"job:{JOB}") is None
    # the shim's diagnostics file is created on first write only; a clean run leaves none
    assert not (shims_dir() / f"{JOB}.log").exists()


def test_the_shim_survives_sighup_and_sigint_and_the_job_gets_default_dispositions(
    real_shims: RealShims, tmp_path: Path
) -> None:
    """Spike A1: a hangup does not end a detached shim. The job must not inherit the shim's
    ignoring: SIGHUP and SIGINT are *handled* (not SIG_IGN) in the shim, so exec resets them,
    and `Popen` restores SIGPIPE. Read from the kernel's SigIgn mask of a fresh `cat`."""
    gate = tmp_path / "gate"
    popen = real_shims.seed_and_spawn(
        ["sh", "-c", f"while [ ! -e {gate} ]; do sleep 0.05; done; cat /proc/self/status"]
    )
    real_shims.wait_status("running")
    os.killpg(popen.pid, signal.SIGHUP)  # the shim leads its own session; it is our child
    os.kill(popen.pid, signal.SIGINT)
    time.sleep(0.3)
    assert popen.poll() is None  # still alive
    gate.touch()
    real_shims.wait_status("done")
    tail = bytes(real_shims.row("SELECT tail FROM outputs WHERE job_id = ?", JOB)["tail"])
    (line,) = [x for x in tail.decode().splitlines() if x.startswith("SigIgn:")]
    ignored = int(line.split()[1], 16)
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE, signal.SIGTERM):
        assert not ignored & (1 << (number - 1)), number.name
    assert popen.wait(timeout=60) == 0


def test_the_shim_is_non_dumpable_and_the_job_is_not(real_shims: RealShims, tmp_path: Path) -> None:
    """Spike C4: the shim's /proc/<pid>/environ is unreadable to another process; the flag resets
    on exec, so the job's is readable."""
    gate = tmp_path / "gate"
    popen = real_shims.seed_and_spawn(["sh", "-c", f"while [ ! -e {gate} ]; do sleep 0.05; done"])
    real_shims.wait_status("running")
    with pytest.raises(PermissionError):
        Path(f"/proc/{popen.pid}/environ").read_bytes()
    leader = real_shims.job_identity().pid
    assert b"JOBSCTL_JOB_ID=" + JOB.encode() in Path(f"/proc/{leader}/environ").read_bytes()
    gate.touch()
    real_shims.wait_status("done")


def test_the_job_leads_a_new_group_inside_the_shims_session_not_the_shims_group(
    real_shims: RealShims, tmp_path: Path
) -> None:
    gate = tmp_path / "gate"
    popen = real_shims.seed_and_spawn(["sh", "-c", f"while [ ! -e {gate} ]; do sleep 0.05; done"])
    real_shims.wait_status("running")
    leader = real_shims.job_identity().pid
    assert os.getpgid(leader) == leader != os.getpgid(popen.pid)
    assert os.getsid(leader) == os.getsid(popen.pid) == popen.pid  # the shim's session
    gate.touch()
    real_shims.wait_status("done")


def test_a_daemon_that_left_the_group_is_adopted_by_the_subreaper_and_counted(
    real_shims: RealShims, tmp_path: Path
) -> None:
    """Spike A4: the shim is a child subreaper, so a descendant that called setsid and lost its
    parent is found by its PPID; it is counted and left alone."""
    pidfile = tmp_path / "daemon.pid"
    popen = real_shims.seed_and_spawn(
        [
            "sh", "-c",
            f"setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; sleep 0.3; exit 0",
        ]
    )  # fmt: skip
    real_shims.wait_status("done")
    pid = int(pidfile.read_text())
    ident = real_shims.track(pid)
    assert check_identity(default_host(), ident).alive  # never signaled by the shim
    att = real_shims.row("SELECT leftovers FROM attempts WHERE job_id = ?", JOB)
    assert att["leftovers"] == 1
    assert popen.wait(timeout=60) == 0


def test_the_shim_dying_leaves_a_silent_job_running_spike_a5(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(["sleep", "30"])
    real_shims.wait_status("running")
    leader = real_shims.job_identity()
    popen.kill()  # the exact child this test spawned
    popen.wait(timeout=30)
    time.sleep(0.5)
    assert check_identity(default_host(), leader).state is Liveness.ALIVE  # no PDEATHSIG
    assert real_shims.row("SELECT status FROM jobs")["status"] == "running"


def test_a_job_that_writes_after_the_shim_died_gets_sigpipe_spike_b4(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(["sh", "-c", "while true; do echo tick; sleep 0.1; done"])
    real_shims.wait_status("running")
    leader = real_shims.job_identity()
    popen.kill()
    popen.wait(timeout=30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if check_identity(default_host(), leader).state is Liveness.DEAD:
            return
        time.sleep(0.1)
    raise AssertionError("the writing job survived the loss of its reader")


def test_two_gigabytes_style_flood_stays_in_bounded_memory_spike_b4(
    real_shims: RealShims,
) -> None:
    """256 MiB through the pipe: the shim's resident set stays small (the spike measured 12 MB
    for 2 GiB). Measured from /proc/<shim>/status while the job runs."""
    popen = real_shims.seed_and_spawn(["sh", "-c", "head -c 268435456 /dev/zero | tr '\\0' 'x'"])
    peak = 0
    deadline = time.monotonic() + 120
    while popen.poll() is None and time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{popen.pid}/status").read_text()
        except OSError:
            break
        for line in status.splitlines():
            if line.startswith("VmHWM:"):
                peak = max(peak, int(line.split()[1]))
        time.sleep(0.05)
    real_shims.wait_status("done")
    assert popen.wait(timeout=60) == 0
    row = real_shims.row("SELECT bytes_seen, truncated FROM outputs WHERE job_id = ?", JOB)
    assert row["bytes_seen"] == 268435456 and row["truncated"] == 1
    assert 0 < peak < 120_000  # KiB: well under 120 MB including the interpreter itself


def test_an_unreadable_config_ends_the_job_spawn_failed_in_a_real_shim(
    real_shims: RealShims, tmp_path: Path
) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("auto_run: [unclosed\n")
    real_shims.config_path = bad
    popen = real_shims.seed_and_spawn(["true"])
    real_shims.wait_status("failed")
    row = real_shims.row("SELECT reason FROM jobs WHERE id = ?", JOB)
    assert row["reason"] == "spawn_failed"
    assert popen.wait(timeout=60) == 0
    log = (resolve_state_dir() / "log.jsonl").read_text()
    assert json.loads(log.splitlines()[0])["event"] == "config_invalid"


def test_sigterm_to_a_shim_waiting_for_its_identity_writes_nothing(real_shims: RealShims) -> None:
    popen = real_shims.seed_and_spawn(["true"], record_identity=False)
    time.sleep(1.0)  # the pass (this test) is alive and has not recorded the shim: it polls
    popen.send_signal(signal.SIGTERM)  # the exact child
    assert popen.wait(timeout=30) == 0
    row = real_shims.row("SELECT status FROM jobs WHERE id = ?", JOB)
    assert row["status"] == "starting"
    att = real_shims.row("SELECT shim_confirmed_at FROM attempts WHERE job_id = ?", JOB)
    assert att["shim_confirmed_at"] is None


def test_a_shim_whose_recorded_identity_is_another_process_exits_at_once(
    real_shims: RealShims, sleeper: str
) -> None:
    popen = real_shims.seed_and_spawn(["true"], record_identity=False)
    with (
        __import__("jobsctl.store_db", fromlist=["Store"]).Store.open() as store,
        store.write() as tx,
    ):
        tx.execute("UPDATE attempts SET shim_identity = ?", [sleeper])
    assert popen.wait(timeout=30) == 0
    assert real_shims.row("SELECT status FROM jobs")["status"] == "starting"


def test_passes_and_real_shims_run_a_queue_to_completion_within_the_cap(
    real_shims: RealShims, runners: list[Runner]
) -> None:
    """A pass claims up to the cap, spawns real shims, and a later pass starts the rest once the
    first jobs have ended; the cap is never exceeded while they run."""
    ids = job_ids(5)
    config = Config.model_validate({"auto_run": False, "queues": {"q": {"cap": 2}}})
    with Store.open() as store:
        for i, job_id in enumerate(ids):
            add_queued(
                store,
                job_id,
                position=f"{i + 1:020d}",
                argv=json.dumps(["sh", "-c", f"sleep 0.4; echo {job_id}"]),
                cwd="/",
            )
    peak = 0
    counts: dict[str, int] = {}
    deadline = time.monotonic() + 120
    with Store.open() as store:
        runner = make_runner(runners, store, config)
        while time.monotonic() < deadline:
            runner.run_pass()
            counts = {
                r["status"]: r["n"]
                for r in store.query_all("SELECT status, count(*) AS n FROM jobs GROUP BY status")
            }
            peak = max(peak, counts.get("starting", 0) + counts.get("running", 0))
            if counts.get("done", 0) == len(ids):
                break
            time.sleep(0.1)
        runner.reap(PassResult())
        outputs = {
            r["job_id"]: bytes(r["tail"]).decode()
            for r in store.query_all("SELECT job_id, tail FROM outputs")
        }
        assert counts == {"done": 5}
    assert 1 <= peak <= 2
    assert outputs == {job_id: f"{job_id}\n" for job_id in ids}


def test_a_failure_before_the_job_starts_leaves_a_private_diagnostics_file_without_values(
    real_shims: RealShims,
) -> None:
    popen = real_shims.seed_and_spawn(["no-such-command-xyz", "--token=hunter2-hunter2"])
    real_shims.wait_status("failed")
    assert popen.wait(timeout=60) == 0
    log = shims_dir() / f"{JOB}.log"
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    text = log.read_text()
    assert "argv[0]" in text and "hunter2" not in text


def test_a_real_job_gets_no_home_when_the_config_does_not_inherit_it(
    real_shims: RealShims, tmp_path: Path
) -> None:
    """HOME is passed to jobsctl's own internal processes only (Internal processes): the shim has
    it, the job does not unless `env.inherit` lists it."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"env": {"inherit": ["PATH"]}, "auto_run": False}))
    real_shims.config_path = cfg
    real_shims.config = load_config(cfg)
    popen = real_shims.seed_and_spawn(["env"])
    real_shims.wait_status("done")
    assert popen.wait(timeout=60) == 0
    row = real_shims.row("SELECT tail FROM outputs WHERE job_id = ?", JOB)
    names = {line.split("=", 1)[0] for line in bytes(row["tail"]).decode().splitlines()}
    assert "HOME" not in names and "XDG_STATE_HOME" not in names
    assert {"PATH", "JOBSCTL_JOB_ID", "JOBSCTL_ATTEMPT"} <= names
