"""`_kill` over a fake host: who is signaled, and when (docs/interface.md, Shim loss).

The host is scripted, so every PID, start time and exit is chosen by the test and nothing real is
signaled. The invariants: a signal is sent only while the recorded leader's identity holds, never
once it is gone, never to a process outside the job's group, and the job is finalized `canceled`
(`forced`, exit kind `unknown`) in one compare-and-set on the claim.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from jobsctl import gateway_proc
from jobsctl.config import Config
from jobsctl.kill import Killer
from jobsctl.leases import expiry
from jobsctl.result_file import ResultRecord, ResultTail, write_result
from jobsctl.shim import StopSignal
from jobsctl.shim_exit import ExitRecord
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_ops import take_lease
from tests.proc_fakes import FakeHost, proc
from tests.shim_helpers import CLAIM, JOB, attempt, job, log_records, seed_claimed, shims_dir

ME = os.getpid()
SHIM = 900
LEADER = 500
MEMBER = 501  # in the job's group
STRANGER = 777  # not in the job's group
START = 1000
BOOT = "boot-1"


def ident(pid: int, start: int = START) -> str:
    return f"{BOOT}:{pid}:{start}"


class Host(FakeHost):
    """A fake host whose signals have effects: `reactions` maps (call, pid, sig) to a callback."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.reactions: dict[tuple[str, int, int], Callable[[], None]] = {}

    def kill(self, pid: int, sig: int) -> None:
        super().kill(pid, sig)
        self.reactions.get(("kill", pid, sig), lambda: None)()

    def killpg(self, pgid: int, sig: int) -> None:
        super().killpg(pgid, sig)
        self.reactions.get(("killpg", pgid, sig), lambda: None)()

    def pidfd_send_signal(self, fd: int, sig: int, flags: int = 0) -> None:
        pid = self._fds[fd]
        super().pidfd_send_signal(fd, sig, flags)
        kind = "pidfd-group" if flags else "pidfd"
        self.reactions.get((kind, pid, sig), lambda: None)()


@dataclass
class Rig:
    store: Store
    host: Host
    now: float = 0.0
    stop: StopSignal = field(default_factory=StopSignal)
    on_sleep: Callable[[], None] = lambda: None

    def killer(self, config: Config | None = None) -> Killer:
        def sleep(seconds: float) -> None:
            self.now += seconds
            self.on_sleep()

        return Killer(
            self.store,
            config or Config(auto_run=False),
            None,
            JOB,
            host=self.host,
            stop=self.stop,
            monotonic=lambda: self.now,
            sleep=sleep,
        )

    def die(self, *pids: int) -> Callable[[], None]:
        def go() -> None:
            for pid in pids:
                self.host.procs.pop(pid, None)

        return go

    def signals(self) -> list[tuple]:
        return self.host.sent()


@pytest.fixture
def rig() -> Iterator[Rig]:
    yield from _rig(pidfd_ok=False)


@pytest.fixture
def pidfd_rig() -> Iterator[Rig]:
    """A host with pidfds whose kernel has PIDFD_SIGNAL_PROCESS_GROUP (faked: 6.9+)."""
    yield from _rig(pidfd_ok=True)


@pytest.fixture
def old_kernel_rig() -> Iterator[Rig]:
    """A host with pidfds whose kernel lacks the flag (EINVAL), like Linux before 6.9."""
    yield from _rig(
        pidfd_ok=True,
        pidfd_send_error={gateway_proc.PIDFD_SIGNAL_PROCESS_GROUP: OSError(errno.EINVAL, "x")},
    )


def _rig(**host_kw: Any) -> Iterator[Rig]:
    host = Host(
        procs={
            ME: proc(ME),
            LEADER: proc(LEADER),
            MEMBER: proc(MEMBER, pgid=LEADER),
            STRANGER: proc(STRANGER, pgid=STRANGER),
        },
        **host_kw,
    )
    with Store.open(mode=OpenMode.CLI) as store:
        seed_claimed(store, ["sleep", "600"], overrides={"kill_grace": 10})
        set_running(store)
        yield Rig(store, host)


def set_running(
    store: Store,
    *,
    shim: str | None = ident(SHIM),
    leader: str | None = ident(LEADER),
    pgid: int | None = LEADER,
    force: str | None = "human:t",
    effective: dict[str, Any] | None = None,
) -> None:
    with store.write() as tx:
        tx.execute("UPDATE jobs SET status = 'running' WHERE id = ?", [JOB])
        tx.execute(
            "UPDATE attempts SET status = 'running', shim_identity = ?, job_identity = ?, "
            "pgid = ?, force_requested_by = ?, effective = ? WHERE job_id = ?",
            [shim, leader, pgid, force, json.dumps(effective or {"kill_grace": 10}), JOB],
        )


def ended(rig: Rig) -> tuple[str, str | None, str | None]:
    row = job(rig.store)
    return row["status"], row["reason"], attempt(rig.store)["status"]


def exit_kind(rig: Rig) -> str:
    return json.loads(attempt(rig.store)["exit"])["kind"]


def untouched(rig: Rig) -> None:
    assert job(rig.store)["status"] == "running"
    assert rig.signals() == []


def lease(rig: Rig) -> Any:
    return rig.store.query_one("SELECT * FROM leases WHERE name = ?", [f"job:{JOB}"])


# -- nothing to signal -------------------------------------------------------------------------


def test_a_gone_leader_is_not_signaled_and_the_job_is_finalized_forced(rig: Rig) -> None:
    del rig.host.procs[LEADER]
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert ended(rig) == ("canceled", "forced", "canceled")
    assert exit_kind(rig) == "unknown"
    assert attempt(rig.store)["leftovers"] is None  # the group could not be probed
    assert lease(rig) is None


def test_a_reused_leader_pid_is_not_signaled(rig: Rig) -> None:
    rig.host.procs[LEADER] = proc(LEADER, start=START + 5)  # another process holds the PID now
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert ended(rig)[:2] == ("canceled", "forced")


def test_a_zombie_leader_counts_as_gone_and_nothing_is_signaled(rig: Rig) -> None:
    rig.host.procs[LEADER] = proc(LEADER, zombie=True)
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert ended(rig)[:2] == ("canceled", "forced")


def test_a_leader_from_another_boot_is_not_signaled(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET job_identity = ?", ["boot-0:500:1000"])
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert ended(rig)[:2] == ("canceled", "forced")


def test_an_unreadable_leader_identity_signals_nothing_and_finalizes_nothing(rig: Rig) -> None:
    rig.host.info_errors[LEADER] = PermissionError("hidepid")
    assert rig.killer().run() == 1
    untouched(rig)
    assert lease(rig) is None


def test_a_recorded_group_that_is_not_the_leaders_is_never_signaled(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET pgid = ?", [STRANGER])
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert ended(rig)[:2] == ("canceled", "forced")


# -- re-checks: eligibility --------------------------------------------------------------------


def test_a_live_shim_means_nothing_to_do(rig: Rig) -> None:
    rig.host.procs[SHIM] = proc(SHIM)
    assert rig.killer().run() == 0
    untouched(rig)
    assert lease(rig) is None


def test_an_unreadable_shim_identity_is_not_proof_of_loss(rig: Rig) -> None:
    rig.host.procs[SHIM] = proc(SHIM)
    rig.host.info_errors[SHIM] = PermissionError("hidepid")
    assert rig.killer().run() == 0
    untouched(rig)


def test_an_attempt_without_a_usable_shim_identity_is_left_alone(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET shim_identity = 'garbage'")
    assert rig.killer().run() == 0
    untouched(rig)


def test_no_committed_force_request_means_nothing_is_signaled(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE attempts SET force_requested_by = NULL")
    assert rig.killer().run() == 0
    untouched(rig)


def test_a_job_that_is_not_running_is_left_alone(rig: Rig) -> None:
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET status = 'done', reason = 'exit'")
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert job(rig.store)["status"] == "done"


def test_an_unknown_job_is_a_clean_exit() -> None:
    with Store.open(mode=OpenMode.CLI) as store:
        host = Host(procs={ME: proc(ME)})
        assert Killer(store, Config(auto_run=False), None, "jb-zzzzzzzz", host=host).run() == 0
        assert host.sent() == []


def test_a_live_lease_holder_means_a_duplicate_kill_exits_untouched(rig: Rig) -> None:
    rig.host.procs[700] = proc(700)
    assert take_lease(
        rig.store,
        f"job:{JOB}",
        ident(700),
        now="2026-01-01T00:00:00.000Z",
        expires_at=expiry(dt.datetime.now(dt.UTC)),
        is_alive=lambda holder: True,
    )
    assert rig.killer().run() == 0
    untouched(rig)
    assert lease(rig)["holder_identity"] == ident(700)  # not released by the loser


def test_a_dead_lease_holder_is_replaced(rig: Rig) -> None:
    assert take_lease(
        rig.store,
        f"job:{JOB}",
        ident(701),  # not in procs: dead
        now="2026-01-01T00:00:00.000Z",
        expires_at="2999-01-01T00:00:00.000Z",
        is_alive=lambda holder: True,
    )
    del rig.host.procs[LEADER]
    assert rig.killer().run() == 0
    assert ended(rig)[:2] == ("canceled", "forced")


# -- the sequence ------------------------------------------------------------------------------


def test_a_leader_that_obeys_sigterm_gets_one_signal_to_its_pid(rig: Rig) -> None:
    rig.host.reactions[("kill", LEADER, signal.SIGTERM)] = rig.die(LEADER)
    assert rig.killer().run() == 0
    assert rig.signals() == [("kill", LEADER, signal.SIGTERM)]
    assert ended(rig) == ("canceled", "forced", "canceled")
    assert exit_kind(rig) == "unknown"


def test_a_stubborn_leader_gets_sigterm_then_the_group_then_sigkill_after_each_grace(
    rig: Rig,
) -> None:
    rig.host.reactions[("killpg", LEADER, signal.SIGKILL)] = rig.die(LEADER, MEMBER)
    assert rig.killer().run() == 0
    assert rig.signals() == [
        ("kill", LEADER, signal.SIGTERM),
        ("killpg", LEADER, signal.SIGTERM),
        ("killpg", LEADER, signal.SIGKILL),  # the leader is gone after it: no PID signal follows
    ]
    assert rig.now >= 20  # two graces of 10 seconds
    assert ended(rig)[:2] == ("canceled", "forced")
    assert attempt(rig.store)["leftovers"] == 0


def test_only_the_leader_and_its_group_are_ever_signaled(rig: Rig) -> None:
    rig.host.reactions[("killpg", LEADER, signal.SIGKILL)] = rig.die(LEADER, MEMBER)
    rig.killer().run()
    targets = {call[1] for call in rig.signals()}
    assert targets == {LEADER}  # not the member by PID, not the stranger, not the shim
    assert STRANGER in rig.host.procs


def test_no_signal_is_sent_once_the_leader_is_gone_and_members_become_leftovers(
    rig: Rig,
) -> None:
    rig.host.reactions[("kill", LEADER, signal.SIGTERM)] = rig.die(LEADER)
    assert rig.killer().run() == 0
    assert rig.signals() == [("kill", LEADER, signal.SIGTERM)]  # the member was not signaled
    assert attempt(rig.store)["leftovers"] == 1
    warn = log_records("leftovers")
    assert warn and warn[0]["level"] == "warn"


# -- PIDFD_SIGNAL_PROCESS_GROUP (#35): the kernel is faked; the real one needs Linux 6.9+ -------

FLAG = gateway_proc.PIDFD_SIGNAL_PROCESS_GROUP


def zombify(rig: Rig, pid: int) -> Callable[[], None]:
    def go() -> None:
        rig.host.procs[pid] = proc(pid, start=START, zombie=True)

    return go


def test_a_live_leader_gets_its_group_signals_through_the_pidfd_flag(pidfd_rig: Rig) -> None:
    pidfd_rig.host.reactions[("pidfd-group", LEADER, signal.SIGKILL)] = pidfd_rig.die(
        LEADER, MEMBER
    )
    assert pidfd_rig.killer().run() == 0
    assert pidfd_rig.signals() == [
        ("pidfd_send_signal", LEADER, signal.SIGTERM, 0),
        ("pidfd_send_signal", LEADER, signal.SIGTERM, FLAG),
        ("pidfd_send_signal", LEADER, signal.SIGKILL, FLAG),
    ]
    assert not any(c[0] in ("kill", "killpg") for c in pidfd_rig.signals())
    assert attempt(pidfd_rig.store)["leftovers"] == 0
    assert ended(pidfd_rig)[:2] == ("canceled", "forced")


def test_an_exited_leader_ends_the_signals_at_once_and_members_are_leftovers(
    pidfd_rig: Rig,
) -> None:
    """No stepping through an exited leader (as before #35): no extra idle, members counted."""
    pidfd_rig.host.reactions[("pidfd", LEADER, signal.SIGTERM)] = zombify(pidfd_rig, LEADER)
    assert pidfd_rig.killer().run() == 0
    assert pidfd_rig.signals() == [("pidfd_send_signal", LEADER, signal.SIGTERM, 0)]
    assert pidfd_rig.now < 10  # returned without idling one kill_grace
    assert pidfd_rig.host.procs[MEMBER].pgid == LEADER  # never signaled
    assert attempt(pidfd_rig.store)["leftovers"] == 1
    assert ended(pidfd_rig)[:2] == ("canceled", "forced")


def test_a_reaped_leader_with_a_live_member_is_not_alive_and_gets_nothing(
    pidfd_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """pidfd_open of a reaped leader whose group still has a member is EINVAL on a real kernel:
    that reads as gone (no signal, no killpg, no error), not as an error."""
    pidfd_rig.host.reactions[("pidfd", LEADER, signal.SIGTERM)] = pidfd_rig.die(LEADER)
    assert pidfd_rig.killer().run() == 0
    assert pidfd_rig.signals() == [("pidfd_send_signal", LEADER, signal.SIGTERM, 0)]
    assert ("pidfd_open", LEADER) in pidfd_rig.host.calls
    assert "error" not in capsys.readouterr().err
    assert pidfd_rig.host.procs[MEMBER].pgid == LEADER
    assert attempt(pidfd_rig.store)["leftovers"] == 1


def test_a_reused_leader_pid_gets_no_group_signal_through_the_pidfd(pidfd_rig: Rig) -> None:
    def reuse() -> None:
        pidfd_rig.host.procs[LEADER] = proc(LEADER, start=START + 9, pgid=LEADER)

    pidfd_rig.host.reactions[("pidfd", LEADER, signal.SIGTERM)] = reuse
    assert pidfd_rig.killer().run() == 0
    assert pidfd_rig.signals() == [("pidfd_send_signal", LEADER, signal.SIGTERM, 0)]


def test_a_leader_that_left_its_group_is_not_signaled_through_the_flag(pidfd_rig: Rig) -> None:
    pidfd_rig.host.procs[LEADER] = proc(LEADER, start=START, pgid=STRANGER)
    set_running(pidfd_rig.store, pgid=LEADER)
    pidfd_rig.host.reactions[("killpg", LEADER, signal.SIGKILL)] = pidfd_rig.die(LEADER)
    pidfd_rig.killer().run()
    sent = pidfd_rig.signals()
    assert ("killpg", LEADER, signal.SIGKILL) in sent  # addressed by PID, after the re-check
    assert not any(c[0] == "pidfd_send_signal" and c[3] == FLAG for c in sent)  # no flag send


def test_an_old_kernel_ends_the_signals_once_the_leader_exited(old_kernel_rig: Rig) -> None:
    old_kernel_rig.host.reactions[("pidfd", LEADER, signal.SIGTERM)] = zombify(
        old_kernel_rig, LEADER
    )
    assert old_kernel_rig.killer().run() == 0
    sent = old_kernel_rig.signals()
    assert sent == [("pidfd_send_signal", LEADER, signal.SIGTERM, 0)]
    assert attempt(old_kernel_rig.store)["leftovers"] == 1
    assert ended(old_kernel_rig)[:2] == ("canceled", "forced")


def test_an_old_kernel_runs_the_whole_sequence_through_killpg(old_kernel_rig: Rig) -> None:
    old_kernel_rig.host.reactions[("killpg", LEADER, signal.SIGKILL)] = old_kernel_rig.die(
        LEADER, MEMBER
    )
    assert old_kernel_rig.killer().run() == 0
    assert [(c[0], c[2]) for c in old_kernel_rig.signals() if c[0] != "pidfd_send_signal"] == [
        ("killpg", signal.SIGTERM),
        ("killpg", signal.SIGKILL),
    ]
    assert old_kernel_rig.now >= 20
    assert attempt(old_kernel_rig.store)["leftovers"] == 0


def test_a_pid_reused_after_the_first_signal_receives_nothing_more(rig: Rig) -> None:
    def reuse() -> None:
        rig.host.procs[LEADER] = proc(LEADER, start=START + 9, pgid=LEADER)

    rig.host.reactions[("kill", LEADER, signal.SIGTERM)] = reuse
    assert rig.killer().run() == 0
    assert rig.signals() == [("kill", LEADER, signal.SIGTERM)]
    assert ended(rig)[:2] == ("canceled", "forced")


def test_sigkill_failing_to_end_a_stuck_leader_is_resent_until_it_goes(rig: Rig) -> None:
    gone_at = 45.0

    def maybe_die() -> None:
        if rig.now >= gone_at:
            rig.die(LEADER)()

    rig.on_sleep = maybe_die
    assert rig.killer().run() == 0
    kills = [c for c in rig.signals() if c[2] == signal.SIGKILL]
    assert len(kills) > 2  # resent about every second
    assert ended(rig)[:2] == ("canceled", "forced")


def test_without_a_pidfd_the_identity_is_rechecked_before_every_signal(rig: Rig) -> None:
    rig.host.reactions[("kill", LEADER, signal.SIGTERM)] = rig.die(LEADER)
    rig.killer().run()
    checks = [c for c in rig.host.calls if c == ("process_info", LEADER)]
    assert len(checks) >= 2  # at the entry and before the signal


def test_an_unreadable_leader_mid_sequence_is_never_signaled_unchecked() -> None:
    """`UNKNOWN` is not "gone": nothing is sent, and `_kill` gives up after a minute."""
    with Store.open(mode=OpenMode.CLI) as store:
        seed_claimed(store, ["sleep", "600"])
        set_running(store)
        host = Host(procs={ME: proc(ME), LEADER: proc(LEADER)}, pidfd_ok=False)
        r = Rig(store, host)

        def hide() -> None:
            host.info_errors[LEADER] = PermissionError("hidepid")

        r.on_sleep = hide
        assert r.killer().run() == 1
        assert [c for c in host.sent() if c[2] != signal.SIGTERM] == []
        assert job(store)["status"] == "running"


# -- stopping and finalizing -------------------------------------------------------------------


def test_sigterm_to_kill_itself_stops_it_without_finalizing_and_frees_the_lease(rig: Rig) -> None:
    rig.on_sleep = rig.stop.set
    assert rig.killer().run() == 1
    assert job(rig.store)["status"] == "running"
    assert lease(rig) is None  # another `_kill` can take over


def test_an_attempt_finalized_meanwhile_ends_this_kill_without_a_second_finalization(
    rig: Rig,
) -> None:
    def finish_elsewhere() -> None:
        with rig.store.write() as tx:
            tx.execute("UPDATE jobs SET status = 'failed', reason = 'lost'")

    rig.on_sleep = finish_elsewhere
    assert rig.killer().run() == 0  # handled by someone else: nothing to do, not a give-up
    assert job(rig.store)["reason"] == "lost"  # untouched by this `_kill`


def test_sigterm_while_the_final_commit_is_retried_stops_promptly(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    del rig.host.procs[LEADER]
    calls = []

    def busy(*a: Any, **k: Any) -> None:
        calls.append(1)
        rig.stop.set()
        raise RuntimeError("database is locked")

    monkeypatch.setattr("jobsctl.kill.finish_running", busy)
    assert rig.killer().run() == 1
    assert len(calls) == 1  # no backoff loop after the stop
    assert job(rig.store)["status"] == "running"
    assert lease(rig) is None


def test_the_leftovers_warning_makes_no_claim_about_signals(rig: Rig) -> None:
    rig.host.reactions[("kill", LEADER, signal.SIGTERM)] = rig.die(LEADER)
    rig.killer().run()
    message = log_records("leftovers")[0]["message"]
    assert message == "processes outlived the job's leader"


def test_a_forced_end_is_not_retried_even_with_a_retry_budget(rig: Rig) -> None:
    del rig.host.procs[LEADER]
    with rig.store.write() as tx:
        tx.execute("UPDATE jobs SET overrides = ?", [json.dumps({"max_retries": 3})])
        tx.execute("UPDATE attempts SET effective = ?", [json.dumps({"max_retries": 3})])
    assert rig.killer().run() == 0
    assert ended(rig)[:2] == ("canceled", "forced")
    assert job(rig.store)["not_before"] is None


def test_a_stale_claim_finalizes_nothing(rig: Rig) -> None:
    del rig.host.procs[LEADER]
    killer = rig.killer()

    def swap() -> None:
        with rig.store.write() as tx:
            tx.execute("UPDATE attempts SET claim_id = 'claim-other'")

    real_finish = killer._finish

    def finish(*a: Any, **k: Any) -> None:
        swap()
        real_finish(*a, **k)

    killer._finish = finish  # type: ignore[method-assign]
    assert killer.run() == 0
    assert job(rig.store)["status"] == "running"
    assert CLAIM != "claim-other"


def test_the_transition_is_logged_once(rig: Rig) -> None:
    del rig.host.procs[LEADER]
    rig.killer().run()
    moves = [r for r in log_records("transition") if r.get("to") == "canceled"]
    assert len(moves) == 1


# -- a result file the dead shim left ----------------------------------------------------------


def put_result(claim: str, *, code: int = 3) -> None:
    write_result(
        shims_dir(),
        ResultRecord(
            job_id=JOB,
            attempt=1,
            claim_id=claim,
            status="failed",
            reason="exit",
            hold_reason=None,
            exit=ExitRecord("exited", code=code),
            leftovers=2,
            tail=ResultTail("last words\n", 11, False),
            ended_at="2026-01-01T00:00:00.000Z",
        ),
    )


def test_a_result_file_gives_the_exit_record_leftovers_and_tail_and_nothing_is_signaled(
    rig: Rig,
) -> None:
    shims_dir().mkdir(parents=True, exist_ok=True)
    put_result(CLAIM)
    assert rig.killer().run() == 0
    assert rig.signals() == []
    assert ended(rig)[:2] == ("canceled", "forced")
    assert json.loads(attempt(rig.store)["exit"])["code"] == 3
    assert attempt(rig.store)["leftovers"] == 2
    assert list(shims_dir().glob("*.result")) == []
    tail = rig.store.query_one("SELECT tail FROM outputs WHERE job_id = ?", [JOB])
    assert tail is not None and bytes(tail["tail"]) == b"last words\n"


def test_a_result_file_of_another_claim_is_ignored(rig: Rig) -> None:
    shims_dir().mkdir(parents=True, exist_ok=True)
    put_result("claim-someone-else")
    del rig.host.procs[LEADER]
    assert rig.killer().run() == 0
    assert exit_kind(rig) == "unknown"


# -- the post-cancel event of the forced end ---------------------------------------------------


def test_the_forced_end_delivers_its_post_cancel_after_the_final_commit(
    rig: Rig, tmp_path: Any
) -> None:
    seen = tmp_path / "seen"
    db = rig.store.paths.root / "state.db"
    probe = (
        f"import sqlite3,sys,json; c=sqlite3.connect({str(db)!r}); "
        "s=c.execute('SELECT status, reason FROM jobs').fetchone(); "
        f"open({str(seen)!r},'a').write(json.loads(sys.stdin.read())['hook']+' '+' '.join(s)+'\\n')"
    )
    cfg = Config.model_validate(
        {
            "auto_run": False,
            "hooks": {"post-cancel": [{"name": "w", "cmd": f'python3 -c "{probe}"'}]},
        }
    )
    del rig.host.procs[LEADER]
    assert rig.killer(cfg).run() == 0
    assert ended(rig)[:2] == ("canceled", "forced")
    assert seen.read_text().split() == ["post-cancel", "canceled", "forced"]  # committed first
    rows = rig.store.query_all("SELECT delivered_at FROM hook_outbox")
    assert len(rows) == 1 and rows[0]["delivered_at"] is not None
    assert lease(rig) is None


def test_a_kill_that_gives_up_still_delivers_the_pending_events(rig: Rig, tmp_path: Any) -> None:
    trace = tmp_path / "trace"
    cfg = Config.model_validate(
        {
            "auto_run": False,
            "hooks": {"post-cancel": [{"name": "w", "cmd": f"echo ran >> {trace}"}]},
        }
    )
    payload = {
        "tool": "jobsctl", "hook": "post-cancel", "event_id": f"{JOB}:post-cancel:1",
        "object_type": "job", "object_id": JOB, "repo": None, "labels": [],
        "previous_status": "running", "job": {"queue": "q"}, "attempt": None,
    }  # fmt: skip
    with rig.store.write() as tx:
        tx.execute(
            "INSERT INTO hook_outbox (event_id, job_id, hook, payload, async, created_at) "
            "VALUES (?, ?, 'post-cancel', ?, 0, 'x')",
            [payload["event_id"], JOB, json.dumps(payload)],
        )
    rig.host.info_errors[LEADER] = PermissionError("hidepid")
    assert rig.killer(cfg).run() == 1  # gave up: nothing finalized
    untouched(rig)
    assert trace.read_text().split() == ["ran"]
    event = rig.store.query_one("SELECT delivered_at FROM hook_outbox")
    assert event is not None and event["delivered_at"] is not None
    assert lease(rig) is None


def test_an_unexpected_delivery_error_never_changes_the_kills_outcome(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.delivery import Deliverer

    def boom(self: Deliverer, *args: Any, **kw: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(Deliverer, "deliver_sync", boom)
    del rig.host.procs[LEADER]
    assert rig.killer().run() == 0
    assert ended(rig)[:2] == ("canceled", "forced")
    assert lease(rig) is None
