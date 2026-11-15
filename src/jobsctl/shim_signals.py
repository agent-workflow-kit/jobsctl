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
    ) -> None:
        self._host = host
        self._leader = leader
        self._grace = float(kill_grace)
        self._leader_exited = leader_exited
        self._own_child = own_child
        self._now = now
        self._diag = diag
        self.phase = Phase.IDLE
        self.cause: str | None = None
        self.sent: list[str] = []  # for logs and tests: "term:pid", "term:group", "kill:group", ...
        self._deadline = 0.0
        self._last_kill = 0.0

    @property
    def started(self) -> bool:
        return self.phase is not Phase.IDLE

    @property
    def done(self) -> bool:
        return self.phase is Phase.DONE

    # -- sending ----------------------------------------------------------------------------

    def _fallback_ok(self, outcome: SignalOutcome) -> bool:
        return outcome.result in (Result.UNKNOWN, Result.ERROR) and self._own_child()

    def _note(self, what: str, outcome: SignalOutcome) -> None:
        if outcome.result is Result.SENT:
            self.sent.append(what)
        elif outcome.result is not Result.NOT_ALIVE:
            self._diag(f"{what}: {outcome.result.value} ({outcome.detail or outcome.method})")

    def _signal_pid(self, sig: int, what: str) -> None:
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
        self._note(what, out)

    def _signal_group(self, sig: int, what: str) -> None:
        out = signal_group(self._host, self._leader, sig, allow_zombie=True)
        if self._fallback_ok(out):
            self._diag(f"{what}: identity not confirmed ({out.detail}); the group id is reserved")
            try:
                self._host.killpg(self._leader.pid, sig)
                out = SignalOutcome(Result.SENT, "killpg", "fallback")
            except ProcessLookupError:
                out = SignalOutcome(Result.NOT_ALIVE, "killpg", "gone")
            except OSError as exc:
                out = SignalOutcome(Result.ERROR, "killpg", exc.strerror or "")
        self._note(what, out)

    def _group_empty(self) -> bool:
        probe = probe_group(self._host, self._leader.pid, exclude=(self._leader.pid,))
        return probe.count == 0 and probe.complete

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
            if exited and self._group_empty():
                self.phase = Phase.DONE
            elif now >= self._deadline and exited:
                # SIGKILL cannot be refused; what still shows is a process stuck in the kernel.
                # Give up waiting: the leftovers probe records it.
                self._diag("processes remain in the group after SIGKILL; not waiting longer")
                self.phase = Phase.DONE
            elif now - self._last_kill >= RESEND_SECONDS:
                self._last_kill = now
                self._signal_group(signal.SIGKILL, "kill:group")
                if not exited:
                    self._signal_pid(signal.SIGKILL, "kill:pid")
