"""Delivery of post-hook outbox events (docs/interface.md, Delivery and Hook processes).

A transition and its post-hook event are committed together (``hook_events.write_post_event``);
this module runs the hooks of an event afterwards, in the process that holds the right to do it:
the holder of the job's ``job:<id>`` lease for synchronous events (``deliver_sync``), or
``_deliver`` for ``async`` ones and for the events of jobs with no live holder (``deliver.py``).

Delivery is at least once and in order per job:

- an event is marked delivered only after every hook it selects ran to its end (exit or timeout)
  and, in a command, after ``InterruptGuard.check()`` passed. A hook stopped by a signal or a
  stop request leaves the event undelivered, and a crash between a hook and the mark runs the
  hook again (the same ``event_id``). A lease lost while hooks run does not undo them: the
  event whose hooks all ran is still marked delivered (a duplicate run by the new holder is
  allowed, delivery being at least once); once the loss is seen no further *event* is started,
  but the hooks of the event in progress finish;
- the hooks of an event are selected **at delivery** from ``payload.labels`` and
  ``payload.job.queue`` with the delivering process's config; the outbox ``async`` column is a
  hint only: an event delivered as synchronous runs the entries that are not ``async`` now, an
  ``async`` one the ``async`` entries. An entry that is no longer configured runs nothing;
- a failing or timed-out hook is logged (``hook_ran``) and never retried; a hook whose leader
  outlived SIGKILL leaves its event undelivered and backs the event off (``hook_retry``): it is
  skipped until its ``retry_after``, and retried at the capped interval for ever;
- nothing here runs inside a write transaction: hooks, log appends and the delivered mark each
  have their own.

``output_tail`` is ``null`` in the stored payload; an entry with ``include_output`` gets the
stored final tail of the event's attempt at delivery, masked and sanitized again with the masking
set of the delivering process.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsctl import gateway
from jobsctl.config import Config
from jobsctl.env_canonical import build_canonical_env
from jobsctl.env_file import load_env_file
from jobsctl.env_mask import MaskingSet, masking_values
from jobsctl.errors import JobsctlError
from jobsctl.hook_env import build_hook_env
from jobsctl.hook_payload import MAX_OUTPUT_TAIL_CHARS, OUTPUT_HOOKS, encode_payload
from jobsctl.hook_retry import deferred_until, record_unkillable
from jobsctl.hook_run import HookResult, HookTuning, run_hook
from jobsctl.hook_select import select_hooks
from jobsctl.log_writer import RecordWriter
from jobsctl.output_store import load_tail
from jobsctl.proc_host import Host
from jobsctl.store_db import Store
from jobsctl.store_ops import outbox_mark_delivered
from jobsctl.textsafe import sanitize
from jobsctl.timestamps import format_timestamp

__all__ = [
    "POST_HOOKS",
    "Deliverer",
    "EventPlan",
    "HookEnvironment",
    "PlannedHook",
    "UnkillableHook",
]

POST_HOOKS = ("post-start", "post-finish", "post-fail", "post-held", "post-cancel")

Stop = Callable[[], str | None]
Check = Callable[[HookResult | None], None]
Runner = Callable[..., HookResult]


@dataclass(frozen=True)
class HookEnvironment:
    """What a delivering process needs to build hook environments and mask text: its canonical
    environment, the hook-visible ``env_file`` values and the masking set. Values stay out of
    the repr."""

    config: Config
    canonical: Mapping[str, str] = field(repr=False)
    env_file_values: Mapping[str, str] = field(repr=False)
    masking: MaskingSet

    @classmethod
    def load(cls, config: Config, environ: Mapping[str, str] | None = None) -> HookEnvironment:
        """Read ``env_file`` now and build the canonical environment from ``environ`` (default:
        the process's own). ``HOME`` is part of the result only when ``env.inherit`` names it:
        the ``HOME`` the spawning process passes to internal processes is not inherited."""
        env_file = load_env_file(config.env_file)
        canonical = build_canonical_env(config, os.environ if environ is None else environ)
        masking = masking_values(config, canonical, env_file)
        values = env_file.hook_values()[0] if env_file.available else {}
        return cls(config, canonical.values, values, masking)

    def for_hook(self, hook: str, job_id: str) -> dict[str, str]:
        return build_hook_env(
            self.canonical,
            hook=hook,
            job_id=job_id,
            env_file=self.env_file_values,
            strip=self.config.hooks_strip_env,
        )


@dataclass(frozen=True)
class PlannedHook:
    """One hook ready to run: everything is decided here, so a worker thread only runs it."""

    hook: str
    hook_id: str
    cmd: str = field(repr=False)
    timeout: float
    payload: bytes = field(repr=False)
    env: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class EventPlan:
    event_id: str
    job_id: str
    hooks: tuple[PlannedHook, ...]
    queue: str | None = None
    problem: str | None = None  # the stored event cannot be delivered (never retried)


class UnkillableHook(JobsctlError):
    """A hook's leader outlived SIGKILL (``internal``): its event stays undelivered and is
    deferred by :meth:`Deliverer.note_unkillable`."""


class Deliverer:
    """Plans, runs and records the delivery of outbox events for one process.

    ``runner`` is ``run_hook`` (tests replace it); ``diag`` receives short messages (never
    hook output or environment values); ``cwd`` is jobsctl's state directory. A command's
    delivery passes ``announce_deferred=False``: only ``_deliver`` and the shim say so."""

    def __init__(
        self,
        store: Store,
        config: Config,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], Any] = gateway.utc_now,
        host: Host | None = None,
        tuning: HookTuning | None = None,
        runner: Runner = run_hook,
        diag: Callable[[str], None] | None = None,
        announce_deferred: bool = True,
    ) -> None:
        self.store = store
        self.config = config
        self.environ = environ
        self.clock = clock
        self.host = host
        self.tuning = tuning
        self.runner = runner
        self.diag = diag or _stderr_warning
        self.announce_deferred = announce_deferred  # False: a command says nothing of a deferral
        self.cwd: Path = store.paths.root
        self._noticed: set[str] = set()
        self.env = HookEnvironment.load(config, environ)
        self.writer = RecordWriter(store, masking=self.env.masking, clock=clock)

    def refresh(self) -> None:
        """Read ``env_file`` again (a long-lived process calls it at the start of a round)."""
        self.env = HookEnvironment.load(self.config, self.environ)
        self.writer = RecordWriter(self.store, masking=self.env.masking, clock=self.clock)

    # -- reading the outbox --------------------------------------------------------------------

    def next_event(self, job_id: str, *, is_async: bool) -> sqlite3.Row | None:
        """The oldest undelivered event of ``job_id`` of the given kind."""
        rows = self.store.query_all(
            "SELECT * FROM hook_outbox WHERE job_id = ? AND delivered_at IS NULL AND async = ? "
            "ORDER BY seq LIMIT 1",
            [job_id, int(is_async)],
        )
        return rows[0] if rows else None

    def has_pending(self, job_id: str, *, is_async: bool | None = None) -> bool:
        sql = "SELECT 1 FROM hook_outbox WHERE job_id = ? AND delivered_at IS NULL"
        params: list[Any] = [job_id]
        if is_async is not None:
            sql += " AND async = ?"
            params.append(int(is_async))
        return self.store.query_one(sql + " LIMIT 1", params) is not None

    # -- planning ------------------------------------------------------------------------------

    def plan_event(self, row: sqlite3.Row) -> EventPlan:
        """The hooks to run for ``row`` with this process's config. A stored event that is not
        what ``hook_events`` writes (a damaged or foreign row) has a ``problem`` and no hooks."""
        job_id, hook, event_id = str(row["job_id"]), str(row["hook"]), str(row["event_id"])
        try:
            payload, queue, labels = _read_payload(row)
        except (ValueError, KeyError, TypeError, RecursionError):
            return EventPlan(event_id, job_id, (), problem="payload")
        planned: list[PlannedHook] = []
        for chosen in select_hooks(self.config, hook, queue, labels):
            if bool(getattr(chosen.entry, "async_", False)) != bool(row["async"]):
                continue
            body = dict(payload)
            body.pop("output_tail", None)  # whatever a stored payload carries is dropped
            if hook in OUTPUT_HOOKS:  # the field exists only on these hooks
                tail = None
                if getattr(chosen.entry, "include_output", False) and (
                    payload.get("output_available") is True
                ):
                    tail = self._tail(job_id, payload.get("attempt"))
                body["output_tail"] = tail
            planned.append(
                PlannedHook(
                    hook=hook,
                    hook_id=chosen.hook_id,
                    cmd=chosen.entry.cmd,
                    timeout=float(chosen.entry.timeout),
                    payload=encode_payload(body),
                    env=self.env.for_hook(hook, job_id),
                )
            )
        return EventPlan(event_id, job_id, tuple(planned), queue=queue)

    def _tail(self, job_id: str, attempt: object) -> str | None:
        """The stored final tail of the event's attempt, masked and sanitized again; ``None``
        when there is none or the row is unreadable."""
        number = attempt.get("attempt") if isinstance(attempt, dict) else None
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        try:
            with self.store.read() as tx:
                stored = load_tail(tx, job_id, number)
        except (JobsctlError, sqlite3.Error) as exc:
            self.diag(f"the output tail of {job_id} was not read ({type(exc).__name__})")
            return None
        if stored is None:
            return None
        return sanitize(stored.snapshot.text, mask=self.env.masking.mask)[:MAX_OUTPUT_TAIL_CHARS]

    # -- running -------------------------------------------------------------------------------

    def run_hooks(
        self, plan: EventPlan, *, stop: Stop, on_result: Callable[[HookResult], None]
    ) -> bool:
        """Run the planned hooks in order, handing each result to ``on_result`` (which may
        raise to abandon the event). ``True`` when all of them ran to their end; ``False`` when
        one was stopped (the event stays undelivered). A hook whose leader outlived SIGKILL is
        an internal error. Safe to call from a worker thread when ``stop`` and ``on_result``
        are."""
        for planned in plan.hooks:
            result = self.runner(
                hook=planned.hook,
                hook_id=planned.hook_id,
                cmd=planned.cmd,
                payload=planned.payload,
                timeout=planned.timeout,
                env=planned.env,
                cwd=self.cwd,
                mask=self.env.masking.mask,
                stop=stop,
                host=self.host,
                tuning=self.tuning,
            )
            on_result(result)
            if result.unkillable:
                raise UnkillableHook(
                    "internal",
                    f"a {planned.hook} hook outlived SIGKILL; its event stays undelivered",
                )
            if result.stopped is not None:
                return False
        return True

    def deliver_sync(
        self,
        job_id: str,
        *,
        stop: Stop = lambda: None,
        check: Check | None = None,
        held: Callable[[], bool] = lambda: True,
        tick: Callable[[], None] = lambda: None,
    ) -> bool:
        """Deliver ``job_id``'s undelivered synchronous events, oldest first, in this process.
        The caller holds the job's lease: ``held`` says whether it still does, ``tick`` renews
        it (called while a hook runs, about every 0.25 s). ``check`` runs after each hook and
        before an event is marked delivered (a command passes ``InterruptGuard.check``; it
        raises to abandon the delivery). ``True`` when no synchronous event is left
        undelivered; ``False`` when the delivery stopped early (``stop`` answered, or the lease
        was lost) or the next event is deferred (backing off), and left events undelivered."""
        self.refresh()

        def stop_and_tick() -> str | None:
            tick()
            return stop()

        while True:
            row = self.next_event(job_id, is_async=False)
            if row is None:
                return True
            if not held() or stop():
                return False
            if self.defer_notice(row):
                return False
            plan = self.plan_event(row)
            if plan.problem is None:

                def on_result(result: HookResult, plan: EventPlan = plan) -> None:
                    self.record(plan, result)
                    if check is not None:
                        check(result)

                try:
                    ran_all = self.run_hooks(plan, stop=stop_and_tick, on_result=on_result)
                except UnkillableHook:
                    self.note_unkillable(plan.event_id)
                    raise
                if not ran_all:
                    return False
                if check is not None:
                    check(None)
            else:
                self.record_problem(plan)
            self.mark_delivered(plan.event_id)
            if not held():
                return False

    # -- backoff -------------------------------------------------------------------------------

    def defer_notice(self, row: sqlite3.Row) -> bool:
        """Whether ``row`` is deferred (its ``retry_after`` is in the future); says so through
        ``diag`` (never twice for one event in one process)."""
        until = deferred_until(row, self.clock())
        if until is None:
            return False
        event_id = str(row["event_id"])
        if self.announce_deferred and event_id not in self._noticed:
            self._noticed.add(event_id)
            self.diag(
                f"the hook event {event_id} is deferred until {until} (a hook outlived SIGKILL)"
            )
        return True

    def note_unkillable(self, event_id: str) -> int | None:
        """Count a hook of ``event_id`` that outlived SIGKILL and defer the event (main thread
        only: it writes). Returns the new attempt count; a failure to write is a diagnostic."""
        try:
            with self.store.write() as tx:
                return record_unkillable(tx, event_id, self.clock())
        except (JobsctlError, sqlite3.Error) as exc:
            self.diag(f"the retry of {event_id} was not deferred ({type(exc).__name__})")
            return None

    # -- recording -----------------------------------------------------------------------------

    def record(self, plan: EventPlan, result: HookResult) -> None:
        """Append the ``hook_ran`` record of a finished hook (and one ``invalid_decision_id`` per
        warning). A hook that was stopped before it started did not run: it has no record. A
        record that cannot be written never fails the delivery."""
        if not result.started:
            return
        fields = {k: v for k, v in result.log_fields(plan.job_id).items() if k != "event"}
        if plan.queue is not None:
            fields["queue"] = plan.queue
        self._best_effort("hook_ran", lambda: self.writer.log("hook_ran", fields=fields))
        for warning in result.warnings:
            detail = {"hook": result.hook, "hook_id": result.hook_id, "invalid": warning.detail}
            self._best_effort(
                "invalid_decision_id",
                lambda detail=detail: self.writer.log(
                    "invalid_decision_id",
                    level="warn",
                    fields={
                        "job_id": plan.job_id,
                        "queue": plan.queue,
                        "message": "a hook printed a decision id that is not valid",
                        "details": detail,
                    },
                ),
            )

    def record_problem(self, plan: EventPlan) -> None:
        """A stored event that cannot be delivered is logged and skipped (it would block the
        job's later events for ever)."""
        self._best_effort(
            "pass",
            lambda: self.writer.log(
                "pass",
                level="warn",
                fields={
                    "job_id": plan.job_id,
                    "message": "an undeliverable hook event was skipped",
                    "details": {"step": "deliver", "problem": plan.problem},
                },
            ),
        )

    def mark_delivered(self, event_id: str) -> None:
        with self.store.write() as tx:
            outbox_mark_delivered(tx, event_id, format_timestamp(self.clock()))

    def _best_effort(self, what: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:  # the hook ran: a record that is not written changes nothing
            self.diag(f"a {what} record was not written ({type(exc).__name__})")


def _read_payload(row: sqlite3.Row) -> tuple[dict[str, Any], str, list[str]]:
    """The stored payload, checked for what delivery relies on (``ValueError`` otherwise)."""
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict) or row["hook"] not in POST_HOOKS:
        raise ValueError("not a post-hook payload")
    if (
        payload.get("event_id") != row["event_id"]
        or payload.get("hook") != row["hook"]
        or payload.get("object_id") != row["job_id"]
    ):
        raise ValueError("the payload does not match its outbox row")
    queue = payload["job"]["queue"]
    labels = payload["labels"]
    if not isinstance(queue, str) or not isinstance(labels, list):
        raise ValueError("queue or labels malformed")
    if not all(isinstance(label, str) for label in labels):
        raise ValueError("labels malformed")
    return payload, queue, labels


def _stderr_warning(message: str) -> None:
    sys.stderr.write(f"jobsctl: warning: {message}\n")
    sys.stderr.flush()
