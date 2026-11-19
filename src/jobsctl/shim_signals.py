"""The job signal sequence (docs/interface.md, Signal sequence; design.md D10).

A small state machine the shim steps from its pump loop, so the job's output pipe keeps being
read (a job blocked on a full pipe could not obey a signal) and the lease keeps being renewed
while the sequence waits `kill_grace` between steps:

1. SIGTERM to the job's exact PID (identity verified: ``proc_signal.signal_process``);
2. when the grace passed and the PID is still alive or processes remain in the job's group, or at
   once when the leader exited and group members remain: SIGTERM to the job's group;
3. when the grace passed again: SIGKILL to the group, and to the PID too if it left the group.

The leader is the shim's own child and is held unreaped (``WNOWAIT``) for the whole sequence, so
its PID, and the group id equal to it, cannot be reused: the group is addressed by that PID and is
signaled only while the leader's recorded identity holds. Nothing here reaps the leader, and no
signal is sent once the shim reaped it (the shim does that after the sequence is done). A process
that left the group is never signaled, except the leader itself in step 3.

``UNKNOWN`` (identity unreadable) and ``ERROR`` (e.g. ``EMFILE`` from ``pidfd_open``) are not
"gone": nothing was signaled. Because the leader is the shim's unreaped child, the same PID/group
signal sent without the identity check is safe by construction, so the sequence falls back to it
rather than stall (``own_child``). ``NOT_ALIVE`` and ``DENIED`` never fall back.
"""

from __future__ import annotations

import enum
import signal
from collections.abc import Callable

from jobsctl.proc_group import probe_group
from jobsctl.proc_host import Host
from jobsctl.proc_identity import Identity
from jobsctl.proc_signal import Result, SignalOutcome, signal_group, signal_process

RESEND_SECONDS = 1.0  # SIGKILL to a group that still has members is repeated at this pace


class Phase(enum.Enum):
    IDLE = "idle"
    TERM_PID = "term_pid"  # SIGTERM sent to the PID; waiting
    TERM_GROUP = "term_group"  # SIGTERM sent to the group; waiting
    KILL = "kill"  # SIGKILL sent; waiting for the leader and the group to go
    DONE = "done"


class SignalSequence:
    def __init__(
        self,
        host: Host,
        leader: Identity,
        *,
        kill_grace: float,
        leader_exited: Callable[[], bool],
        own_child: Callable[[], bool],
        now: Callable[[], float],
        diag: Callable[[str], None],
        allow_zombie: bool = True,
        pidfd_group: bool = False,
    ) -> None:
        self._host = host
        self._leader = leader
        self._grace = float(kill_grace)
        self._leader_exited = leader_exited
        self._own_child = own_child
        self._now = now
        self._diag = diag
        # True only for a leader the caller holds unreaped (the shim): its zombie still reserves
        # the group id. A leader the caller does not hold (`_kill`) counts as gone once it is a
        # zombie, and then nothing is signaled.
        self._allow_zombie = allow_zombie
        # Group signals try ``PIDFD_SIGNAL_PROCESS_GROUP`` first (Linux 6.9+) and fall back to the
        # same re-checked ``killpg`` on an older kernel. The pidfd path also reaches the group of
        # a leader that exited but is not reaped yet, even when ``allow_zombie`` is False (the
        # pidfd cannot be redirected); the fallback honors ``allow_zombie`` as before.
        self._pidfd_group = pidfd_group
        # True once the latest group signal found nothing to signal (leader gone or replaced).
        self.group_unreachable = False
        # How the latest group signal went out: True through the pidfd flag, False otherwise,
        # None while no group signal was attempted.
        self.group_via_pidfd: bool | None = None
        self.phase = Phase.IDLE
        self.cause: str | None = None
        self.sent: list[str] = []  # for logs and tests: "term:pid", "term:group", "kill:group", ...
        self._deadline = 0.0
        self._last_kill = 0.0
        self._seen: set[tuple[str, str]] = set()  # (what, result) pairs already noted
        self._overdue_noted = False

    @property
    def started(self) -> bool:
        return self.phase is not Phase.IDLE

    @property
    def done(self) -> bool:
        return self.phase is Phase.DONE

    # -- sending ----------------------------------------------------------------------------

    def _fallback_ok(self, outcome: SignalOutcome) -> bool:
        return outcome.result in (Result.UNKNOWN, Result.ERROR) and self._own_child()

    def _note(self, what: str, outcome: SignalOutcome, *, repeat: bool = False) -> None:
        key = (what, outcome.result.value)
        if repeat and key in self._seen:  # the SIGKILL resend keeps the first of each pair only
            return
        self._seen.add(key)
        if outcome.result is Result.SENT:
            self.sent.append(what)
        elif outcome.result is not Result.NOT_ALIVE:
            self._diag(f"{what}: {outcome.result.value} ({outcome.detail or outcome.method})")

    def _signal_pid(self, sig: int, what: str, *, repeat: bool = False) -> None:
        out = signal_process(self._host, self._leader, sig)
        if self._fallback_ok(out):
            self._diag(
                f"{what}: identity not confirmed ({out.detail}); the PID is our unreaped child"
            )
            try:
                self._host.kill(self._leader.pid, sig)
                out = SignalOutcome(Result.SENT, "kill", "fallback")
            except OSError as exc:
                out = SignalOutcome(Result.ERROR, "kill", exc.strerror or "")
        self._note(what, out, repeat=repeat)

    def _signal_group(self, sig: int, what: str, *, repeat: bool = False) -> None:
        out = signal_group(
            self._host,
            self._leader,
            sig,
            allow_zombie=True if self._pidfd_group else self._allow_zombie,
            use_pidfd=self._pidfd_group,
            fallback_allow_zombie=self._allow_zombie,
        )
        if out.method == "pidfd-group" and out.result is Result.NOT_ALIVE and self._own_child():
            # The leader is our own unreaped child, so it cannot be gone: the pidfd path could not
            # reach the group (a kernel that does not deliver to a zombie leader's group). Its
            # group id is reserved, so the plain group signal is safe.
            self._diag(f"{what}: the pidfd group signal found nothing; using killpg")
            out = signal_group(
                self._host, self._leader, sig, allow_zombie=self._allow_zombie, use_pidfd=False
            )
        if self._fallback_ok(out):
            self._diag(f"{what}: identity not confirmed ({out.detail}); the group id is reserved")
            try:
                self._host.killpg(self._leader.pid, sig)
                out = SignalOutcome(Result.SENT, "killpg", "fallback")
            except ProcessLookupError:
                out = SignalOutcome(Result.NOT_ALIVE, "killpg", "gone")
            except OSError as exc:
                out = SignalOutcome(Result.ERROR, "killpg", exc.strerror or "")
        self.group_unreachable = out.result is Result.NOT_ALIVE
        self.group_via_pidfd = out.method == "pidfd-group" and out.result is Result.SENT
        self._note(what, out, repeat=repeat)

    def _group_empty(self) -> bool:
        probe = probe_group(self._host, self._leader.pid, exclude=(self._leader.pid,))
        # A `/proc` that hides other users' processes always bumps `unreadable` once; with no
        # entry that was seen failing to read, the members shown are all there are to wait for.
        return probe.count == 0 and probe.complete_but_hidden

    # -- the steps --------------------------------------------------------------------------

    def start(self, cause: str) -> None:
        """Step 1. Idempotent: a second cause (a duplicated SIGTERM, a force request after a
        timeout) never restarts the sequence."""
        if self.started:
            return
        self.cause = cause
        self._signal_pid(signal.SIGTERM, "term:pid")
        self.phase = Phase.TERM_PID
        self._deadline = self._now() + self._grace

    def _to_group_term(self) -> None:
        self._signal_group(signal.SIGTERM, "term:group")
        self.phase = Phase.TERM_GROUP
        self._deadline = self._now() + self._grace

    def _to_kill(self) -> None:
        self._signal_group(signal.SIGKILL, "kill:group")
        if not self._leader_exited():
            self._signal_pid(signal.SIGKILL, "kill:pid")  # a no-op repeat unless it left the group
        self.phase = Phase.KILL
        self._last_kill = self._now()
        self._deadline = self._now() + self._grace

    def advance(self) -> None:
        """One look; never blocks. Call it from the pump loop while the sequence is not done."""
        if self.phase in (Phase.IDLE, Phase.DONE):
            return
        now = self._now()
        exited = self._leader_exited()
        if self.phase is Phase.TERM_PID:
            if exited:
                if self._group_empty():
                    self.phase = Phase.DONE
                else:
                    self._to_group_term()
            elif now >= self._deadline:
                self._to_group_term()
        elif self.phase is Phase.TERM_GROUP:
            if exited and self._group_empty():
                self.phase = Phase.DONE
            elif now >= self._deadline:
                self._to_kill()
        elif self.phase is Phase.KILL:
            if now >= self._deadline and not exited and not self._overdue_noted:
                # A leader that survives SIGKILL is stuck in the kernel. The shim keeps resending
                # and waits: the give-up applies only once the leader has exited.
                self._overdue_noted = True
                self._diag("the job leader survives SIGKILL; resending every second and waiting")
            if exited and self._group_empty():
                self.phase = Phase.DONE
            elif now >= self._deadline and exited:
                # SIGKILL cannot be refused; what still shows is a process stuck in the kernel.
                # Give up waiting: the leftovers probe records it.
                self._diag("processes remain in the group after SIGKILL; not waiting longer")
                self.phase = Phase.DONE
            elif now - self._last_kill >= RESEND_SECONDS:
                self._last_kill = now
                self._signal_group(signal.SIGKILL, "kill:group", repeat=True)
                if not exited:
                    self._signal_pid(signal.SIGKILL, "kill:pid", repeat=True)
