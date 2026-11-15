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


def test_the_shims_snapshot_decides_not_the_config_it_would_read_now(store: Store) -> None:
    seed_claimed(store, sh("sleep 2.5"), overrides={"timeout": 60})
    make_shim(store, cfg(queues={"q": {"timeout": "1s"}})).run()  # override 60 wins
    assert job(store)["status"] == "done"


def test_the_clock_starts_at_running(store: Store, tmp_path: Path) -> None:
    """The job runs for longer than its timeout in total only if the clock had started earlier:
    here the whole run (about 1.5 s) is within a 3 s timeout."""
    seed_claimed(store, sh("sleep 1.5"), overrides={"timeout": 3})
    make_shim(store).run()
    assert (job(store)["status"], job(store)["reason"]) == ("done", "exit")


def test_a_job_that_exits_on_its_own_before_the_timeout_is_not_signaled(store: Store) -> None:
    seed_claimed(store, sh("exit 3"), overrides={"timeout": 30})
    make_shim(store).run()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "exit")
    assert exit_of(store)["code"] == 3  # type: ignore[index]


# -- escalation: a job that ignores SIGTERM, shell jobs with children (spike A4) ---------------------


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
        for text in pids.read_text().split() if pids.exists() else []:
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
        "import os,time; os.setsid(); open(%r,'a').write(str(os.getpid())+'\\n'); time.sleep(600)"
        % str(pids)
    )
    script = f'{sys.executable} -I -c "{daemon}" & while [ ! -s {pids} ]; do sleep 0.05; done; wait'
    seed_claimed(store, sh(script), overrides={"timeout": 2, "kill_grace": 1})
    running = start_shim()
    (daemon_ident,) = [job_guard.track(p) for p in read_pids(pids, 1)]
    running.join()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "timeout")
    assert alive(daemon_ident)  # still running: it was not in the group


# -- SIGTERM to the shim and force requests ------------------------------------------------------------


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
    assert time.monotonic() - started < 6  # one SIGTERM grace + one more, SIGKILL
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


def test_a_force_request_beats_a_natural_exit_in_the_final_commit(store: Store) -> None:
    """The job exits 0 while the force request is committed: the end is `canceled` (`forced`)."""
    seed_claimed(store, sh("sleep 1.2"))
    running = start_shim()
    wait_status(store, "running")
    force(store)
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
