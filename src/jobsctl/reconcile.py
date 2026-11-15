"""Reconcile: recovery of crashes, reboots and shim loss by rule (docs/interface.md, Reconcile;
design.md D11, D30, D33, D38).

The rules run in order and each applies to every job that matches it at that point
(:data:`RULES`; ``hooks_redelivered`` and ``pruned`` are the delivery and retention steps of the
pass, in ``runner.py``). A rule is two halves:

* **decide** reads the job and its current attempt and asks the process layer (identity checks, the
  result file, the `job:<id>` lease) outside any write transaction. Only a *proven dead* process
  counts as gone (``Liveness.DEAD``: not found, reused PID, zombie, another boot id); an
  inconclusive check (``UNKNOWN``) and an alive process both mean "do nothing". A slow shim is
  therefore never finalized, and a dead one stays dead, so a decision cannot go stale in the
  direction that matters.
* **apply** is one compare-and-set write transaction on ``(status, attempt, claim_id)`` through the
  shared transition code of the shim (``shim_store``) and ``claims``, so a live shim, ``_kill`` or
  ``cancel`` that wins the race makes the apply a no-op. The same code runs inside the transaction
  of ``cancel --force`` (:func:`reconcile_job`), which is why apply takes the transaction.

Reconcile signals nothing and fires no hooks: it writes the post-hook events with the transition
like every other transition, and it spawns a detached ``_kill`` only for ``force_resumed``.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from jobsctl import gateway
from jobsctl.claims import LogRecord, requeue_orphaned
from jobsctl.config import Config
from jobsctl.leases import LEASE_COLUMNS, holder_of, parse_identity
from jobsctl.leftovers import Leftovers
from jobsctl.output_capture import TailSnapshot
from jobsctl.proc_group import probe_group
from jobsctl.proc_host import BootIdUnavailable, Host
from jobsctl.proc_identity import Liveness, LivenessResult, check_identity
from jobsctl.result_file import ResultFileError, ResultRecord, delete_result, read_result
from jobsctl.shim_exit import ExitRecord, Outcome, natural_outcome
from jobsctl.shim_store import fail_starting, finish_running_in, read_claim_in
from jobsctl.store_db import Tx
from jobsctl.store_ops import transition_job_by_claim
from jobsctl.timestamps import TimestampError, format_timestamp

# Rules that act on one job's attempt, in the order of the Reconcile table.
JOB_RULES: Final = (
    "result_file_finalized",
    "boot_changed",
    "claim_orphaned",
    "shim_dead",
    "job_gone",
    "shim_lost",
)
RULES: Final = (*JOB_RULES, "force_resumed")
BUDGET_SECONDS: Final = 20.0  # a pass spends at most this long on the rules (lease TTL is 60 s)

_SUBJECTS = (
    "SELECT j.id AS job_id, j.queue, j.status, j.attempt, j.shim_lost, a.claim_id, a.claimed_by, "
    "a.shim_identity, a.shim_confirmed_at, a.job_identity, a.pgid, a.force_requested_by "
    "FROM jobs j JOIN attempts a ON a.job_id = j.id AND a.n = j.attempt "
    "WHERE j.status IN ('starting', 'running')"
)


@dataclass(frozen=True)
class Finding:
    job_id: str
    finding: str
    action: str

    def as_json(self) -> dict[str, str]:
        return {"job_id": self.job_id, "finding": self.finding, "action": self.action}


@dataclass(frozen=True)
class Subject:
    """A `starting` or `running` job with its current attempt, as read."""

    job_id: str
    queue: str
    status: str
    attempt: int
    shim_lost: bool
    claim_id: str
    claimed_by: str
    shim_identity: str | None
    confirmed: bool
    job_identity: str | None
    pgid: int | None
    force_requested_by: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Subject:
        return cls(
            job_id=str(row["job_id"]),
            queue=str(row["queue"]),
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            shim_lost=bool(row["shim_lost"]),
            claim_id=str(row["claim_id"]),
            claimed_by=str(row["claimed_by"]),
            shim_identity=_text_or_none(row["shim_identity"]),
            confirmed=row["shim_confirmed_at"] is not None,
            job_identity=_text_or_none(row["job_identity"]),
            pgid=None if row["pgid"] is None else int(row["pgid"]),
            force_requested_by=_text_or_none(row["force_requested_by"]),
        )


def _text_or_none(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def read_subjects(one_all: Callable[[str, list[Any]], list[sqlite3.Row]]) -> list[Subject]:
    return [Subject.from_row(r) for r in one_all(_SUBJECTS + " ORDER BY j.id", [])]


@dataclass(frozen=True)
class Decision:
    """What a rule found. `kind` selects the write: `result` (finalize from the result file),
    `lost_running`, `lost_starting`, `requeue`, `mark` (shim_lost) or `kill` (force_resumed)."""

    subject: Subject
    finding: str
    kind: str
    result: ResultRecord | None = None
    leftovers: Leftovers | None = None


@dataclass
class Step:
    """What an applied (or, in a dry run, a would-be applied) decision did."""

    finding: Finding
    records: list[LogRecord] = field(default_factory=list)
    after: list[Callable[[], object]] = field(default_factory=list)  # run after the commit
    ended: bool = False  # the job is no longer `starting`/`running` (later rules skip it)
    frees_slot: bool = False


# -- liveness -------------------------------------------------------------------------------------


class Prober:
    """Identity checks over a `Host`. A recorded identity that is not one counts as dead (nothing
    can be alive under it); no recorded identity is `None`."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def check(self, text: str | None) -> LivenessResult | None:
        if text is None:
            return None
        ident = parse_identity(text)
        if ident is None:
            return LivenessResult(Liveness.DEAD, "unparseable")
        return check_identity(self.host, ident)

    def dead(self, text: str | None) -> bool:
        result = self.check(text)
        return result is not None and result.state is Liveness.DEAD

    def alive(self, text: str | None) -> bool:
        result = self.check(text)
        return result is not None and result.state is Liveness.ALIVE


_ALLOWED_FAILURES: Final = frozenset({"exit", "signaled", "timeout", "terminated"})


def _plausible(record: ResultRecord) -> bool:
    """Whether the file records an outcome a shim writes: what the exit alone decides, or a
    `timeout`/`terminated` end after its signal sequence (a forced end is re-read, not written)."""
    exit_record = record.exit
    if exit_record.kind not in ("exited", "signaled"):
        return False
    natural = natural_outcome(exit_record)
    recorded = Outcome(record.status, record.reason, record.hold_reason)
    if recorded == natural:
        return True
    return (
        record.status == "failed"
        and record.reason in _ALLOWED_FAILURES
        and record.hold_reason is None
    )


class Reconciler:
    """The rules over one `Host` and config. `warn` receives one-line diagnostics (a result file
    that cannot be used); `shims` is the state directory's `shims/`."""

    def __init__(
        self,
        config: Config,
        host: Host,
        shims: Path,
        *,
        warn: Callable[[str], None] = lambda message: None,
        spawn_kill: Callable[[str], subprocess.Popen[bytes] | None] | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.shims = shims
        self.warn = warn
        self.spawn_kill = spawn_kill
        self.probe = Prober(host)

    # -- decide ------------------------------------------------------------------------------

    def decide(
        self,
        rule: str,
        s: Subject,
        one: Callable[[str, list[Any]], sqlite3.Row | None],
        now: dt.datetime,
    ) -> Decision | None:
        if rule == "result_file_finalized":
            return self._result_file(s)
        if rule == "boot_changed":
            return self._boot_changed(s)
        if rule == "claim_orphaned":
            return self._claim_orphaned(s)
        if rule == "shim_dead":
            return self._shim_dead(s)
        if rule == "job_gone":
            return self._job_gone(s)
        if rule == "shim_lost":
            return self._shim_lost(s)
        if rule == "force_resumed":
            return self._force_resumed(s, one, now)
        raise ValueError(f"unknown rule {rule!r}")

    def _result_file(self, s: Subject) -> Decision | None:
        # A result file records what the *job* did, so only a `running` attempt can have one.
        if s.status != "running" or not self.probe.dead(s.shim_identity):
            return None
        try:
            record = read_result(self.shims, s.job_id, s.attempt)
        except ResultFileError as exc:
            self.warn(f"{s.job_id}: the result file is not used ({exc})")
            return None
        if record is None:
            return None
        if record.claim_id != s.claim_id:
            self.warn(f"{s.job_id}: the result file belongs to another claim; it is not used")
            return None
        if not _plausible(record):
            self.warn(f"{s.job_id}: the result file records an outcome no shim writes; not used")
            return None
        return Decision(s, "result_file_finalized", "result", result=record)

    def _boot_changed(self, s: Subject) -> Decision | None:
        try:
            current = self.host.boot_id()
        except BootIdUnavailable:
            return None
        reference = s.shim_identity or (s.job_identity if s.status == "running" else s.claimed_by)
        ident = None if reference is None else parse_identity(reference)
        if ident is None or ident.boot_id == current:
            return None
        if s.status == "starting":
            kind = "lost_starting" if s.confirmed else "requeue"
        else:
            kind = "lost_running"
        return Decision(s, "boot_changed", kind)

    def _claim_orphaned(self, s: Subject) -> Decision | None:
        if s.status != "starting" or s.confirmed:
            return None
        gone = (
            self.probe.dead(s.claimed_by)
            if s.shim_identity is None
            else self.probe.dead(s.shim_identity)
        )
        return Decision(s, "claim_orphaned", "requeue") if gone else None

    def _shim_dead(self, s: Subject) -> Decision | None:
        if s.status != "starting" or not s.confirmed or not self.probe.dead(s.shim_identity):
            return None
        return Decision(s, "shim_dead", "lost_starting")

    def _job_gone(self, s: Subject) -> Decision | None:
        if s.status != "running" or not self.probe.dead(s.shim_identity):
            return None
        job = self.probe.check(s.job_identity)
        if job is None or job.state is not Liveness.DEAD:
            return None
        return Decision(s, "job_gone", "lost_running", leftovers=self._group(s, job))

    def _group(self, s: Subject, job: LivenessResult) -> Leftovers | None:
        """The job's process group counted, when its id is verifiable: the leader is gone or a
        zombie (its PID cannot be a stranger's while members remain), never `reused`."""
        ident = None if s.job_identity is None else parse_identity(s.job_identity)
        if ident is None or s.pgid != ident.pid or job.reason not in ("gone", "zombie"):
            return None
        probe = probe_group(self.host, ident.pid, exclude={ident.pid})
        return Leftovers(tuple(sorted(m.pid for m in probe.live)), probe.complete)

    def _shim_lost(self, s: Subject) -> Decision | None:
        if s.status != "running" or s.shim_lost or not self.probe.dead(s.shim_identity):
            return None
        return Decision(s, "shim_lost", "mark") if self.probe.alive(s.job_identity) else None

    def _force_resumed(
        self,
        s: Subject,
        one: Callable[[str, list[Any]], sqlite3.Row | None],
        now: dt.datetime,
    ) -> Decision | None:
        # `_kill` itself requires a proven-dead shim, so only that case starts one.
        if s.status != "running" or s.force_requested_by is None:
            return None
        if not self.probe.dead(s.shim_identity):
            return None
        if holder_of(one(LEASE_COLUMNS, [f"job:{s.job_id}"]), self.host, now) is not None:
            return None
        return Decision(s, "force_resumed", "kill")

    # -- the action name of a decision -------------------------------------------------------

    def action(self, d: Decision, *, forced: bool) -> str:
        if d.kind == "result":
            return "finalized"
        if d.kind in ("lost_running", "lost_starting"):
            return "canceled_forced" if forced else "failed_lost"
        return {"requeue": "requeued", "mark": "marked_shim_lost", "kill": "kill_started"}[d.kind]

    # -- apply (a write transaction) -----------------------------------------------------------

    def apply(self, tx: Tx, d: Decision, now: str) -> Step | None:
        """Make the decision's write in `tx`; `None` when its compare-and-set lost (someone else
        moved the job first)."""
        s = d.subject
        if d.kind == "requeue":
            won, records = requeue_orphaned(tx, s.job_id, s.claim_id, now=now, finding=d.finding)
            if not won:
                return None
            return Step(
                Finding(s.job_id, d.finding, "requeued"), records, ended=True, frees_slot=True
            )
        if d.kind == "lost_starting":
            view = read_claim_in(tx, s.job_id, s.claim_id)
            if view is None or view.status != "starting":
                return None
            won, records = fail_starting(
                tx, self.config, view, s.claim_id, reason="lost", exit_record=None, now=now
            )
            if not won:
                return None
            return self._ended(d, records, "failed_lost")
        if d.kind in ("lost_running", "result"):
            return self._finish(tx, d, now)
        if d.kind == "mark":
            view = read_claim_in(tx, s.job_id, s.claim_id)
            if view is None or view.status != "running" or bool(view.row["shim_lost"]):
                return None
            if not transition_job_by_claim(
                tx,
                s.job_id,
                claim_id=s.claim_id,
                expect_status="running",
                to_status="running",
                updates={"shim_lost": 1},
            ):
                return None
            fields = {"job_id": s.job_id, "queue": s.queue, "attempt": s.attempt}
            return Step(
                Finding(s.job_id, d.finding, "marked_shim_lost"),
                [
                    LogRecord(
                        "shim_lost",
                        "warn",
                        {**fields, "message": "the shim is gone but the job process is alive"},
                    ),
                    self._record(d, "marked_shim_lost"),
                ],
            )
        raise ValueError(f"cannot apply a {d.kind!r} decision in a transaction")

    def _finish(self, tx: Tx, d: Decision, now: str) -> Step | None:
        s = d.subject
        if d.result is not None:
            r = d.result
            outcome = Outcome(r.status, r.reason, r.hold_reason)
            exit_record, leftovers = r.exit, r.leftovers
            tail: TailSnapshot | None = TailSnapshot(
                r.tail.text, r.tail.bytes_seen, r.tail.truncated
            )
        else:
            outcome = Outcome("failed", "lost")
            exit_record = ExitRecord("unknown")
            leftovers = None if d.leftovers is None else d.leftovers.recorded
            tail = None  # the last flushed tail stays
        done = finish_running_in(
            tx,
            self.config,
            s.job_id,
            s.claim_id,
            outcome=outcome,
            exit_record=exit_record,
            leftovers=leftovers,
            tail=tail,
            now=now,
        )
        if not done.committed or done.outcome is None:
            return None
        forced = done.outcome.reason == "forced"
        applied = self._ended(d, list(done.records), self.action(d, forced=forced))
        if d.result is not None:
            applied.after.append(lambda: self._drop_result(s))
        if d.leftovers is not None and d.leftovers.count:
            applied.records.append(
                LogRecord(
                    "leftovers",
                    "warn",
                    {
                        "job_id": s.job_id,
                        "queue": s.queue,
                        "attempt": s.attempt,
                        "message": "processes outlived the job's leader",
                        "details": {"count": d.leftovers.count, "complete": d.leftovers.complete},
                    },
                )
            )
        return applied

    def _ended(self, d: Decision, records: list[LogRecord], action: str) -> Step:
        return Step(
            Finding(d.subject.job_id, d.finding, action),
            [*records, self._record(d, action)],
            ended=True,
            frees_slot=True,
        )

    def _record(self, d: Decision, action: str) -> LogRecord:
        s = d.subject
        return LogRecord(
            "reconcile",
            "info",
            {
                "job_id": s.job_id,
                "queue": s.queue,
                "attempt": s.attempt,
                "finding": d.finding,
                "action": action,
            },
        )

    def _drop_result(self, s: Subject) -> None:
        try:
            delete_result(self.shims, s.job_id, s.attempt)
        except ResultFileError as exc:
            self.warn(f"{s.job_id}: the result file was not removed ({exc})")

    # -- kill (no write) and dry runs --------------------------------------------------------------

    def start_kill(self, d: Decision, children: list[subprocess.Popen[bytes]]) -> Step | None:
        """`force_resumed`: spawn the detached `_kill`; the handle is left in `children` for the
        caller to reap by PID."""
        if self.spawn_kill is None:
            return None
        popen = self.spawn_kill(d.subject.job_id)
        if popen is not None:
            children.append(popen)
        return Step(
            Finding(d.subject.job_id, d.finding, "kill_started"), [self._record(d, "kill_started")]
        )

    def preview(self, d: Decision) -> Step:
        """The finding a dry run reports for `d`, nothing written."""
        s = d.subject
        forced = s.force_requested_by is not None and d.kind == "lost_running"
        if d.kind == "result":
            forced = False
        action = self.action(d, forced=forced)
        ended = d.kind in ("result", "lost_running", "lost_starting", "requeue")
        return Step(Finding(s.job_id, d.finding, action), ended=ended, frees_slot=ended)


# -- running the rules -------------------------------------------------------------------------


def now_text(clock: Callable[[], dt.datetime]) -> str:
    return format_timestamp(clock())


def one_all_of(tx: Tx) -> Callable[[str, list[Any]], list[sqlite3.Row]]:
    return lambda sql, params: tx.all(sql, params)


def run_rules(
    reconciler: Reconciler,
    *,
    read: Callable[[], Any],
    write: Callable[[], Any],
    clock: Callable[[], dt.datetime],
    dry_run: bool,
    tick: Callable[[], None] = lambda: None,
    children: list[subprocess.Popen[bytes]] | None = None,
    emit: Callable[[list[LogRecord]], None] = lambda records: None,
    rules: Sequence[str] = RULES,
    budget: float = BUDGET_SECONDS,
    fail: Callable[[str, str, Exception], None] = lambda job_id, rule, exc: None,
) -> list[Step]:
    """The rules in order over every matching job of the store (a pass, or `reconcile`).

    `read` and `write` are the store's ``read()`` and ``write()`` context managers. `tick` runs
    before each job (it renews the runner lease and may raise to stop the pass). One job's failure
    (a busy database, an I/O error) is passed to `fail` and never stops the others; the whole run
    stops after `budget` seconds (the rest waits for the next pass). A dry run reads only.
    """
    applied: list[Step] = []
    ended: set[str] = set()
    sink: list[subprocess.Popen[bytes]] = children if children is not None else []
    deadline = gateway.monotonic() + budget
    for rule in rules:
        with read() as tx:
            subjects = read_subjects(one_all_of(tx))
        for s in subjects:
            if s.job_id in ended:
                continue
            if gateway.monotonic() > deadline:
                return applied
            tick()
            try:
                result = _one(reconciler, rule, s, read, write, clock, dry_run, sink)
            except (OSError, sqlite3.Error, ValueError) as exc:
                fail(s.job_id, rule, exc)
                continue
            except Exception as exc:
                if not _is_jobsctl_error(exc):
                    raise
                fail(s.job_id, rule, exc)
                continue
            if result is None:
                continue
            applied.append(result)
            emit(result.records)
            for action in result.after:
                _after(action, reconciler, s)
            if result.ended:
                ended.add(s.job_id)
    return applied


def _is_jobsctl_error(exc: Exception) -> bool:
    from jobsctl.errors import JobsctlError

    return isinstance(exc, JobsctlError) and exc.code != "version_skew"


def _after(action: Callable[[], object], reconciler: Reconciler, s: Subject) -> None:
    try:
        action()
    except Exception as exc:  # the work is committed; a leftover file is swept later
        reconciler.warn(f"{s.job_id}: an after-commit step failed ({type(exc).__name__})")


def _one(
    reconciler: Reconciler,
    rule: str,
    s: Subject,
    read: Callable[[], Any],
    write: Callable[[], Any],
    clock: Callable[[], dt.datetime],
    dry_run: bool,
    children: list[subprocess.Popen[bytes]],
) -> Step | None:
    with read() as rtx:
        decision = reconciler.decide(rule, s, rtx.one, clock())
    if decision is None:
        return None
    if dry_run:
        return reconciler.preview(decision)
    if decision.kind == "kill":
        return reconciler.start_kill(decision, children)
    with write() as tx:
        return reconciler.apply(tx, decision, now_text(clock))


# -- one job inside another command's transaction (cancel --force) ---------------------------------


def reconcile_job(
    tx: Tx,
    reconciler: Reconciler,
    job_id: str,
    *,
    now: str,
    write: bool,
) -> tuple[list[Step], Subject | None]:
    """The job rules for one job inside the caller's transaction (`write`: its write transaction;
    else a read transaction and nothing is changed). Returns what was applied and the subject as
    last read (`None` when the job is not `starting`/`running` any more).

    `force_resumed` is not among them: ``cancel --force`` starts the `_kill` itself, after its own
    commit (``cancel_force.deliver_request``).
    """
    applied: list[Step] = []
    try:
        clock_now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    except (ValueError, TimestampError):
        clock_now = gateway.utc_now()
    for rule in JOB_RULES:
        rows = tx.all(_SUBJECTS + " AND j.id = ?", [job_id])
        if not rows:
            return applied, None
        s = Subject.from_row(rows[0])
        decision = reconciler.decide(rule, s, tx.one, clock_now)
        if decision is None:
            continue
        result = reconciler.preview(decision) if not write else reconciler.apply(tx, decision, now)
        if result is None:
            continue
        applied.append(result)
        if result.ended:
            return applied, None
    rows = tx.all(_SUBJECTS + " AND j.id = ?", [job_id])
    return applied, (Subject.from_row(rows[0]) if rows else None)
