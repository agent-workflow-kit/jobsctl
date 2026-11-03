"""Run one hook process (interface.md, Hooks and Hook processes).

``run_hook`` starts ``/bin/sh -c <cmd>`` in its own process group, feeds the payload on stdin,
captures stdout and stderr in memory (64 KiB each), waits for the leader with
``waitid(WEXITED|WNOWAIT)`` and keeps the leader **unreaped** until every group signal and the
group probe are done, so the process group id stays reserved. On timeout, or when ``stop``
asks, it sends SIGTERM to the hook's group, waits ``term_grace`` (5 seconds), then SIGKILL. The
leader is then reaped by its exact PID, and only the leader.

Signals go through ``proc_signal.signal_group`` with the identity recorded at spawn, and only
to the group this call created. Nothing is ever signaled by name or pattern.

The caller (``pre-start`` in the shim, the delivery of post hooks) selects hooks, builds the
payload and the environment, and logs the result; this module knows nothing of jobs or state.
TODO(#24): outbox delivery and ``async`` hooks call ``run_hook`` per event.
TODO(#25): the shim's ``pre-start`` loop applies veto and escalation from ``HookResult``.
TODO(#28): ``wait_exited_keep`` (``waitid`` with ``WNOWAIT``) is ENOSYS on macOS: ``run_hook`` is
Linux-only until the macOS port.
"""

from __future__ import annotations

import os
import selectors
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gateway
from .hook_output import (
    LOG_STDERR_LIMIT,
    MESSAGE_LIMIT,
    Capture,
    excerpt,
    parse_decision_ids,
)
from .proc_group import probe_group
from .proc_host import BootIdUnavailable, Host, default_host
from .proc_identity import Identity, ProcessGone, identify
from .proc_linux import reap_held_leader, wait_exited_keep
from .proc_signal import Result, signal_group

__all__ = [
    "EXIT_ESCALATE",
    "HookResult",
    "HookTuning",
    "HookWarning",
    "run_hook",
]

EXIT_ESCALATE = 13
PRE_START = "pre-start"
_READ_CHUNK = 65536
_TICK = 0.02


@dataclass(frozen=True)
class HookTuning:
    """Time constants; the defaults are the documented ones, tests shorten them."""

    term_grace: float = 5.0  # SIGTERM to SIGKILL
    kill_wait: float = 2.0  # after SIGKILL, wait this long for the leader and the group
    drain: float = 2.0  # after the leader exited, read the pipes this long at most
    stop_interval: float = 0.25  # ``stop`` is polled at least this often


@dataclass(frozen=True)
class HookWarning:
    code: str  # invalid_decision_id
    detail: str  # sanitized, masked, at most 64 bytes


@dataclass(frozen=True)
class HookResult:
    hook: str
    hook_id: str
    outcome: str  # allow, escalate, veto, failed or timeout
    exit: int | None  # the exit status; None on timeout, a signal or no start
    signal: int | None  # the signal that ended the leader, if any
    decision_ids: tuple[str, ...]
    warnings: tuple[HookWarning, ...]
    stderr: str | None  # masked, sanitized, at most 2 KiB; None for ``allow``
    message: str | None  # a ``pre-start`` hook that did not allow: at most 4096 bytes
    stdout_truncated: bool
    stderr_truncated: bool
    leftovers: int | None  # group members left behind; None when the probe was incomplete
    stopped: str | None  # the reason ``stop`` gave: the hook was stopped, its outcome is moot
    start_error: str | None  # the hook could not be started (the errno text)
    signals_sent: tuple[str, ...]  # group signals sent, in order
    unkillable: bool  # the leader outlived SIGKILL: it was left unreaped
    duration: float

    @property
    def vetoes(self) -> bool:
        """A `pre-start` hook that vetoes: any non-zero exit other than 13, a timeout, or a hook
        that could not run. A stopped hook decides nothing.

        TODO(#25): the `pre-start` loop records a vetoing result in `hook_messages` with outcome
        `veto` (also for a timeout, a kill by signal and a hook that could not start), while its
        `hook_ran` outcome stays veto, timeout or failed. A result with ``unkillable`` set (the
        leader outlived SIGKILL and was left unreaped) must be treated as an internal error by
        the callers (#24, #25), not as a veto."""
        return (
            self.hook == PRE_START
            and self.stopped is None
            and self.outcome in ("veto", "timeout", "failed")
        )

    @property
    def escalates(self) -> bool:
        return self.hook == PRE_START and self.stopped is None and self.outcome == "escalate"

    def log_fields(self, job_id: str) -> dict[str, Any]:
        """The fields of the `hook_ran` log record (`schemas/log.json`), without seq, ts, level.

        TODO(#6): the `hook_ran` writer consumes these fields and ``warnings`` (the
        `invalid_decision_id` log warnings)."""
        fields: dict[str, Any] = {
            "event": "hook_ran",
            "job_id": job_id,
            "hook": self.hook,
            "hook_id": self.hook_id,
            "outcome": self.outcome,
            "exit": self.exit,
            "decision_ids": list(self.decision_ids),
        }
        if self.outcome != "allow":
            fields["stderr"] = self.stderr or ""
        return fields


def classify(hook: str, exit_code: int | None, *, timed_out: bool) -> str:
    """Design guide section 8: exit 0 allows, exit 13 escalates (`pre-start` only), a timeout is
    `timeout`; any other non-zero exit vetoes a `pre-start` hook and merely fails a post hook."""
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return "allow"
    if hook == PRE_START:
        return "escalate" if exit_code == EXIT_ESCALATE else "veto"
    return "failed"


def _identity(host: Host, pid: int) -> Identity | None:
    try:
        return identify(host, pid)
    except (ProcessGone, BootIdUnavailable, OSError, ValueError):
        return None


@dataclass
class _Run:
    host: Host
    tuning: HookTuning
    proc: Any
    payload: memoryview
    sel: selectors.BaseSelector
    out: Capture = field(default_factory=Capture)
    err: Capture = field(default_factory=Capture)
    ident: Identity | None = None
    signals: list[str] = field(default_factory=list)
    leader_done: bool = False
    written: int = 0

    def __post_init__(self) -> None:
        self.pid = int(self.proc.pid)  # nothing here can fail: ``emergency`` works from here on

    def setup(self) -> None:
        """Register the pipes. May fail or be interrupted; ``emergency`` copes with a half-done
        setup."""
        for stream, capture in ((self.proc.stdout, self.out), (self.proc.stderr, self.err)):
            os.set_blocking(stream.fileno(), False)
            self.sel.register(stream.fileno(), selectors.EVENT_READ, capture)
        os.set_blocking(self.proc.stdin.fileno(), False)
        if len(self.payload):
            self.sel.register(self.proc.stdin.fileno(), selectors.EVENT_WRITE, None)
        else:
            self._close_stdin()

    # --- pipes -------------------------------------------------------------------------------

    def _close_stdin(self) -> None:
        stdin = self.proc.stdin
        if stdin is None or stdin.closed:
            return
        try:
            self.sel.unregister(stdin.fileno())
        except (KeyError, ValueError):
            pass
        try:
            stdin.close()
        except OSError:
            pass

    def _pump(self, timeout: float) -> bool:
        """Move bytes for at most ``timeout`` seconds (one select round); whether any I/O was
        ready."""
        timeout = max(0.0, min(timeout, _TICK))
        if not self.sel.get_map():
            time.sleep(timeout)
            return False
        ready = self.sel.select(timeout)
        for key, _ in ready:
            fd = key.fd
            if key.events & selectors.EVENT_WRITE:
                self._write_stdin(fd)
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if chunk:
                key.data.add(chunk)
            else:
                self.sel.unregister(fd)
        return bool(ready)

    def _drain_ready(self) -> None:
        """Read what the pipes already hold, never waiting, for at most ``tuning.drain``."""
        end = time.monotonic() + self.tuning.drain
        while time.monotonic() < end and self._pump(0.0):
            pass

    def _write_stdin(self, fd: int) -> None:
        try:
            n = os.write(fd, self.payload[self.written : self.written + _READ_CHUNK])
        except BlockingIOError:
            return
        except OSError:  # EPIPE: the hook does not read its stdin; that is its right
            self._close_stdin()
            return
        self.written += n
        if self.written >= len(self.payload):
            self._close_stdin()

    @property
    def streams_open(self) -> bool:
        return any(key.data is not None for key in self.sel.get_map().values())

    # --- leader and group ----------------------------------------------------------------------

    def leader_exited(self) -> bool:
        if not self.leader_done:
            self.leader_done = wait_exited_keep(self.pid, nohang=True) is not None
        return self.leader_done

    def group_clear(self) -> bool:
        probe = probe_group(self.host, self.pid, exclude=[self.pid])
        return probe.complete and probe.count == 0

    def send(self, sig: int) -> None:
        """``sig`` to the hook's process group, only while the leader (held unreaped) is ours.

        ``signals`` records only a signal that was sent. When the identity cannot be verified
        (``UNKNOWN``) the leader is still our own unreaped child, so its PID reserves the group id
        and ``killpg(pid)`` is safe, exactly as when no identity could be read at all.
        """
        if self.proc.returncode is not None:
            return  # reaped: the group id is no longer reserved, never signal
        if self.ident is not None:
            outcome = signal_group(self.host, self.ident, sig, allow_zombie=True)
            if outcome.sent:
                self.signals.append(signal.Signals(sig).name)
                return
            if outcome.result is not Result.UNKNOWN:
                return  # not alive, denied or an error: nothing was signaled
        try:
            self.host.killpg(self.pid, sig)
        except OSError:
            return
        self.signals.append(signal.Signals(sig).name)

    def terminate(self) -> bool:
        """SIGTERM, ``term_grace``, SIGKILL; returns False if the leader outlived SIGKILL."""
        self.send(signal.SIGTERM)
        end = time.monotonic() + self.tuning.term_grace
        while time.monotonic() < end:
            if self.leader_exited() and self.group_clear():
                self._drain_ready()
                return True
            self._pump(end - time.monotonic())
        if not (self.leader_exited() and self.group_clear()):
            self.send(signal.SIGKILL)
        end = time.monotonic() + self.tuning.kill_wait
        while time.monotonic() < end:
            if self.leader_exited() and self.group_clear():
                break
            self._pump(end - time.monotonic())
        self._drain_ready()  # the last bytes written before the signal
        return self.leader_exited()

    def close(self) -> None:
        self.sel.close()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def emergency(self) -> None:
        """An unexpected exception, also on a half-built run: SIGKILL the group (the leader is
        our own unreaped child, ``returncode`` still ``None``), reap it if it died, close all."""
        try:
            self.send(signal.SIGKILL)
            end = time.monotonic() + self.tuning.kill_wait
            while time.monotonic() < end and not self.leader_exited():
                time.sleep(_TICK)
            if self.leader_done:
                reap_held_leader(self.proc)
        finally:
            self.close()


def _noop_mask(text: str) -> str:
    return text


def _stopped_before_start(hook: str, hook_id: str, reason: str) -> HookResult:
    return HookResult(
        hook, hook_id, "failed", None, None, (), (), None, None, False, False, None, reason,
        None, (), False, 0.0,
    )  # fmt: skip


def run_hook(
    *,
    hook: str,
    hook_id: str,
    cmd: str,
    payload: bytes,
    timeout: float,
    env: Mapping[str, str],
    cwd: str | Path,
    mask: Callable[[str], str] = _noop_mask,  # TODO(#8): the masked-values set
    stop: Callable[[], str | None] | None = None,
    host: Host | None = None,
    tuning: HookTuning | None = None,
) -> HookResult:
    """Run ``cmd`` as one hook and report its outcome.

    ``env`` is the complete environment (``hook_env.build_hook_env``); ``cwd`` is jobsctl's state
    directory; ``payload`` is the encoded JSON for stdin. ``stop`` is polled at least every
    ``tuning.stop_interval`` and returns a reason to stop the hook (a canceled job, a signal
    received by the firing command): the group is then stopped as on timeout and the result has
    ``stopped`` set. ``mask`` is the step-4 masking function of every text that leaves here.
    """
    tune = tuning or HookTuning()
    began = time.monotonic()
    if stop is not None and (reason := stop()):
        return _stopped_before_start(hook, hook_id, reason)
    use_host = host or default_host()
    view = memoryview(payload)
    sel = selectors.DefaultSelector()
    try:
        proc = gateway.spawn(["/bin/sh", "-c", cmd], cwd=cwd, env=env, new_process_group=True)
    except (OSError, ValueError) as exc:
        sel.close()
        text = os.strerror(exc.errno) if isinstance(exc, OSError) and exc.errno else "invalid"
        message = f"hook could not be started ({text})" if hook == PRE_START else None
        return HookResult(
            hook, hook_id, "failed", None, None, (), (), "", message, False, False, None,
            None, text, (), False, time.monotonic() - began,
        )  # fmt: skip
    # From here on the leader exists: any exception, a KeyboardInterrupt included, must end in
    # ``emergency`` (SIGKILL to the group, reap by PID), even before the run is fully set up.
    run = _Run(use_host, tune, proc, view, sel)
    try:
        run.setup()
        run.ident = _identity(use_host, run.pid)
        return _supervise(run, hook, hook_id, timeout, mask, stop, began)
    except BaseException:
        run.emergency()
        raise


def _supervise(
    run: _Run,
    hook: str,
    hook_id: str,
    timeout: float,
    mask: Callable[[str], str],
    stop: Callable[[], str | None] | None,
    began: float,
) -> HookResult:
    tune = run.tuning
    deadline = began + timeout
    next_stop = began
    timed_out = False
    stopped: str | None = None
    killed = False

    def poll_stop(now: float) -> None:
        nonlocal stopped, next_stop
        if stop is not None and stopped is None and now >= next_stop:
            next_stop = now + tune.stop_interval
            stopped = stop()

    while not run.leader_exited():
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        poll_stop(now)
        if stopped:
            break
        run._pump(deadline - now)

    leftovers: int | None = 0
    if timed_out or stopped:
        killed = run.terminate()
    else:
        drain_end = time.monotonic() + tune.drain
        while run.streams_open and time.monotonic() < drain_end:
            poll_stop(time.monotonic())
            if stopped:
                break
            run._pump(drain_end - time.monotonic())
        if stopped:
            killed = run.terminate()
    if not (timed_out or stopped):
        probe = probe_group(run.host, run.pid, exclude=[run.pid])
        leftovers = probe.count if probe.complete else None
    elif killed:
        probe = probe_group(run.host, run.pid, exclude=[run.pid])
        leftovers = probe.count if probe.complete else None
    else:
        leftovers = None

    code: int | None = None
    sig: int | None = None
    if run.leader_done:
        status = reap_held_leader(run.proc)
        if status is not None and status >= 0:
            code = status
        elif status is not None:
            sig = -status
    run.close()

    outcome = classify(hook, code, timed_out=timed_out)
    if stopped and not timed_out:
        outcome = "failed"
    ids: list[str] = []
    bad: list[str] = []
    if not (timed_out or stopped) and sig is None:
        ids, bad = parse_decision_ids(run.out, mask)
    log_stderr = None
    message = None
    if outcome != "allow":
        log_stderr = excerpt(run.err, LOG_STDERR_LIMIT, mask)
        if hook == PRE_START:
            message = excerpt(run.err, MESSAGE_LIMIT, mask)
    return HookResult(
        hook=hook,
        hook_id=hook_id,
        outcome=outcome,
        exit=code,
        signal=sig,
        decision_ids=tuple(ids),
        warnings=tuple(HookWarning("invalid_decision_id", d) for d in bad),
        stderr=log_stderr,
        message=message,
        stdout_truncated=run.out.truncated,
        stderr_truncated=run.err.truncated,
        leftovers=leftovers,
        stopped=stopped,
        start_error=None,
        signals_sent=tuple(run.signals),
        unkillable=bool(timed_out or stopped) and not killed,
        duration=time.monotonic() - began,
    )
