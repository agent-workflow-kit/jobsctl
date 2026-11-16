"""Live macOS checks, run by a human on a Mac before 1.0 (docs/macos-checklist.md). Never in CI:

    make live-macos
    JOBSCTL_LIVE_MACOS=1 uv run pytest -m live tests/test_macos_live.py -v -rA

Everything here is safe by construction. HOME and every XDG directory are private (the autouse
``private_home`` fixture) and the state and config live under pytest's temporary directory. The
real crontab is never read or written (the cron checks are ``tests/test_cron_live.py`` and the
manual part of the checklist). A process is signaled only after its recorded identity was
confirmed (a PID read back from a job's file is identified first), and only by that exact PID or
the process group of a leader this test started: no pattern kills. Every wait is bounded.

Off macOS the whole module is skipped with the reason below, so it is collected and skipped
cleanly in CI and on Linux. A maintainer can run the platform-neutral tests (the CLI ones) on
Linux to check the tests themselves with ``JOBSCTL_LIVE_MACOS_ANY_OS=1``; the ``libproc`` ones
need a Mac and never run elsewhere.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import gateway_proc
from jobsctl.proc_host import DarwinHost, default_host
from jobsctl.proc_identity import Identity, Liveness, check_identity, identify
from jobsctl.proc_signal import Result, signal_group, signal_process
from tests.kill_guard import allow

ON_MAC = sys.platform == "darwin"
OPTED_IN = os.environ.get("JOBSCTL_LIVE_MACOS") == "1"
ANY_OS = os.environ.get("JOBSCTL_LIVE_MACOS_ANY_OS") == "1"

if not ON_MAC and not ANY_OS:
    _SKIP = "macOS only (docs/macos-checklist.md); JOBSCTL_LIVE_MACOS_ANY_OS=1 runs the CLI checks"
elif not OPTED_IN:
    _SKIP = "set JOBSCTL_LIVE_MACOS=1 (make live-macos)"
else:
    _SKIP = ""

pytestmark = [pytest.mark.live, pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "enabled")]
darwin_only = pytest.mark.skipif(not ON_MAC, reason="needs macOS libproc")

WAIT = 60.0
HOST = default_host()


def wait_for(cond: Callable[[], bool], timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Recorder:
    """Records every process a test starts or learns of and stops exactly those at teardown."""

    def __init__(self) -> None:
        self.popens: list[subprocess.Popen[bytes]] = []
        self.groups: list[Identity] = []  # leaders whose process group the test created
        self.pids: list[Identity] = []  # other processes, with the identity read when announced

    def popen(self, argv: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        kw.setdefault("stdin", subprocess.DEVNULL)
        proc: subprocess.Popen[bytes] = subprocess.Popen(argv, **kw)  # type: ignore[call-overload]
        allow(proc.pid)
        self.popens.append(proc)
        return proc

    def group_of(self, proc: subprocess.Popen[bytes]) -> Identity:
        ident = identify(HOST, proc.pid)
        self.groups.append(ident)
        return ident

    def process(self, pid: int) -> Identity:
        ident = identify(HOST, pid)  # raises if it is not a live, readable process
        allow(pid)
        self.pids.append(ident)
        return ident

    def close(self) -> None:
        for leader in self.groups:
            signal_group(HOST, leader, signal.SIGKILL, allow_zombie=True)
        for ident in self.pids:
            signal_process(HOST, ident, signal.SIGKILL, allow_zombie=True)
        for proc in self.popens:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=WAIT)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


@pytest.fixture
def rec() -> Iterator[Recorder]:
    recorder = Recorder()
    try:
        yield recorder
    finally:
        recorder.close()


class Cli:
    """``jobsctl`` as a real process on a private state and config directory."""

    def __init__(self, tmp_path: Path, rec: Recorder, **config: Any) -> None:
        self.tmp = tmp_path
        self.rec = rec
        home = Path(os.environ["HOME"])
        cfg = home / ".config" / "jobsctl"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "config.yaml").write_text(json.dumps({"auto_run": False, **config}))
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
            "XDG_DATA_HOME": os.environ["XDG_DATA_HOME"],
            "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
            "LC_ALL": "C",
        }

    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-I", "-m", "jobsctl", *args]

    def run(self, *args: str, timeout: float = WAIT) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.argv(*args),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def json(self, *args: str) -> Any:
        res = self.run("--json", *args)
        assert res.returncode == 0, f"{args}: {res.stderr}"
        return json.loads(res.stdout)

    def add(self, *argv: str, flags: tuple[str, ...] = ()) -> str:
        return str(self.json("add", *flags, "--", *argv)["job"]["id"])

    def show(self, job_id: str) -> dict[str, Any]:
        found: dict[str, Any] = self.json("show", job_id)
        return found

    def finish(self, job_id: str) -> dict[str, Any]:
        """Run passes (the default cap is one job at a time, and nothing else starts a queued
        job) until the job is out of `queued`, `starting` and `running`; bounded."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            self.run("run")
            job = self.show(job_id)
            if job["status"] not in ("queued", "starting", "running"):
                return job
            time.sleep(0.3)
        pytest.fail(f"{job_id} did not finish: {self.show(job_id)['status']}")


@pytest.fixture
def cli(tmp_path: Path, rec: Recorder) -> Cli:
    return Cli(tmp_path, rec)


def read_pid(path: Path) -> int:
    assert wait_for(lambda: path.exists() and path.read_text().strip().isdigit())
    return int(path.read_text().strip())


# -- libproc and sysctl on a real Mac (checklist: identity, liveness, boot id) -------------------


@darwin_only
def test_proc_bsdinfo_layout_and_the_identity_of_this_process() -> None:
    assert gateway_proc.ctypes.sizeof(gateway_proc._ProcBsdInfo) == 136
    got = gateway_proc.DarwinLibproc().bsdinfo(os.getpid())
    assert got is not None
    ppid, pgid, sec, usec, zombie = got
    assert (ppid, pgid, zombie) == (os.getppid(), os.getpgrp(), False)
    assert abs(sec - time.time()) < 7 * 24 * 3600 and 0 <= usec < 1_000_000
    again = gateway_proc.DarwinLibproc().bsdinfo(os.getpid())
    assert again is not None and again[2:4] == (sec, usec)  # the start time is stable


@darwin_only
def test_boot_id_is_the_boot_session_uuid_and_stable() -> None:
    first = DarwinHost().boot_id()
    assert first == DarwinHost().boot_id()
    out = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    assert first == out.stdout.strip() and len(first) >= 32


@darwin_only
@pytest.mark.parametrize(
    ("script", "kind", "code", "sig"),
    [("exit 7", "exited", 7, None), ("kill -9 $$", "signaled", None, 9)],
)
def test_a_zombie_child_is_visible_and_its_status_is_read_without_reaping(
    rec: Recorder, script: str, kind: str, code: int | None, sig: int | None
) -> None:
    from jobsctl.shim_exit import exit_record

    child = rec.popen(["/bin/sh", "-c", script])
    lib = gateway_proc.DarwinLibproc()
    # the zombie state is what held-leader waiting, the identity check of a zombie shim
    # (Runner.adopt_shims) and signal_group(allow_zombie=True) all depend on
    assert wait_for(lambda: (lib.bsdinfo(child.pid) or (0, 0, 0, 0, False))[4]), "never a zombie"
    ident = Identity(HOST.boot_id(), child.pid, HOST.process_info(child.pid).start)  # type: ignore[union-attr]
    assert check_identity(HOST, ident).reason == "zombie"
    assert check_identity(HOST, ident, allow_zombie=True).state is Liveness.ALIVE
    got = gateway_proc.waitid_nowait(child.pid, nohang=True)
    assert got is not None and got.si_pid == child.pid
    record = exit_record(got.si_code, got.si_status)
    assert (record.kind, record.code, record.signal) == (kind, code, sig)
    # still a zombie after the read (nothing was reaped), then reaped by the exact PID
    assert gateway_proc.waitid_nowait(child.pid, nohang=True) is not None
    assert gateway_proc.reap_if_exited(child.pid) is True
    assert lib.bsdinfo(child.pid) is None
    with pytest.raises(ChildProcessError) as ei:
        gateway_proc.waitid_nowait(child.pid, nohang=True)
    assert ei.value.errno == errno.ECHILD


@darwin_only
def test_a_running_child_is_not_exited_and_a_blocking_wait_returns_after_it_exits(
    rec: Recorder,
) -> None:
    child = rec.popen(["/bin/sh", "-c", "sleep 1"])
    assert gateway_proc.waitid_nowait(child.pid, nohang=True) is None
    started = time.monotonic()
    got = gateway_proc.waitid_nowait(child.pid)
    assert got is not None and got.si_status == 0 and time.monotonic() - started < 30


@darwin_only
def test_process_groups_children_and_the_signal_fallback_without_pidfds(rec: Recorder) -> None:
    assert HOST.pidfd_supported() is False
    child = rec.popen(
        ["/bin/sh", "-c", "sleep 120 & echo $!; wait"], process_group=0, stdout=subprocess.PIPE
    )
    leader = rec.group_of(child)
    assert child.stdout is not None
    grand = int(child.stdout.readline().strip())
    rec.process(grand)
    assert os.getpgid(child.pid) == child.pid == os.getpgid(grand)
    members = {m.pid for m in HOST.scan_group(child.pid).members}
    assert {child.pid, grand} <= members
    assert child.pid in {m.pid for m in HOST.scan_children(os.getpid()).members}
    out = signal_group(HOST, leader, signal.SIGTERM)
    assert (out.result, out.method) == (Result.SENT, "killpg")
    assert wait_for(lambda: child.poll() is not None)
    assert child.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM)
    # the grandchild died of the same group signal; wait for it, never assume
    assert wait_for(
        lambda: not any(m.pid == grand and not m.zombie for m in HOST.scan_group(child.pid).members)
    )


@darwin_only
def test_a_reused_or_gone_identity_is_never_signaled(rec: Recorder) -> None:
    child = rec.popen(["/bin/sh", "-c", "sleep 60"])
    ident = rec.process(child.pid)
    wrong = Identity(ident.boot_id, ident.pid, ident.start + 1)
    assert signal_process(HOST, wrong, signal.SIGKILL).result is not Result.SENT
    assert child.poll() is None
    assert signal_process(HOST, ident, signal.SIGKILL).result is Result.SENT
    assert wait_for(lambda: child.poll() is not None)
    assert check_identity(HOST, ident).state is Liveness.DEAD


# -- the CLI end to end on a real shim (platform-neutral code; checklist: process behavior) ------


def test_exit_codes_and_a_death_by_signal_are_recorded(cli: Cli) -> None:
    failed = cli.add("sh", "-c", "exit 3")
    killed = cli.add("sh", "-c", "kill -9 $$")
    a, b = cli.finish(failed), cli.finish(killed)
    assert (a["status"], a["exit"]["kind"], a["exit"]["code"]) == ("failed", "exited", 3)
    assert (b["status"], b["exit"]["kind"], b["exit"]["signal"]) == ("failed", "signaled", 9)


def test_the_pass_exits_while_the_shim_keeps_the_job_running(cli: Cli) -> None:
    marker = cli.tmp / "done"
    job = cli.add("sh", "-c", f"sleep 4; touch {marker}")
    started = time.monotonic()
    assert cli.json("run")["started"] == [job]
    assert time.monotonic() - started < 3, "the pass waited for the job"
    assert not marker.exists()
    assert cli.finish(job)["status"] == "done" and marker.exists()


def test_a_leftover_in_the_job_group_is_counted_and_never_signaled(cli: Cli, rec: Recorder) -> None:
    pid_file = cli.tmp / "left.pid"
    job = cli.add("sh", "-c", f"sleep 120 & echo $! > {pid_file}")
    cli.json("run")
    ident = rec.process(read_pid(pid_file))  # recorded before anything else can happen to it
    done = cli.finish(job)
    assert done["status"] == "done" and done["leftovers"] == 1
    assert check_identity(HOST, ident).state is Liveness.ALIVE  # counted, not signaled


@pytest.mark.parametrize(
    ("script", "expected_signal"),
    [("exec sleep 300", 15), ("trap '' TERM; exec sleep 300", 9)],
    ids=["term", "term-ignored-then-kill"],
)
def test_cancel_signals_the_group_term_then_kill(
    cli: Cli, rec: Recorder, script: str, expected_signal: int
) -> None:
    pid_file = cli.tmp / "leader.pid"
    job = cli.add("sh", "-c", f"echo $$ > {pid_file}; {script}", flags=("--kill-grace", "2s"))
    cli.json("run")
    leader = rec.process(read_pid(pid_file))
    res = cli.run("cancel", "--force", "--actor", "human:tester", "--wait-timeout", "60s", job)
    assert res.returncode == 0, res.stderr
    done = cli.finish(job)
    assert done["status"] == "canceled"
    assert (done["exit"]["kind"], done["exit"]["signal"]) == ("signaled", expected_signal)
    assert check_identity(HOST, leader).state is Liveness.DEAD


def test_concurrent_adds_are_all_recorded_with_distinct_ranks(cli: Cli, rec: Recorder) -> None:
    """Many processes write the database at once (flock and SQLite locking on this volume)."""
    procs = [
        rec.popen(
            cli.argv("--json", "add", "--no-start", "--", "true"),
            env=cli.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(12)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=WAIT)
        assert proc.returncode == 0, err.decode(errors="replace")
    jobs = cli.json("list")["jobs"]
    assert len(jobs) == 12
    assert len({job["rank"] for job in jobs}) == 12


def test_run_loop_re_execs_in_place_and_stops_on_sigterm(tmp_path: Path, rec: Recorder) -> None:
    from tests.manual_helpers import config_file

    worker = Path(__file__).with_name("loop_worker.py")
    version, starts = tmp_path / "version", tmp_path / "starts.log"
    version.write_text("1.0.0")
    starts.write_text("")
    cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
    env = dict(os.environ, LOOP_VERSION_FILE=str(version), LOOP_LOG=str(starts))
    with open(tmp_path / "loop.out", "wb") as out, open(tmp_path / "loop.err", "wb") as err:
        loop = rec.popen(
            [sys.executable, "-I", str(worker), "--config", cfg, "--json", "run", "--loop"],
            env=env,
            stdout=out,
            stderr=err,
            cwd=str(tmp_path),
            start_new_session=True,
        )
    lines = lambda: [ln.split() for ln in starts.read_text().splitlines()]  # noqa: E731
    assert wait_for(lambda: len(lines()) >= 1), (tmp_path / "loop.err").read_text()
    out_file = tmp_path / "loop.out"
    assert wait_for(lambda: out_file.read_text().strip() != "")  # one pass done first
    version.write_text("2.0.0")
    assert wait_for(lambda: len(lines()) >= 2), (tmp_path / "loop.err").read_text()
    first, second = lines()[:2]
    assert first[0] == second[0] == str(loop.pid) and second[2] == "2.0.0"  # same PID, replaced
    assert loop.poll() is None
    loop.send_signal(signal.SIGTERM)
    assert loop.wait(timeout=WAIT) == 0
