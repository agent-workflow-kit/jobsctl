"""The signal sequence state machine over a fake host: order, grace, fallbacks, never-sent."""

from __future__ import annotations

import errno
import signal
from dataclasses import dataclass, field

from jobsctl import gateway_proc
from jobsctl.proc_identity import Identity
from jobsctl.shim_signals import Phase, SignalSequence
from tests.proc_fakes import FakeHost, oserror, proc

LEADER = 500
IDENT = Identity("boot-1", LEADER, 1000)


@dataclass
class Rig:
    host: FakeHost
    now: float = 0.0
    exited: bool = False
    own: bool = True
    notes: list[str] = field(default_factory=list)

    def seq(
        self, grace: float = 10, *, pidfd_group: bool = False, allow_zombie: bool = True
    ) -> SignalSequence:
        return SignalSequence(
            self.host,
            IDENT,
            kill_grace=grace,
            leader_exited=lambda: self.exited,
            own_child=lambda: self.own,
            now=lambda: self.now,
            diag=self.notes.append,
            allow_zombie=allow_zombie,
            pidfd_group=pidfd_group,
        )

    def sent(self) -> list[tuple]:
        return self.host.sent()


def rig(**kw: object) -> Rig:
    host = FakeHost(procs={LEADER: proc(LEADER), 501: proc(501, pgid=LEADER)}, **kw)  # type: ignore[arg-type]
    return Rig(host)


def test_the_steps_run_in_order_with_the_grace_between_them() -> None:
    r = rig(pidfd_ok=False)
    seq = r.seq(grace=10)
    seq.start("timeout")
    assert r.sent() == [("kill", LEADER, signal.SIGTERM)]
    r.now = 9
    seq.advance()
    assert len(r.sent()) == 1  # still within the grace
    r.now = 10
    seq.advance()
    assert r.sent()[-1] == ("killpg", LEADER, signal.SIGTERM)
    r.now = 19
    seq.advance()
    assert len(r.sent()) == 2
    r.now = 20
    seq.advance()
    assert ("killpg", LEADER, signal.SIGKILL) in r.sent()
    assert ("kill", LEADER, signal.SIGKILL) in r.sent()  # the PID too, in case it left the group
    assert seq.phase is Phase.KILL


def test_a_leader_that_exits_with_an_empty_group_ends_the_sequence_at_once() -> None:
    r = rig(pidfd_ok=False)
    del r.host.procs[501]
    seq = r.seq()
    seq.start("terminated")
    r.exited = True
    seq.advance()
    assert seq.done and len(r.sent()) == 1


def test_a_leader_that_exited_with_members_left_gets_the_group_sigterm_at_once() -> None:
    r = rig(pidfd_ok=False)
    seq = r.seq()
    seq.start("timeout")
    r.exited = True
    seq.advance()
    assert r.sent()[-1] == ("killpg", LEADER, signal.SIGTERM) and not seq.done
    del r.host.procs[501]
    seq.advance()
    assert seq.done


def test_a_zombie_member_does_not_keep_the_sequence_going() -> None:
    r = rig(pidfd_ok=False)
    r.host.procs[501] = proc(501, pgid=LEADER, zombie=True)
    seq = r.seq()
    seq.start("timeout")
    r.exited = True
    seq.advance()
    assert seq.done


def test_start_is_idempotent() -> None:
    r = rig(pidfd_ok=False)
    seq = r.seq()
    seq.start("timeout")
    seq.start("terminated")
    assert seq.cause == "timeout" and len(r.sent()) == 1


def test_an_inconclusive_identity_falls_back_only_for_our_own_unreaped_child() -> None:
    r = rig(pidfd_ok=False, boot=None)  # no boot id: the identity cannot be established
    seq = r.seq()
    seq.start("timeout")
    # signal_process sent nothing (UNKNOWN); the fallback killed our own child's PID
    assert r.sent() == [("kill", LEADER, signal.SIGTERM)]
    r2 = rig(pidfd_ok=False, boot=None)
    r2.own = False
    seq2 = r2.seq()
    seq2.start("timeout")
    assert r2.sent() == []  # not our child: nothing is sent without a verified identity


def test_a_pidfd_error_is_retried_by_the_fallback_not_taken_for_a_gone_process() -> None:
    r = rig(pidfd_open_error=oserror(errno.EMFILE))
    seq = r.seq()
    seq.start("timeout")
    assert r.sent() == [("kill", LEADER, signal.SIGTERM)]


def test_nothing_is_sent_to_a_leader_that_is_not_the_recorded_process() -> None:
    r = rig(pidfd_ok=False)
    r.host.procs[LEADER] = proc(LEADER, start=999)  # the PID now belongs to someone else
    seq = r.seq()
    seq.start("timeout")
    r.now = 100
    seq.advance()
    seq.advance()
    assert r.sent() == []


def test_sigkill_is_repeated_while_members_remain_and_given_up_after_the_grace() -> None:
    r = rig(pidfd_ok=False)
    seq = r.seq(grace=2)
    seq.start("timeout")
    r.now = 2
    seq.advance()
    r.now = 4
    seq.advance()
    assert seq.phase is Phase.KILL
    r.exited = True
    r.now = 5.5
    seq.advance()
    kills = [c for c in r.sent() if c[2] == signal.SIGKILL and c[0] == "killpg"]
    assert len(kills) >= 2  # resent while the member remains
    r.now = 7
    seq.advance()
    assert seq.done and any("not waiting longer" in n for n in r.notes)


def test_a_leader_that_survives_sigkill_is_resent_to_with_bounded_records() -> None:
    r = rig(pidfd_ok=False)
    seq = r.seq(grace=2)
    seq.start("timeout")
    r.now = 2
    seq.advance()
    r.now = 4
    seq.advance()
    for second in range(5, 605):  # ten minutes of a leader stuck in the kernel
        r.now = second
        seq.advance()
    assert seq.phase is Phase.KILL  # it waits: the give-up applies only once the leader exited
    assert len(seq.sent) <= 4  # term:pid, term:group, kill:group, kill:pid; no growth
    assert len(r.notes) == 1 and "survives SIGKILL" in r.notes[0]
    resent = [c for c in r.sent() if c[0] == "killpg" and c[2] == signal.SIGKILL]
    assert len(resent) > 100  # but SIGKILL does keep being sent


def test_a_hiding_proc_alone_does_not_make_an_empty_group_unprovable() -> None:
    r = rig(pidfd_ok=False, hidden=True)
    del r.host.procs[501]
    seq = r.seq()
    seq.start("timeout")
    r.exited = True
    seq.advance()
    assert seq.done and len(r.sent()) == 1  # no wait for 2 x kill_grace


def test_a_really_unreadable_entry_still_keeps_the_sequence_waiting() -> None:
    r = rig(pidfd_ok=False, hidden=True)
    del r.host.procs[501]
    r.host.info_errors[777] = PermissionError("unreadable entry")
    seq = r.seq()
    seq.start("timeout")
    r.exited = True
    seq.advance()
    assert not seq.done  # the group may have a member that could not be read


def test_members_that_sigkill_cannot_remove_are_abandoned_after_one_more_grace() -> None:
    r = rig(pidfd_ok=False)  # the leader exited; member 501 stays (the fake kills nothing)
    r.exited = True
    seq = r.seq(grace=2)
    seq.start("timeout")
    seq.advance()  # group SIGTERM at once
    r.now = 2
    seq.advance()  # group SIGKILL
    r.now = 3
    seq.advance()  # resent after one second
    assert not seq.done
    r.now = 4  # one more grace after the first SIGKILL
    seq.advance()
    assert seq.done and any("not waiting longer" in n for n in r.notes)
    kills = [c for c in r.sent() if c[0] == "killpg" and c[2] == signal.SIGKILL]
    assert len(kills) == 2


def test_a_child_reaped_elsewhere_is_not_our_unreaped_child() -> None:
    import os
    import subprocess

    from jobsctl.shim import _own_unreaped_child

    live = subprocess.Popen(["sleep", "600"])
    try:
        assert _own_unreaped_child(live)
    finally:
        live.kill()  # the exact child this test started
        live.wait()
    gone = subprocess.Popen(["true"])
    os.waitpid(gone.pid, 0)  # reaped behind Popen's back: returncode stays None
    assert gone.returncode is None
    assert not _own_unreaped_child(gone)


def test_no_fallback_signal_is_sent_for_a_child_reaped_elsewhere() -> None:
    r = rig(pidfd_ok=False, boot=None)  # identity inconclusive
    r.own = False  # what `_own_unreaped_child` answers for a child reaped elsewhere
    seq = r.seq()
    seq.start("timeout")
    assert r.sent() == []


# --- PIDFD_SIGNAL_PROCESS_GROUP (#35), against a faked kernel --------------------------------

FLAG = gateway_proc.PIDFD_SIGNAL_PROCESS_GROUP


def test_the_group_steps_use_the_pidfd_group_flag_when_enabled() -> None:
    r = rig()
    seq = r.seq(pidfd_group=True)
    seq.start("timeout")
    r.now = 10
    seq.advance()
    r.now = 20
    seq.advance()
    assert ("pidfd_send_signal", LEADER, signal.SIGTERM, FLAG) in r.sent()
    assert ("pidfd_send_signal", LEADER, signal.SIGKILL, FLAG) in r.sent()
    assert not any(c[0] == "killpg" for c in r.sent())
    assert seq.sent == ["term:pid", "term:group", "kill:group", "kill:pid"]


def test_a_kernel_without_the_flag_gives_the_same_sequence_through_killpg() -> None:
    plain = rig(pidfd_ok=False)
    old = rig(pidfd_send_error={FLAG: oserror(errno.EINVAL)})
    results = []
    for r, group in ((plain, False), (old, True)):
        seq = r.seq(pidfd_group=group)
        seq.start("timeout")
        r.now = 10
        seq.advance()
        r.now = 20
        seq.advance()
        results.append((seq.sent, seq.phase, [c[0] for c in r.sent() if c[0] == "killpg"]))
    assert results[0] == results[1]


def test_the_group_signal_is_not_sent_when_the_leader_pid_was_reused() -> None:
    r = rig()
    seq = r.seq(pidfd_group=True)
    seq.start("timeout")
    r.host.procs[LEADER] = proc(LEADER, start=5555)
    r.now = 10
    seq.advance()
    assert [c for c in r.sent() if c[2] == signal.SIGTERM and len(c) == 4 and c[3]] == []
    assert not any(c[0] == "killpg" for c in r.sent())


def test_an_own_child_retries_a_refused_group_flag_signal_with_killpg() -> None:
    """The shim holds the leader unreaped, so its group id is reserved: a pidfd that reports the
    leader gone (a kernel that does not reach a zombie leader's group) must not lose the signal."""
    r = rig(pidfd_send_error={FLAG: oserror(errno.ESRCH)})
    seq = r.seq(pidfd_group=True)
    seq.start("timeout")
    r.exited = True
    seq.advance()
    assert r.sent()[-1] == ("killpg", LEADER, signal.SIGTERM)
    assert "term:group" in seq.sent


def test_a_leader_that_is_not_our_child_is_never_retried_with_killpg() -> None:
    r = rig(pidfd_send_error={FLAG: oserror(errno.ESRCH)})
    r.own = False
    seq = r.seq(pidfd_group=True, allow_zombie=False)
    seq.start("forced")
    r.exited = True
    seq.advance()
    assert not any(c[0] == "killpg" for c in r.sent())
    assert seq.group_unreachable


def test_a_zombie_leader_not_held_by_us_is_signaled_only_through_the_pidfd() -> None:
    r = rig()
    r.own = False
    r.host.procs[LEADER] = proc(LEADER, start=1000, zombie=True)
    seq = r.seq(pidfd_group=True, allow_zombie=False)
    seq.start("forced")  # SIGTERM to the PID: a zombie reads as gone, nothing sent
    r.exited = True
    seq.advance()
    assert r.sent() == [("pidfd_send_signal", LEADER, signal.SIGTERM, FLAG)]
    assert not seq.group_unreachable


def test_a_zombie_leader_not_held_by_us_gets_no_killpg_on_an_old_kernel() -> None:
    r = rig(pidfd_send_error={FLAG: oserror(errno.EINVAL)})
    r.own = False
    r.host.procs[LEADER] = proc(LEADER, start=1000, zombie=True)
    seq = r.seq(pidfd_group=True, allow_zombie=False)
    seq.start("forced")
    r.exited = True
    seq.advance()
    assert not any(c[0] in ("kill", "killpg") for c in r.sent())
    assert seq.group_unreachable


def test_the_group_is_reachable_until_a_group_signal_is_refused() -> None:
    r = rig()
    seq = r.seq(pidfd_group=True)
    assert not seq.group_unreachable
    seq.start("timeout")
    r.now = 10
    seq.advance()
    assert not seq.group_unreachable
