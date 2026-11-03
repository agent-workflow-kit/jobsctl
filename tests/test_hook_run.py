"""Hook processes for real: exit mapping, timeouts, SIGTERM-ignoring hooks, hostile output.

Every hook leader is recorded at launch and killed by its exact handle (``tests/hook_real.py``);
the environment is explicit and HOME/XDG are private (``tests/conftest.py``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobsctl.hook_env import build_hook_env
from jobsctl.hook_run import HookResult, HookTuning, run_hook
from jobsctl.proc_host import BootIdUnavailable, LinuxHost
from jobsctl.proc_linux import wait_exited_keep
from tests.conftest import validate_against
from tests.hook_real import (
    HookTracker,
    alive,
    recorded_pids,
    running_pids,
    tracker_fixture,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc and process groups")

tracker = pytest.fixture(tracker_fixture)
FAST = HookTuning(term_grace=0.4, kill_wait=3.0, drain=0.3, stop_interval=0.05)
SECRET = "s3cr3t-VALUE-9f2"


def mask(text: str) -> str:
    return text.replace(SECRET, "***")


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


def hook_env(**extra: str) -> dict[str, str]:
    base = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **extra}
    return build_hook_env(base, hook="pre-start", job_id="jb-abcdefgh")


def run(
    cmd: str,
    state_dir: Path,
    *,
    hook: str = "pre-start",
    payload: bytes = b'{"x":1}\n',
    timeout: float = 10,
    env: dict[str, str] | None = None,
    **kw: object,
) -> HookResult:
    return run_hook(
        hook=hook,
        hook_id="h1",
        cmd=cmd,
        payload=payload,
        timeout=timeout,
        env=env if env is not None else hook_env(),
        cwd=state_dir,
        mask=mask,
        tuning=kw.pop("tuning", FAST),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# --- outcomes ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hook", "code", "outcome"),
    [
        ("pre-start", 0, "allow"),
        ("pre-start", 13, "escalate"),
        ("pre-start", 1, "veto"),
        ("pre-start", 2, "veto"),
        ("pre-start", 12, "veto"),
        ("pre-start", 14, "veto"),
        ("pre-start", 255, "veto"),
        ("post-fail", 0, "allow"),
        ("post-fail", 1, "failed"),
        ("post-fail", 13, "failed"),  # exit 13 escalates only a pre-start hook
        ("post-start", 255, "failed"),
    ],
)
def test_exit_status_maps_to_the_outcome(
    tracker: HookTracker, state_dir: Path, hook: str, code: int, outcome: str
) -> None:
    res = run(f"exit {code}", state_dir, hook=hook)
    assert (res.outcome, res.exit, res.signal) == (outcome, code, None)
    assert res.stopped is None and res.start_error is None
    assert res.vetoes is (hook == "pre-start" and outcome == "veto")
    assert res.escalates is (outcome == "escalate")
    assert tracker.last.popen.returncode == code  # the leader was reaped


def test_the_payload_arrives_on_stdin_and_cwd_is_the_state_dir(
    tracker: HookTracker, state_dir: Path
) -> None:
    res = run("pwd >&2; cat >&2; exit 13", state_dir, payload=b'{"hello":"w\xc3\xb6rld"}\n')
    assert res.outcome == "escalate"
    assert res.message == f'{state_dir}\n{{"hello":"w\xf6rld"}}\n'
    assert res.stderr == res.message


def test_the_command_is_run_by_bin_sh_c_and_never_expanded(
    tracker: HookTracker, state_dir: Path
) -> None:
    res = run('echo "$0 $#" >&2; exit 1', state_dir)
    assert res.message == "/bin/sh 0\n" or res.message == "sh 0\n"
    assert tracker.last.popen.args == ["/bin/sh", "-c", 'echo "$0 $#" >&2; exit 1']


def test_a_hook_gets_exactly_the_given_environment(tracker: HookTracker, state_dir: Path) -> None:
    os.environ["LEAKED_FROM_PARENT"] = "yes"
    try:
        res = run("env | sort >&2; exit 1", state_dir, env=hook_env(TOKEN="t"))
    finally:
        del os.environ["LEAKED_FROM_PARENT"]
    assert res.message is not None
    names = {line.split("=", 1)[0] for line in res.message.splitlines()}
    assert {"JOBSCTL_HOOK", "JOBSCTL_ID", "JOBSCTL_OBJECT_TYPE", "TOKEN", "PATH"} <= names
    assert "LEAKED_FROM_PARENT" not in names and "XDG_STATE_HOME" not in names
    assert "JOBSCTL_HOOK=pre-start" in res.message and "JOBSCTL_ID=jb-abcdefgh" in res.message
    assert "JOBSCTL_OBJECT_TYPE=job" in res.message


def test_the_hook_runs_in_its_own_process_group(tracker: HookTracker, state_dir: Path) -> None:
    res = run('ps -o pgid= -p $$ >&2; echo "$$" >&2; exit 1', state_dir)
    assert res.message is not None
    pgid, pid = res.message.split()
    assert pgid == pid == str(tracker.last.popen.pid)
    assert os.getpgid(0) != tracker.last.popen.pid


# --- decision ids and messages ---------------------------------------------------------------


def test_decision_ids_come_from_json_stdout(tracker: HookTracker, state_dir: Path) -> None:
    res = run("""echo '{"decision_id": ["pol-1", "bad id", 7, "pol-2"]}'""", state_dir)
    assert res.outcome == "allow" and res.decision_ids == ("pol-1", "pol-2")
    assert [w.code for w in res.warnings] == ["invalid_decision_id"] * 2
    assert all(len(w.detail.encode()) <= 64 for w in res.warnings)


def test_decision_ids_are_kept_for_a_vetoing_hook_too(
    tracker: HookTracker, state_dir: Path
) -> None:
    res = run("""echo '{"decision_id": "why-1"}'; echo nope >&2; exit 1""", state_dir)
    assert res.outcome == "veto" and res.decision_ids == ("why-1",)
    assert res.message == "nope\n"


def test_a_timed_out_hook_yields_no_decision_ids(tracker: HookTracker, state_dir: Path) -> None:
    res = run("""echo '{"decision_id": "x"}'; sleep 30""", state_dir, timeout=0.3)
    assert res.outcome == "timeout" and res.decision_ids == ()


def test_message_only_for_a_pre_start_hook_that_did_not_allow(
    tracker: HookTracker, state_dir: Path
) -> None:
    assert run("echo hi >&2", state_dir).message is None
    assert run("echo hi >&2; exit 1", state_dir, hook="post-fail").message is None
    assert run("echo hi >&2; exit 1", state_dir, hook="post-fail").stderr == "hi\n"
    assert run("echo hi >&2", state_dir).stderr is None


def test_stderr_and_message_limits_and_masking(tracker: HookTracker, state_dir: Path) -> None:
    cmd = f"head -c 20000 /dev/zero | tr '\\0' e >&2; echo; echo {SECRET} >&2; exit 1"
    res = run(cmd, state_dir)
    assert res.stderr is not None and res.message is not None
    assert len(res.stderr.encode()) <= 2048 and len(res.message.encode()) <= 4096
    cmd = f"echo 'token {SECRET} end' >&2; exit 13"
    res = run(cmd, state_dir)
    assert res.message == "token *** end\n" and SECRET not in (res.stderr or "")


def test_hostile_stderr_is_sanitized(tracker: HookTracker, state_dir: Path) -> None:
    cmd = (
        r"printf '\033]0;title\007\033[31mred\033[0m\000nul \377bad \342\200\256rtl"
        r"\r\nline2\n' >&2; exit 1"
    )
    res = run(cmd, state_dir)
    assert res.message is not None
    for bad in ("\x1b", "\x00", "\x07", "‮", "\r"):
        assert bad not in res.message
    assert res.message == "red\\u0000nul \\xffbad \\u202ertl\nline2\n"


# --- log fields ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "timeout"),
    [("exit 0", 10), ("echo e >&2; exit 13", 10), ("echo e >&2; exit 4", 10), ("sleep 30", 0.2)],
)
def test_log_fields_validate_against_the_log_schema(
    tracker: HookTracker, state_dir: Path, cmd: str, timeout: float
) -> None:
    res = run(cmd, state_dir, timeout=timeout)
    record = {
        "seq": 1,
        "ts": "2026-11-03T12:00:00.000Z",
        "level": "info",
        **res.log_fields("jb-abcdefgh"),
    }
    validate_against("log.json", record)
    assert ("stderr" in record) == (res.outcome != "allow")


# --- timeouts and kills ----------------------------------------------------------------------


def test_timeout_sends_sigterm_and_reaps_the_leader(tracker: HookTracker, state_dir: Path) -> None:
    began = time.monotonic()
    res = run("sleep 30", state_dir, timeout=0.3)
    assert time.monotonic() - began < 5
    assert (res.outcome, res.exit) == ("timeout", None) and res.vetoes
    assert res.signals_sent == ("SIGTERM",) and not res.unkillable
    assert res.leftovers == 0
    assert tracker.last.popen.returncode is not None


def test_a_hook_ignoring_sigterm_and_its_children_end_only_at_sigkill(
    tracker: HookTracker, state_dir: Path
) -> None:
    pidfile = state_dir / "pids"
    cmd = (
        f"trap '' TERM; sleep 60 & echo $! >> {pidfile}; sleep 60 & echo $! >> {pidfile}; sleep 60"
    )
    began = time.monotonic()
    res = run(cmd, state_dir, timeout=0.5)
    elapsed = time.monotonic() - began
    pids = recorded_pids(pidfile)
    assert len(pids) == 2
    assert res.outcome == "timeout" and res.signals_sent == ("SIGTERM", "SIGKILL")
    assert 0.5 + FAST.term_grace <= elapsed < 6
    assert res.leftovers == 0 and not res.unkillable
    assert running_pids(pids) == []  # no survivors
    assert tracker.last.popen.returncode is not None


def test_sigterm_ignoring_children_are_killed_even_if_the_leader_exits_at_sigterm(
    tracker: HookTracker, state_dir: Path
) -> None:
    pidfile = state_dir / "pids"
    # the child traps TERM and survives the leader's death; only SIGKILL to the group ends it
    cmd = f"(trap '' TERM; exec sleep 60) & echo $! > {pidfile}; sleep 60"
    res = run(cmd, state_dir, timeout=0.3)
    assert res.signals_sent == ("SIGTERM", "SIGKILL")
    pids = recorded_pids(pidfile)
    assert len(pids) == 1 and running_pids(pids) == []


def test_a_hook_that_forks_and_exits_leaves_its_child_alone(
    tracker: HookTracker, state_dir: Path
) -> None:
    pidfile = state_dir / "pids"
    began = time.monotonic()
    res = run(f"sleep 60 & echo $! > {pidfile}", state_dir)
    elapsed = time.monotonic() - began
    kids = tracker.adopt(pidfile)
    # the child holds stdout and stderr open: the drain gives up instead of waiting for EOF
    assert res.outcome == "allow" and elapsed < 5
    assert res.signals_sent == () and res.leftovers == 1
    assert len(kids) == 1 and alive(kids[0])


def test_a_grandchild_holding_the_pipes_open_never_blocks_the_hook(
    tracker: HookTracker, state_dir: Path
) -> None:
    pidfile = state_dir / "pids"
    res = run(f"sleep 60 & echo $! > {pidfile}; echo out; echo err >&2; exit 1", state_dir)
    tracker.adopt(pidfile)
    assert res.outcome == "veto" and res.message == "err\n"


def test_a_hook_that_dies_instantly_or_by_signal(tracker: HookTracker, state_dir: Path) -> None:
    res = run("kill -KILL $$", state_dir)
    assert (res.outcome, res.exit, res.signal) == ("veto", None, signal.SIGKILL)
    res = run("kill -TERM $$", state_dir, hook="post-held")
    assert (res.outcome, res.exit, res.signal) == ("failed", None, signal.SIGTERM)
    res = run("", state_dir)  # an empty command is a successful shell
    assert res.outcome == "allow"


def test_zero_length_output_and_a_hook_that_never_reads_stdin(
    tracker: HookTracker, state_dir: Path
) -> None:
    big = b'{"pad":"' + b"x" * (2 * 1024 * 1024) + b'"}\n'
    res = run("exit 0", state_dir, payload=big)
    assert res.outcome == "allow" and res.stdout_truncated is False and res.decision_ids == ()
    res = run("sleep 30", state_dir, payload=big, timeout=0.3)  # blocked write must not hang
    assert res.outcome == "timeout"


def test_a_large_payload_is_delivered_whole(tracker: HookTracker, state_dir: Path) -> None:
    big = b'{"pad":"' + b"y" * (1024 * 1024) + b'"}\n'
    res = run("wc -c >&2; exit 13", state_dir, payload=big)
    assert res.message is not None and int(res.message) == len(big)


def test_huge_output_is_capped_and_counted_without_blocking_the_hook(
    tracker: HookTracker, state_dir: Path
) -> None:
    cmd = "head -c 10000000 /dev/zero | tr '\\0' x; head -c 10000000 /dev/zero | tr '\\0' y >&2"
    res = run(cmd, state_dir, timeout=30)
    assert res.outcome == "allow"
    assert res.stdout_truncated and res.stderr_truncated


def test_stdout_exactly_at_the_cap_is_not_truncated(tracker: HookTracker, state_dir: Path) -> None:
    res = run("head -c 65536 /dev/zero | tr '\\0' x", state_dir)
    assert not res.stdout_truncated
    res = run("head -c 65537 /dev/zero | tr '\\0' x", state_dir)
    assert res.stdout_truncated


def test_start_errors(tracker: HookTracker, state_dir: Path) -> None:
    res = run("exit 0", state_dir / "missing")
    assert res.outcome == "failed" and res.vetoes and res.start_error
    assert res.message is not None and "could not be started" in res.message
    assert tracker.spawned == []
    res = run("exit 0", state_dir, hook="post-fail", env={"A": "x\x00y"})
    assert res.outcome == "failed" and not res.vetoes and res.message is None


# --- stop requests ---------------------------------------------------------------------------


def test_stop_before_start_runs_nothing(tracker: HookTracker, state_dir: Path) -> None:
    res = run("exit 0", state_dir, stop=lambda: "canceled")
    assert res.stopped == "canceled" and tracker.spawned == [] and not res.vetoes


def test_stop_kills_the_group_and_reports_the_reason(tracker: HookTracker, state_dir: Path) -> None:
    pidfile = state_dir / "pids"
    t0 = time.monotonic()
    res = run(
        f"trap '' TERM; sleep 60 & echo $! > {pidfile}; sleep 60",
        state_dir,
        stop=lambda: "canceled" if time.monotonic() - t0 > 0.3 else None,
    )
    pids = recorded_pids(pidfile)
    assert res.stopped == "canceled" and res.outcome == "failed" and not res.vetoes
    assert res.signals_sent == ("SIGTERM", "SIGKILL") and res.leftovers == 0
    assert len(pids) == 1 and running_pids(pids) == []
    assert tracker.last.popen.returncode is not None


def test_stop_is_polled_at_least_every_interval(tracker: HookTracker, state_dir: Path) -> None:
    calls: list[float] = []

    def stop() -> str | None:
        calls.append(time.monotonic())
        return "x" if len(calls) > 6 else None

    run("sleep 30", state_dir, stop=stop)
    gaps = [b - a for a, b in zip(calls[1:], calls[2:], strict=False)]
    assert gaps and max(gaps) < 0.5


# --- reaping and signalling discipline -------------------------------------------------------


class SpyHost(LinuxHost):
    """A real host that records group signals and whether the leader was still unreaped."""

    def __init__(self, tracker: HookTracker, no_boot_id: bool = False) -> None:
        super().__init__()
        self.tracker = tracker
        self.calls: list[tuple[int, int, bool]] = []
        self.no_boot_id = no_boot_id

    def boot_id(self) -> str:
        if self.no_boot_id:
            raise BootIdUnavailable
        return super().boot_id()

    def killpg(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, sig, self.tracker.last.popen.returncode is None))
        super().killpg(pgid, sig)


def test_group_signals_go_only_to_the_hooks_group_while_the_leader_is_unreaped(
    tracker: HookTracker, state_dir: Path
) -> None:
    host = SpyHost(tracker)
    res = run("trap '' TERM; sleep 60", state_dir, timeout=0.3, host=host)
    assert res.signals_sent == ("SIGTERM", "SIGKILL")
    pid = tracker.last.popen.pid
    assert host.calls and all(c[0] == pid and c[2] for c in host.calls)
    assert [c[1] for c in host.calls] == [signal.SIGTERM, signal.SIGKILL]


def test_without_an_identity_the_leader_is_still_signaled_as_our_unreaped_child(
    tracker: HookTracker, state_dir: Path
) -> None:
    host = SpyHost(tracker, no_boot_id=True)
    res = run("sleep 60", state_dir, timeout=0.3, host=host)
    assert res.outcome == "timeout"
    assert host.calls == [(tracker.last.popen.pid, signal.SIGTERM, True)]


def test_only_the_hook_leader_is_reaped_never_a_sibling(
    tracker: HookTracker, state_dir: Path
) -> None:
    sibling = subprocess.Popen(["/bin/sh", "-c", "exit 7"])
    try:
        assert wait_exited_keep(sibling.pid) is not None  # an exited, unreaped sibling child
        res = run("exit 0", state_dir)
        assert res.outcome == "allow"
        assert wait_exited_keep(sibling.pid, nohang=True) is not None  # still a zombie of ours
        assert sibling.wait(timeout=5) == 7  # and it still has its status
    finally:
        if sibling.returncode is None:
            sibling.kill()


def test_an_exception_inside_the_supervisor_kills_and_reaps_the_group(
    tracker: HookTracker, state_dir: Path
) -> None:
    def boom() -> str | None:
        if tracker.spawned:
            raise RuntimeError("stop callback bug")
        return None

    with pytest.raises(RuntimeError):
        run("trap '' TERM; sleep 60", state_dir, stop=boom)
    assert tracker.last.popen.returncode is not None  # killed and reaped


def test_hook_result_is_json_friendly_in_its_log_fields(
    tracker: HookTracker, state_dir: Path
) -> None:
    res = run("""echo '{"decision_id":"a"}'""", state_dir)
    assert json.loads(json.dumps(res.log_fields("jb-abcdefgh")))["decision_ids"] == ["a"]
