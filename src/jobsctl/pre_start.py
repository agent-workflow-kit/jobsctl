"""The `pre-start` hooks of the shim (docs/interface.md, Start sequence step 3, and Hooks).

:class:`PreStart` runs the selected hooks of one `starting` attempt in configured order, with
the shim holding the `job:<id>` lease. Fail-closed rules:

- a vetoing result (exit other than 0 and 13, a timeout, a kill by signal, a hook that cannot be
  started) ends the job `canceled` (`vetoed`) and stops the loop; a veto beats any escalation;
- exit 13 escalates and the rest still run; without a veto the job ends `held` (`escalated`);
- a hook named in the job's `override_hooks` still runs and its decision ids are recorded, but
  its exit 13 counts as allow; `override_hooks` is cleared once the hooks have run (in the same
  transaction as the verdict) and kept when the loop is cut short;
- before each hook, and at least every 0.25 s while one runs, the job row is re-read: a job that
  is no longer `starting` under this claim stops the hook's process group (only the hook's own
  group, through :func:`jobsctl.hook_run.run_hook`) and runs no more hooks;
- a hook whose leader outlived SIGKILL is an internal error: nothing is written and the job is
  not started (reconcile deals with the dead shim).
"""

from __future__ import annotations

import enum
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jobsctl.claims import LogRecord, transition_record
from jobsctl.config import Config
from jobsctl.delivery import Deliverer, EventPlan
from jobsctl.errors import JobsctlError
from jobsctl.hook_events import attempt_object
from jobsctl.hook_payload import build_payload, encode_payload
from jobsctl.hook_run import HookResult
from jobsctl.hook_select import pre_start_event_id, select_hooks
from jobsctl.job_view import job_object
from jobsctl.shim_store import ClaimView, post_event, read_claim, read_claim_in
from jobsctl.store_db import Store, Tx
from jobsctl.store_ops import transition_job_by_claim, update_attempt_by_claim

__all__ = ["PreStart", "Verdict"]

HOOK = "pre-start"


class Verdict(enum.Enum):
    PROCEED = "proceed"  # every hook allowed: start the job
    ENDED = "ended"  # vetoed or held: the transition is committed
    GONE = "gone"  # the job is no longer `starting` under this claim
    TERMINATED = "terminated"  # SIGTERM while the hooks ran
    INTERNAL = "internal"  # a hook outlived SIGKILL: write nothing, do not start


@dataclass
class _Tally:
    decision_ids: list[str] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    vetoed_by: str | None = None

    def add_ids(self, ids: tuple[str, ...]) -> None:
        for one in ids:
            if one not in self.decision_ids:
                self.decision_ids.append(one)


class PreStart:
    def __init__(
        self,
        store: Store,
        config: Config,
        deliverer: Deliverer,
        job_id: str,
        claim_id: str,
        *,
        terminated: Callable[[], bool],
        tick: Callable[[], None],
        now: Callable[[], str],
        retrying: Callable[[Callable[[], Any], float], Any],
        busy_seconds: float,
        diag: Callable[[str], None],
    ) -> None:
        self.store = store
        self.config = config
        self.deliverer = deliverer
        self.job_id = job_id
        self.claim_id = claim_id
        self.terminated = terminated
        self.tick = tick
        self.now = now
        self.retrying = retrying
        self.busy_seconds = busy_seconds
        self.diag = diag
        self.records: list[LogRecord] = []  # transition records to emit after the commit

    # -- the loop ------------------------------------------------------------------------------

    def _stop_reason(self) -> str | None:
        """Polled by `run_hook`: renews the lease, then looks at the job row and at SIGTERM. A
        failed read is no verdict (the next poll looks again)."""
        self.tick()
        try:
            view = read_claim(self.store, self.job_id, self.claim_id)
            if view is None or view.status != "starting":
                return "canceled"
        except (JobsctlError, sqlite3.Error):
            pass
        return "SIGTERM" if self.terminated() else None

    def _payload_parts(self, view: ClaimView) -> tuple[dict[str, Any], dict[str, Any]] | None:
        with self.store.read() as tx:
            row = tx.one("SELECT * FROM jobs WHERE id = ?", [self.job_id])
            att = tx.one(
                "SELECT * FROM attempts WHERE job_id = ? AND claim_id = ?",
                [self.job_id, self.claim_id],
            )
            if row is None or att is None:
                return None
            job = job_object(tx, row, self.config, env_file_unavailable=False, now=self.now())
            return job, attempt_object(att)

    def run(self, view: ClaimView) -> Verdict:
        """Run the hooks for `view` (the job, `starting` under this claim)."""
        queue = view.queue
        parts = self._payload_parts(view)
        if parts is None:
            return Verdict.GONE
        job, att = parts
        selected = select_hooks(self.config, HOOK, queue, job["labels"])
        override = set(json.loads(view.row["override_hooks"]))
        tally = _Tally()
        for chosen in selected:
            if self._stop_reason() == "canceled":
                return self._cut_short(tally, Verdict.GONE)
            event_id = pre_start_event_id(self.claim_id, chosen.index)
            payload = build_payload(
                HOOK,
                event_id=event_id,
                job=job,
                previous_status="queued",
                attempt=att,
            )
            result = self.deliverer.runner(
                hook=HOOK,
                hook_id=chosen.hook_id,
                cmd=chosen.entry.cmd,
                payload=encode_payload(payload),
                timeout=float(chosen.entry.timeout),
                env=self.deliverer.env.for_hook(HOOK, self.job_id),
                cwd=self.deliverer.cwd,
                mask=self.deliverer.env.masking.mask,
                stop=self._stop_reason,
                host=self.deliverer.host,
                tuning=self.deliverer.tuning,
            )
            self.deliverer.record(EventPlan(event_id, self.job_id, (), queue=queue), result)
            tally.add_ids(result.decision_ids)
            if result.unkillable:
                self.diag(
                    f"a pre-start hook outlived SIGKILL; the job {self.job_id} is not started"
                )
                return Verdict.INTERNAL
            if result.stopped is not None:
                verdict = Verdict.GONE if result.stopped == "canceled" else Verdict.TERMINATED
                return self._cut_short(tally, verdict)
            self._tally(tally, result, override)
            if tally.vetoed_by is not None:
                break
        return self._finish(view, tally)

    @staticmethod
    def _tally(tally: _Tally, result: HookResult, override: set[str]) -> None:
        if result.vetoes:
            tally.vetoed_by = result.hook_id
            tally.messages.append(_message(result, "veto"))
        elif result.escalates and result.hook_id not in override:
            tally.escalated.append(result.hook_id)
            tally.messages.append(_message(result, "escalate"))

    # -- the verdict ---------------------------------------------------------------------------

    def _cut_short(self, tally: _Tally, verdict: Verdict) -> Verdict:
        """Hooks were stopped: keep what the ones that ran decided (their ids), write no verdict.
        The attempt row is written even if the job was canceled meanwhile."""
        if tally.decision_ids:
            try:
                self.retrying(self._write_ids(tally), self.busy_seconds)
            except Exception as exc:  # the ids are a nicety; the stop is what matters
                self.diag(f"decision ids were not recorded ({type(exc).__name__})")
        return verdict

    def _write_ids(self, tally: _Tally) -> Callable[[], bool]:
        def write() -> bool:
            with self.store.write() as tx:
                return update_attempt_by_claim(
                    tx,
                    self.job_id,
                    self.claim_id,
                    {"decision_ids": json.dumps(tally.decision_ids)},
                )

        return write

    def _finish(self, view: ClaimView, tally: _Tally) -> Verdict:
        def write() -> tuple[bool, list[LogRecord]]:
            with self.store.write() as tx:
                fresh = read_claim_in(tx, self.job_id, self.claim_id)
                if fresh is None or fresh.status != "starting":
                    return False, []
                return self._commit(tx, fresh, tally)

        won, records = self.retrying(write, self.busy_seconds)
        self.records = records
        if not won:
            return Verdict.GONE
        return Verdict.ENDED if tally.vetoed_by or tally.escalated else Verdict.PROCEED

    def _commit(self, tx: Tx, view: ClaimView, tally: _Tally) -> tuple[bool, list[LogRecord]]:
        now = self.now()
        attempt_fields: dict[str, Any] = {
            "decision_ids": json.dumps(tally.decision_ids),
            "hook_messages": json.dumps(tally.messages),
            "escalated_hooks": json.dumps(tally.escalated),
        }
        job_fields: dict[str, Any] = {"override_hooks": "[]"}  # the hooks have run
        if tally.vetoed_by is None and not tally.escalated:  # allowed: the job goes on starting
            won = transition_job_by_claim(
                tx,
                self.job_id,
                claim_id=self.claim_id,
                expect_status="starting",
                to_status="starting",
                updates=job_fields,
            )
            if not won:
                return False, []
            update_attempt_by_claim(tx, self.job_id, self.claim_id, attempt_fields)
            return True, []
        if tally.vetoed_by is not None:
            to, hook = "canceled", "post-cancel"
            job_fields.update(reason="vetoed", ended_at=now, status_changed_at=now, not_before=None)
            attempt_fields.update(
                status="canceled", reason="vetoed", ended_at=now, vetoed_by=tally.vetoed_by
            )
            extra: dict[str, Any] = {
                "reason": "vetoed",
                "attempt_status": "canceled",
                "attempt_reason": "vetoed",
            }
        else:
            to, hook = "held", "post-held"
            job_fields.update(hold_reason="escalated", status_changed_at=now, not_before=None)
            attempt_fields.update(status="held", hold_reason="escalated", ended_at=now)
            extra = {"hold_reason": "escalated", "attempt_status": "held"}
        won = transition_job_by_claim(
            tx,
            self.job_id,
            claim_id=self.claim_id,
            expect_status="starting",
            to_status=to,
            updates=job_fields,
        )
        if not won:
            return False, []
        update_attempt_by_claim(tx, self.job_id, self.claim_id, attempt_fields)
        post_event(tx, self.config, hook, self.job_id, view.attempt, "starting", now)
        record = transition_record(
            self.job_id, view.queue, "starting", to, view.attempt, retry_pending=False, **extra
        )
        return True, [record]


def _message(result: HookResult, outcome: str) -> dict[str, str]:
    return {"hook_id": result.hook_id, "outcome": outcome, "message": result.message or ""}
