"""The signal sequence and timeouts through an in-process shim with real jobs (docs/interface.md,
Signal sequence and Timeouts; design.md D10 and spike A4).

Jobs write their PIDs to files; every process a test announces is tracked with its identity
(`job_guard`) and killed exactly, never by name.
"""

from __future__ import annotations

import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jobsctl.config import Config
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Liveness, ProcessGone, check_identity
from jobsctl.store_db import OpenMode, Store
from tests.shim_helpers import JOB, attempt, exit_of, job, make_shim, seed_claimed
from tests.shim_real import JobGuard

WAIT = 60.0


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def _validated(validated_records: None) -> None:
    return None


@pytest.fixture(autouse=True)
def _guarded(job_guard: JobGuard) -> JobGuard:
    """Every test here may start a long job: a failure must not leak a `sleep 600`."""
    return job_guard


@dataclass
class Running:
    """A shim running in a thread with its own store."""

    shim: Any = None
    out: list[int] = field(default_factory=list)
    thread: threading.Thread | None = None

    def join(self, seconds: float = WAIT) -> int:
        assert self.thread is not None
        self.thread.join(seconds)
        assert not self.thread.is_alive(), "the shim did not finish"
        return self.out[0]


def start_shim(config: Config | None = None) -> Running:
    running = Running()

    def target() -> None:
        with Store.open(mode=OpenMode.CLI) as own:
            running.shim = make_shim(own, config)
            running.out.append(running.shim.run())

    running.thread = threading.Thread(target=target, daemon=True)
    running.thread.start()
    return running


def wait_for(predicate: Any, seconds: float = WAIT) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def read_pids(path: Path, count: int) -> list[int]:
    def ready() -> bool:
        return path.exists() and len(path.read_text().split()) >= count

    wait_for(ready)
    return [int(x) for x in path.read_text().split()[:count]]


def complete_lines(path: Path) -> list[str]:
    """The newline-terminated lines of `path` (a job may be mid-write of the last one)."""
    if not path.exists():
        return []
    raw = path.read_text()
    return raw[: raw.rfind("\n") + 1].splitlines()


def alive(ident: Any) -> bool:
    return check_identity(default_host(), ident).state is Liveness.ALIVE


def wait_status(store: Store, status: str) -> None:
    wait_for(lambda: job(store)["status"] == status)


def cfg(**raw: Any) -> Config:
    return Config.model_validate({"tail_flush_interval": "1s", **raw})


def py(script: str) -> list[str]:
    return [sys.executable, "-I", "-c", textwrap.dedent(script)]


def sh(script: str) -> list[str]:
    return ["sh", "-c", script]


SLEEPER = py("import time; time.sleep(600)")
TERM_IGNORER = py(
    """
    import signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(600)
    """
)


# -- timeouts -------------------------------------------------------------------------------------


def test_a_timeout_ends_the_job_failed_timeout_after_sigterm(store: Store) -> None:
    seed_claimed(store, SLEEPER, overrides={"timeout": 1, "kill_grace": 5})
    started = time.monotonic()
    assert make_shim(store).run() == 0
    elapsed = time.monotonic() - started
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "timeout")
    assert (attempt(store)["status"], attempt(store)["reason"]) == ("failed", "timeout")
    assert exit_of(store) == {"kind": "signaled", "code": None, "signal": 15, "core": False}
    assert 1 <= elapsed < 5  # the job obeyed SIGTERM: no grace wait


def test_the_timeout_holds_whatever_the_exit_status(store: Store) -> None:
    """A job that traps SIGTERM and exits 0 still ends `timeout`."""
    script = """
        import signal, sys, time
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        time.sleep(600)
    """
    seed_claimed(store, py(script), overrides={"timeout": 1})
    make_shim(store).run()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")
    assert exit_of(store)["code"] == 0  # type: ignore[index]


def test_the_timeout_comes_from_the_queue_when_the_job_has_no_override(store: Store) -> None:
    config = cfg(queues={"q": {"timeout": "1s"}})
    seed_claimed(store, SLEEPER)
    make_shim(store, config).run()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")


def test_timeout_none_overrides_the_queue_timeout(store: Store) -> None:
    config = cfg(queues={"q": {"timeout": "1s"}})
    seed_claimed(store, sh("sleep 2.5"), overrides={"timeout": "none"})
    make_shim(store, config).run()
    assert (job(store)["status"], job(store)["reason"]) == ("done", "exit")


@pytest.mark.parametrize(
    ("snapshot_timeout", "config_timeout", "script", "expected"),
    [
        (1, "60s", SLEEPER, ("failed", "timeout")),  # snapshot 1 s fires, not config 60 s
        (60, "1s", sh("sleep 2.5"), ("done", "exit")),  # the snapshot's 60 s holds, not the 1 s
    ],
)
def test_the_shims_snapshot_decides_not_the_config_it_would_read_now(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_timeout: int,
    config_timeout: str,
    script: list[str],
    expected: tuple[str, str],
) -> None:
    """The snapshot the shim records (`effective`) disagrees with the config it holds."""
    from jobsctl import shim as shim_module

    real = shim_module.effective_snapshot
    monkeypatch.setattr(
        shim_module,
        "effective_snapshot",
        lambda config, row: {**real(config, row), "timeout": snapshot_timeout},
    )
    seed_claimed(store, script)
    make_shim(store, cfg(queues={"q": {"timeout": config_timeout}})).run()
    assert (job(store)["status"], job(store)["reason"]) == expected


def test_the_clock_starts_at_running(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `running` commit is delayed by 2 s; the job (3 s in all) outlives a 2.5 s timeout
    counted from the claim but not one counted from `running`."""
    from jobsctl import shim as shim_module

    real = shim_module.begin_running

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(2)
        return real(*args, **kwargs)

    monkeypatch.setattr(shim_module, "begin_running", slow)
    seed_claimed(store, sh("sleep 3"), overrides={"timeout": 2.5})
    make_shim(store).run()
    assert (job(store)["status"], job(store)["reason"]) == ("done", "exit")


def test_a_job_that_exits_on_its_own_before_the_timeout_is_not_signaled(store: Store) -> None:
    seed_claimed(store, sh("exit 3"), overrides={"timeout": 30})
    make_shim(store).run()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "exit")
    assert exit_of(store)["code"] == 3  # type: ignore[index]


# -- escalation: a job that ignores SIGTERM, shell jobs with children (spike A4) ------------


def test_a_job_ignoring_sigterm_is_killed_after_two_graces(
    store: Store, job_guard: JobGuard
) -> None:
    seed_claimed(store, TERM_IGNORER, overrides={"timeout": 1, "kill_grace": 1})
    started = time.monotonic()
    make_shim(store).run()
    elapsed = time.monotonic() - started
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")
    assert exit_of(store)["signal"] == 9  # type: ignore[index]
    assert 3 <= elapsed < 20  # timeout 1 + SIGTERM grace 1 + SIGTERM-to-group grace 1


def test_kill_grace_comes_from_the_jobs_snapshot_between_steps(store: Store) -> None:
    seed_claimed(store, TERM_IGNORER, overrides={"timeout": 1, "kill_grace": 3})
    started = time.monotonic()
    make_shim(store, cfg(kill_grace="1s")).run()
    assert time.monotonic() - started >= 6.5  # 1 + 3 + 3, not the config's 1 s grace


def test_a_shell_job_with_children_is_cleaned_up_by_the_group_steps(
    store: Store, tmp_path: Path, job_guard: JobGuard
) -> None:
    """Spike A4: SIGTERM to a shell's exact PID leaves its children running; the group step stops
    them."""
    pids = tmp_path / "pids"
    script = f"sleep 600 & echo $! >> {pids}; sleep 600 & echo $! >> {pids}; wait"
    seed_claimed(store, sh(script), overrides={"timeout": 1, "kill_grace": 1})
    running = start_shim()
    kids = [job_guard.track(p) for p in read_pids(pids, 2)]
    assert running.join() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")
    wait_for(lambda: not any(alive(k) for k in kids))
    assert attempt(store)["leftovers"] == 0


def test_children_that_ignore_sigterm_get_the_sigkill_step(
    store: Store, tmp_path: Path, job_guard: JobGuard
) -> None:
    pids = tmp_path / "pids"
    kid = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
    script = f"{sys.executable} -I -c '{kid}' & echo $! >> {pids}; wait"
    seed_claimed(store, sh(script), overrides={"timeout": 1, "kill_grace": 1})
    running = start_shim()
    (kid_ident,) = [job_guard.track(p) for p in read_pids(pids, 1)]
    running.join()
    wait_for(lambda: not alive(kid_ident))
    assert attempt(store)["leftovers"] == 0


def test_a_job_that_traps_sigterm_and_spawns_is_still_stopped(
    store: Store, tmp_path: Path, job_guard: JobGuard
) -> None:
    """The leader traps SIGTERM and keeps spawning children into the group; the group steps and
    SIGKILL stop every one that is in the group."""
    pids = tmp_path / "pids"
    script = f"""
        import os, signal, subprocess, sys, time
        signal.signal(signal.SIGTERM, lambda *a: None)
        while True:
            p = subprocess.Popen(["sleep", "600"])
            open({str(pids)!r}, "a").write(f"{{p.pid}}\\n")
            time.sleep(0.2)
    """
    seed_claimed(store, py(script), overrides={"timeout": 1, "kill_grace": 1})
    running = start_shim()
    kids = []
    seen: set[int] = set()
    while running.thread.is_alive():  # type: ignore[union-attr]
        for text in complete_lines(pids):
            if int(text) not in seen:
                seen.add(int(text))
                try:
                    kids.append(job_guard.track(int(text)))
                except ProcessGone:
                    pass
        time.sleep(0.05)
    running.join()
    assert len(seen) >= 3 and kids
    wait_for(lambda: not any(alive(k) for k in kids))
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")


def test_a_process_that_left_the_group_is_never_signaled(
    store: Store, tmp_path: Path, job_guard: JobGuard
) -> None:
    """A daemon that called setsid is not in the job's group: the sequence never touches it."""
    pids = tmp_path / "pids"
    daemon = (
        "import os,time; os.setsid(); "
        f"open({str(pids)!r},'a').write(str(os.getpid())+'\\n'); time.sleep(600)"
    )
    script = f'{sys.executable} -I -c "{daemon}" & while [ ! -s {pids} ]; do sleep 0.05; done; wait'
    seed_claimed(store, sh(script), overrides={"timeout": 2, "kill_grace": 1})
    running = start_shim()
    (daemon_ident,) = [job_guard.track(p) for p in read_pids(pids, 1)]
    running.join()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")
    assert alive(daemon_ident)  # still running: it was not in the group


# -- SIGTERM to the shim and force requests -----------------------------------------


def test_sigterm_while_running_ends_the_job_terminated(store: Store, job_guard: JobGuard) -> None:
    seed_claimed(store, SLEEPER, overrides={"kill_grace": 5})
    running = start_shim()
    wait_status(store, "running")
    running.shim.request_stop()
    assert running.join() == 0
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "terminated")
    assert exit_of(store)["signal"] == 15  # type: ignore[index]


def test_a_duplicated_sigterm_does_not_restart_the_sequence(store: Store) -> None:
    seed_claimed(store, TERM_IGNORER, overrides={"kill_grace": 1})
    running = start_shim()
    wait_status(store, "running")
    started = time.monotonic()
    running.shim.request_stop()
    time.sleep(0.5)
    running.shim.request_stop()
    running.join()
    assert time.monotonic() - started < 20  # a restart per SIGTERM would not stay this short
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "terminated")


def test_sigterm_with_a_committed_force_request_ends_the_job_canceled_forced(
    store: Store,
) -> None:
    seed_claimed(store, SLEEPER)
    running = start_shim()
    wait_status(store, "running")
    force(store)
    running.shim.request_stop()  # `cancel --force`'s wake-up
    running.join()
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "forced")
    assert exit_of(store)["signal"] == 15  # type: ignore[index]


def force(store: Store) -> None:
    with Store.open(mode=OpenMode.CLI) as other, other.write() as tx:
        tx.execute(
            "UPDATE attempts SET force_requested_by = ? WHERE job_id = ?", ["human:test", JOB]
        )


def test_a_force_request_whose_sigterm_was_lost_is_found_at_the_next_tail_flush(
    store: Store,
) -> None:
    seed_claimed(store, SLEEPER)
    running = start_shim(cfg(tail_flush_interval="1s"))
    wait_status(store, "running")
    force(store)  # no SIGTERM at all
    running.join()
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "forced")


def test_a_force_request_beats_a_natural_exit_in_the_final_commit(
    store: Store, tmp_path: Path
) -> None:
    """The job exits 0 on its own (released by a flag file) right after the force request is
    committed: the end is `canceled` (`forced`) whichever of the shim's looks comes first."""
    flag = tmp_path / "go"
    seed_claimed(store, sh(f"while [ ! -e {flag} ]; do sleep 0.05; done"))
    running = start_shim()
    wait_status(store, "running")
    force(store)
    flag.write_text("")
    running.join()
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "forced")


def test_a_force_request_during_a_timeout_sequence_wins_in_the_final_commit(store: Store) -> None:
    seed_claimed(store, TERM_IGNORER, overrides={"timeout": 1, "kill_grace": 2})
    running = start_shim()
    wait_status(store, "running")
    time.sleep(1.8)  # the timeout sequence is under way
    force(store)
    running.join()
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "forced")


def test_sigterm_after_the_job_exited_does_not_change_its_end(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.shim_pump import Pump

    seed_claimed(store, sh("exit 0"))
    shim = make_shim(store)
    real = Pump.drain

    def drain_after_stop(self: Pump, *args: Any, **kwargs: Any) -> None:
        shim.request_stop()  # SIGTERM arrives once the leader has exited
        real(self, *args, **kwargs)

    monkeypatch.setattr(Pump, "drain", drain_after_stop)
    shim.run()
    assert (job(store)["status"], job(store)["reason"]) == ("done", "exit")


def test_the_job_is_in_no_group_signal_after_its_leader_was_reaped(store: Store) -> None:
    """The shim reaps the leader only after the sequence is done: the exit record exists and the
    leader's recorded identity is gone afterwards."""
    seed_claimed(store, SLEEPER, overrides={"timeout": 1})
    make_shim(store).run()
    ident = attempt(store)["job_identity"]
    assert ident
    from jobsctl.leases import parse_identity

    parsed = parse_identity(ident)
    assert parsed is not None and not alive(parsed)


# -- the result file written before the final commit ----------------------------------------------


def stop_before_the_final_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobsctl.shim import Shim

    monkeypatch.setattr(Shim, "_finish", lambda self, *args, **kwargs: None)


def result_of(store: Store) -> tuple[str, str | None]:
    from jobsctl.result_file import read_result

    record = read_result(store.paths.shims, JOB, 1)
    assert record is not None
    return record.status, record.reason


def test_the_result_file_of_a_timeout_says_failed_timeout(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_before_the_final_commit(monkeypatch)
    seed_claimed(store, SLEEPER, overrides={"timeout": 1})
    assert make_shim(store).run() == 1  # the final commit never happened: the file stays
    assert result_of(store) == ("failed", "timeout")


def test_the_result_file_of_a_sigterm_says_failed_terminated(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_before_the_final_commit(monkeypatch)
    seed_claimed(store, SLEEPER)
    running = start_shim()
    wait_status(store, "running")
    running.shim.request_stop()
    assert running.join() == 1
    assert result_of(store) == ("failed", "terminated")


def test_the_result_file_of_a_forced_end_keeps_the_natural_outcome(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced end is decided in the final commit; the file holds what the exit alone says."""
    stop_before_the_final_commit(monkeypatch)
    seed_claimed(store, SLEEPER)
    running = start_shim()
    wait_status(store, "running")
    force(store)
    running.shim.request_stop()
    assert running.join() == 1
    assert result_of(store) == ("failed", "signaled")  # SIGTERM killed it; not canceled/forced


# -- the loop's guards ----------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["terminated", "timeout", "forced"])
def test_a_leader_that_already_exited_never_starts_the_sequence(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    from jobsctl import gateway
    from jobsctl import shim as shim_module
    from jobsctl.proc_identity import Identity
    from jobsctl.shim_signals import SignalSequence
    from tests.proc_fakes import FakeHost, proc

    seed_claimed(store, ["true"])
    shim = make_shim(store)
    monkeypatch.setattr(shim, "_tick", lambda: None)
    monkeypatch.setattr(shim_module, "_leader_exited", lambda pid: True)
    host = FakeHost(procs={500: proc(500)}, pidfd_ok=False)
    seq = SignalSequence(
        host,
        Identity("boot-1", 500, 1000),
        kill_grace=1,
        leader_exited=lambda: False,
        own_child=lambda: True,
        now=gateway.monotonic,
        diag=lambda message: None,
    )
    running_job = shim_module._Job(None, 500, None, Identity("boot-1", 500, 1000), seq=seq)  # type: ignore[arg-type]
    if case == "terminated":
        shim.request_stop()
    elif case == "timeout":
        running_job.deadline = gateway.monotonic() - 1
    else:
        running_job.force_by = "human:test"
    shim._tick_job(running_job)
    shim._finish_sequence(running_job, None)  # type: ignore[arg-type]
    assert not seq.started and host.sent() == []
    err = capsys.readouterr().err
    assert ("SIGTERM received; the job has exited" in err) == (case == "terminated")


def test_an_error_in_a_sequence_step_is_logged_and_retried_not_fatal(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jobsctl.shim_signals import SignalSequence

    real = SignalSequence.advance
    calls: list[int] = []

    def flaky(self: SignalSequence) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        real(self)

    monkeypatch.setattr(SignalSequence, "advance", flaky)
    seed_claimed(store, SLEEPER, overrides={"timeout": 1})
    assert make_shim(store).run() == 0
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")
    err = capsys.readouterr().err
    assert "a signal sequence step failed (RuntimeError: boom)" in err
    assert "the output pump failed" not in err


class _AlwaysFailingSequence:
    """A started sequence whose every step raises: no host is involved, nothing is signaled."""

    started = True
    done = False
    cause = "timeout"

    def __init__(self) -> None:
        self.calls = 0

    def advance(self) -> None:
        self.calls += 1
        raise RuntimeError("persistent failure detail")


class _NoPump:
    def pump_once(self) -> None:
        return None


def _failing_job(seq: _AlwaysFailingSequence) -> Any:
    from jobsctl import shim as shim_module
    from jobsctl.proc_identity import Identity

    return shim_module._Job(None, 500, None, Identity("boot-1", 500, 1000), seq=seq)  # type: ignore[arg-type]


def test_a_persistently_failing_step_after_the_leader_exited_gives_up_with_bounded_diags(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_claimed(store, ["true"])
    shim = make_shim(store)
    monkeypatch.setattr(shim, "_tick", lambda: None)
    seq = _AlwaysFailingSequence()
    shim._finish_sequence(_failing_job(seq), _NoPump())  # type: ignore[arg-type]
    assert seq.calls == 30
    lines = [x for x in capsys.readouterr().err.splitlines() if "signal sequence" in x]
    assert len(lines) == 1 + 1  # the first failure, then the one line when giving up
    assert "persistent failure detail" in lines[0]
    assert "giving up" in lines[-1]


def test_a_persistently_failing_step_while_the_job_runs_keeps_retrying_with_rate_limited_diags(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_claimed(store, ["true"])
    shim = make_shim(store)
    monkeypatch.setattr(shim, "_tick", lambda: None)
    before = dict(job(store))
    seq = _AlwaysFailingSequence()
    running_job = _failing_job(seq)
    for _ in range(100):
        shim._tick_job(running_job)
    assert seq.calls == 100  # never gives up while the leader may still run
    lines = [x for x in capsys.readouterr().err.splitlines() if "signal sequence step" in x]
    assert len(lines) == 4  # failures 1, 31, 61 and 91
    assert dict(job(store)) == before  # the job record was not ended
