"""Ordering, selection and capacity (interface.md: Order, Selection, Capacity).

Three parts, none of which prints or logs:

* **Position keys.** A position is sortable text: 20 digits, optionally followed by `.` and
  decimal digits without a trailing zero (`00000000001000000000000000`, `...0000.5`). Text order
  equals numeric order, so SQLite's binary collation orders keys correctly and `key_between`
  can always insert between two keys. Keys are unique per queue among queued and held jobs
  (new keys are checked against every live job, also of other bands), so ties are never needed;
  `rank` and selection still break one by job id. Repeated moves between the same neighbours
  lengthen a key by about one digit per three moves; past `MAX_KEY_LENGTH` the queue is
  renumbered (order preserved) before the key is chosen again.
* **Rank, `blocked_by`, move planning** on a transaction.
* **Selection and capacity** as pure functions over a snapshot (`load_snapshot`), which the
  runner and the `capacity` and `queues` commands share.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.store_db import Tx

KEY_WIDTH: Final = 20
MIDPOINT: Final = 10**15
RENUMBER_STEP: Final = 1000
MAX_KEY_LENGTH: Final = 64
LIVE: Final = ("queued", "held")  # the statuses that keep a place in the queue
RUNNING: Final = ("starting", "running")

# -- position keys ---------------------------------------------------------------------------


def _format(units: int, scale: int) -> str:
    """The key for `units / 10**scale`: canonical (no trailing zero in the fraction)."""
    whole, frac = divmod(units, 10**scale)
    if not 0 <= whole < 10**KEY_WIDTH:
        raise OverflowError("position key out of range")
    text = f"{whole:0{KEY_WIDTH}d}"
    digits = f"{frac:0{scale}d}".rstrip("0") if scale else ""
    return f"{text}.{digits}" if digits else text


def _parse(key: str) -> tuple[int, int]:
    """(units, scale): the value of `key` as `units / 10**scale`."""
    whole, _, frac = key.partition(".")
    return int(whole + frac), len(frac)


def integer_key(number: int) -> str:
    return _format(number, 0)


def key_between(lo: str | None, hi: str | None) -> str:
    """A key strictly between `lo` and `hi` (`None`: unbounded on that side).

    Raises `OverflowError` when no non-negative integer part is left below `hi`, and
    `ValueError` when `lo >= hi`.
    """
    if lo is None and hi is None:
        return integer_key(MIDPOINT)
    if lo is None:
        assert hi is not None
        return integer_key(int(hi.partition(".")[0]) - 1)
    if hi is None:
        return integer_key(int(lo.partition(".")[0]) + 1)
    lo_units, lo_scale = _parse(lo)
    hi_units, hi_scale = _parse(hi)
    scale = max(lo_scale, hi_scale)
    a = lo_units * 10 ** (scale - lo_scale)
    b = hi_units * 10 ** (scale - hi_scale)
    if a >= b:
        raise ValueError("lo must sort before hi")
    while (a + b) // 2 <= a:
        a, b, scale = a * 10, b * 10, scale + 1
    return _format((a + b) // 2, scale)


# -- rows and neighbours ---------------------------------------------------------------------

_LIVE_SQL = "status IN ('queued', 'held')"

# Planners take `overlay`: `None` renumbers in the database (the real path); a dict leaves the
# database alone (`--dry-run`, a read-only connection) and collects the new keys there, so the
# projection of a move that needs a renumber is computed without writing.
Overlay = dict[str, str] | None


def _live_rows(tx: Tx, queue: str, overlay: Overlay) -> list[dict[str, object]]:
    """The queued and held jobs of `queue` as plain dicts (id, position, pinned, priority),
    with the overlay's keys applied."""
    rows = tx.all(
        f"SELECT id, position, pinned, priority FROM jobs WHERE queue = ? AND {_LIVE_SQL}", [queue]
    )
    found = [dict(r) for r in rows]
    if overlay:
        for r in found:
            r["position"] = overlay.get(str(r["id"]), r["position"])
    return found


def _live_positions(tx: Tx, queue: str, exclude: str | None, overlay: Overlay = None) -> list[str]:
    return [str(r["position"]) for r in _live_rows(tx, queue, overlay) if r["id"] != exclude]


def renumber(tx: Tx, queue: str, overlay: Overlay = None) -> None:
    """Give the queued and held jobs of `queue` fresh, evenly spaced integer keys in their
    current (position, id) order. Relative order never changes. With an `overlay` the new keys
    go there and nothing is written."""
    rows = sorted(_live_rows(tx, queue, overlay), key=_sort_key)
    for index, row in enumerate(rows):
        key = integer_key(MIDPOINT + index * RENUMBER_STEP)
        if overlay is None:
            tx.execute("UPDATE jobs SET position = ? WHERE id = ?", [key, row["id"]])
        else:
            overlay[str(row["id"])] = key


def tail_position(
    tx: Tx, queue: str, *, exclude: str | None = None, overlay: Overlay = None
) -> str:
    """A key after every queued or held job of `queue` (but `exclude`)."""
    return _edge(tx, queue, exclude, tail=True, overlay=overlay)


def head_position(
    tx: Tx, queue: str, *, exclude: str | None = None, overlay: Overlay = None
) -> str:
    """A key before every queued or held job of `queue` (but `exclude`): before every band, so
    it is also the top of any band."""
    return _edge(tx, queue, exclude, tail=False, overlay=overlay)


def _edge(tx: Tx, queue: str, exclude: str | None, *, tail: bool, overlay: Overlay) -> str:
    for attempt in range(2):
        found = _live_positions(tx, queue, exclude, overlay)
        try:
            if not found:
                return integer_key(MIDPOINT)
            if tail:
                return key_between(max(found), None)
            return key_between(None, min(found))
        except OverflowError:
            if attempt:
                raise JobsctlError("internal", "position keys are exhausted") from None
            renumber(tx, queue, overlay)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Placement:
    """Where a job goes: its new key and the band (pinned, priority) it lands in.
    `renumbered` holds the keys a dry run would have given the queue first (empty otherwise)."""

    position: str
    pinned: bool
    priority: int
    renumbered: Mapping[str, str] = field(default_factory=dict)


def _sort_key(row: Mapping[str, object] | sqlite3.Row) -> tuple[str, str]:
    return (str(row["position"]), str(row["id"]))


def _fresh(used: set[str], key: str, lo: str | None) -> str:
    """`key`, or the nearest key below it (above `lo`) that no other live job of the queue
    uses, so positions stay unique across bands."""
    while key in used:
        key = key_between(lo, key)
    return key


def _row(tx: Tx, job_id: str) -> sqlite3.Row:
    row = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
    if row is None:
        raise JobsctlError("not_found", f"no job matches '{job_id}'")
    return row


def plan_move_relative(
    tx: Tx, job_id: str, other_id: str, *, before: bool, overlay: Overlay = None
) -> Placement | None:
    """Placement that puts `job_id` directly before (or after) `other_id`, adopting its band.
    `None` when the job already sits there (nothing is renumbered then). Both are queued or
    held jobs of one queue."""
    for attempt in range(2):
        queue = str(_row(tx, job_id)["queue"])
        live = _live_rows(tx, queue, overlay)
        job = next(r for r in live if r["id"] == job_id)
        other = next(r for r in live if r["id"] == other_id)
        anchor = _sort_key(other)
        band = [
            r
            for r in live
            if (r["pinned"], r["priority"]) == (other["pinned"], other["priority"])
            and r["id"] != job_id
        ]
        if before:
            near = max((r for r in band if _sort_key(r) < anchor), key=_sort_key, default=None)
        else:
            near = min((r for r in band if _sort_key(r) > anchor), key=_sort_key, default=None)
        lo_row, hi_row = (near, other) if before else (other, near)
        in_band = (job["pinned"], job["priority"]) == (other["pinned"], other["priority"])
        here = _sort_key(job)
        if (
            in_band
            and (lo_row is None or _sort_key(lo_row) < here)
            and (hi_row is None or here < _sort_key(hi_row))
        ):
            return None
        lo = None if lo_row is None else str(lo_row["position"])
        hi = None if hi_row is None else str(hi_row["position"])
        if lo is not None and hi is not None and lo >= hi:
            if attempt:
                raise JobsctlError("internal", "position keys could not be separated")
            renumber(tx, queue, overlay)  # equal keys: give every job its own
            continue
        used = {str(r["position"]) for r in live if r["id"] != job_id}
        try:
            key = _fresh(used, key_between(lo, hi), lo)
        except OverflowError:
            if attempt:
                raise JobsctlError("internal", "position keys are exhausted") from None
            renumber(tx, queue, overlay)
            continue
        if len(key) > MAX_KEY_LENGTH and not attempt:
            renumber(tx, queue, overlay)
            continue
        renumbered = dict(overlay) if overlay else {}
        return Placement(key, bool(other["pinned"]), int(str(other["priority"])), renumbered)
    raise AssertionError("unreachable")


def plan_move_edge(tx: Tx, job_id: str, *, top: bool, overlay: Overlay = None) -> Placement | None:
    """Placement at the top or bottom of the job's own band; `None` when it is already there."""
    job = _row(tx, job_id)
    queue = str(job["queue"])
    band = [
        r
        for r in _live_rows(tx, queue, overlay)
        if (r["pinned"], r["priority"]) == (job["pinned"], job["priority"]) and r["id"] != job_id
    ]
    here = _sort_key(job)
    if all((here < _sort_key(r)) if top else (here > _sort_key(r)) for r in band):
        return None
    key = (
        head_position(tx, queue, exclude=job_id, overlay=overlay)
        if top
        else tail_position(tx, queue, exclude=job_id, overlay=overlay)
    )
    renumbered = dict(overlay) if overlay else {}
    return Placement(key, bool(job["pinned"]), int(job["priority"]), renumbered)


def apply_placement(tx: Tx, job_id: str, placement: Placement) -> None:
    tx.execute(
        "UPDATE jobs SET position = ?, pinned = ?, priority = ? WHERE id = ?",
        [placement.position, int(placement.pinned), placement.priority, job_id],
    )


def long_keys(tx: Tx, queue: str) -> int:
    """How many live keys of `queue` exceed `MAX_KEY_LENGTH` (for tests and diagnostics)."""
    return sum(len(p) > MAX_KEY_LENGTH for p in _live_positions(tx, queue, None))


# -- rank and blocked_by ---------------------------------------------------------------------


def rank(tx: Tx, row: sqlite3.Row | Mapping[str, object]) -> int:
    """1-based place among the queued jobs of the job's queue: pinned first, then priority
    (highest first), then position, then id. Counts the other queued jobs only, so `row` may be
    a projection of a stored job."""
    found = tx.one(
        "SELECT count(*) AS n FROM jobs WHERE queue = :q AND status = 'queued' AND id != :id AND ("
        " pinned > :pin OR (pinned = :pin AND (priority > :pri OR (priority = :pri AND ("
        " position < :pos OR (position = :pos AND id < :id))))))",
        {
            "q": row["queue"],
            "pin": row["pinned"],
            "pri": row["priority"],
            "pos": row["position"],
            "id": row["id"],
        },
    )
    assert found is not None
    return int(found["n"]) + 1


def projected_rank(
    tx: Tx, row: sqlite3.Row | Mapping[str, object], renumbered: Mapping[str, str]
) -> int:
    """`rank` of `row` as if the queue had the keys in `renumbered` (a dry run's pending
    renumber); `row` already carries its own projected key."""
    others = tx.all(
        "SELECT id, position, pinned, priority FROM jobs WHERE queue = ? AND status = 'queued' "
        "AND id != ?",
        [row["queue"], row["id"]],
    )

    def order(r: sqlite3.Row | Mapping[str, object]) -> tuple[int, int, str, str]:
        return (-int(str(r["pinned"])), -int(str(r["priority"])), str(r["position"]), str(r["id"]))

    mine = order(row)
    return 1 + sum(
        order({**dict(r), "position": renumbered.get(str(r["id"]), r["position"])}) < mine
        for r in others
    )


def blocked_by(
    config: Config,
    *,
    status: str,
    queue: str,
    not_before: str | None,
    has_pass_env: bool,
    env_file_unavailable: bool,
    now: str,
) -> str | None:
    """Why a queued job cannot start now: the first that applies (Job object, `blocked_by`)."""
    if status != "queued":
        return None
    if not_before is not None and not_before > now:
        return "not_before"
    if has_pass_env and env_file_unavailable:
        return "env_file_unavailable"
    if config.queue(queue).cap == 0:
        return "queue_paused"
    if config.global_cap == 0:
        return "global_paused"
    return None


# -- snapshot, selection and capacity --------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A queued job as selection sees it."""

    id: str
    enqueued_at: str
    not_before: str | None
    needs_env_file: bool  # has `--pass-env` names


@dataclass(frozen=True)
class QueueState:
    """One queue: its settings, running count and queued jobs in rank order."""

    name: str
    cap: int | None
    priority: int
    running: int
    queued: tuple[Candidate, ...]


@dataclass(frozen=True)
class Snapshot:
    global_cap: int | None
    queues: tuple[QueueState, ...]  # every queue with a job or known by config, by name

    @property
    def running(self) -> int:
        return sum(q.running for q in self.queues)


def load_snapshot(tx: Tx, config: Config) -> Snapshot:
    """Read the counts and queued jobs of every queue in one transaction."""
    running = {
        str(r["queue"]): int(r["n"])
        for r in tx.all(
            "SELECT queue, count(*) AS n FROM jobs WHERE status IN ('starting', 'running') "
            "GROUP BY queue"
        )
    }
    queued: dict[str, list[Candidate]] = {}
    for r in tx.all(
        "SELECT id, queue, enqueued_at, not_before, pass_env FROM jobs WHERE status = 'queued' "
        "ORDER BY queue, pinned DESC, priority DESC, position, id"
    ):
        queued.setdefault(str(r["queue"]), []).append(
            Candidate(
                str(r["id"]),
                str(r["enqueued_at"]),
                r["not_before"],
                r["pass_env"] not in ("[]", ""),
            )
        )
    names = sorted(set(running) | set(queued) | set(config.queues))
    states = []
    for name in names:
        settings = config.queue(name)
        states.append(
            QueueState(
                name,
                settings.cap,
                settings.priority,
                running.get(name, 0),
                tuple(queued.get(name, ())),
            )
        )
    return Snapshot(config.global_cap, tuple(states))


def is_ready(candidate: Candidate, *, now: str, env_file_unavailable: bool) -> bool:
    """Not waiting for `not_before`, and not skipped for an unavailable `env_file`."""
    if candidate.not_before is not None and candidate.not_before > now:
        return False
    return not (candidate.needs_env_file and env_file_unavailable)


@dataclass(frozen=True)
class Selection:
    queue: str
    job_id: str


def select_next(snapshot: Snapshot, *, now: str, env_file_unavailable: bool) -> Selection | None:
    """The job the next claim takes, or `None` (interface.md, Selection).

    A queue is eligible with a ready job, running below its cap (`None`: unbounded) and the
    global running count below `global_cap`. The highest queue priority wins; ties go to the
    queue whose head job (its first ready job in rank order) was enqueued earliest, then to the
    lowest head job id. Pure: the caller adds the claim to the counts and calls again.
    """
    if snapshot.global_cap is not None and snapshot.running >= snapshot.global_cap:
        return None
    best: tuple[int, str, str, str] | None = None
    for queue in snapshot.queues:
        if queue.cap is not None and queue.running >= queue.cap:
            continue
        head = next(
            (
                c
                for c in queue.queued
                if is_ready(c, now=now, env_file_unavailable=env_file_unavailable)
            ),
            None,
        )
        if head is None:
            continue
        # Sort ascending: higher queue priority first, then earlier enqueue, then lower id.
        candidate = (-queue.priority, head.enqueued_at, head.id, queue.name)
        if best is None or candidate < best:
            best = candidate
    return None if best is None else Selection(best[3], best[2])


@dataclass(frozen=True)
class QueueCapacity:
    queue: str
    known: bool
    cap: int | None
    running: int
    queued: int
    free: int | None
    effective_free: int | None


@dataclass(frozen=True)
class GlobalCapacity:
    cap: int | None
    running: int
    queued: int
    free: int | None


def _free(cap: int | None, running: int, queued: int) -> int | None:
    return None if cap is None else max(0, cap - running - queued)


def _lowest(*values: int | None) -> int | None:
    bounded = [v for v in values if v is not None]
    return min(bounded) if bounded else None


def global_capacity(snapshot: Snapshot, *, now: str) -> GlobalCapacity:
    """The global entry: `queued` is the sum over queues of `min(ready, max(0, cap - running))`
    (all ready for a `None` cap); `ready` counts queued jobs whose `not_before` is not in the
    future, also those skipped for an unavailable `env_file`."""
    queued = 0
    for q in snapshot.queues:
        ready = sum(1 for c in q.queued if c.not_before is None or c.not_before <= now)
        queued += ready if q.cap is None else min(ready, max(0, q.cap - q.running))
    running = snapshot.running
    return GlobalCapacity(
        snapshot.global_cap, running, queued, _free(snapshot.global_cap, running, queued)
    )


def queue_capacity(
    snapshot: Snapshot, name: str, *, known: bool, default_cap: int | None, now: str
) -> QueueCapacity:
    """Capacity of one queue. A queue absent from the snapshot has no jobs and is reported with
    `default_cap` (`known` is the caller's: configured, seen or with jobs)."""
    state = next((q for q in snapshot.queues if q.name == name), None)
    cap = default_cap if state is None else state.cap
    running = 0 if state is None else state.running
    queued = 0 if state is None else len(state.queued)
    free = _free(cap, running, queued)
    return QueueCapacity(
        name,
        known,
        cap,
        running,
        queued,
        free,
        _lowest(free, global_capacity(snapshot, now=now).free),
    )


def known_queues(tx: Tx, config: Config) -> list[str]:
    """Configured, seen (`queues_seen`) or holding any job; sorted."""
    names: set[str] = set(config.queues)
    names.update(str(r["name"]) for r in tx.all("SELECT name FROM queues_seen"))
    names.update(str(r["queue"]) for r in tx.all("SELECT DISTINCT queue FROM jobs"))
    return sorted(names)


def counts_by_status(tx: Tx, queue: str, statuses: Iterable[str]) -> dict[str, int]:
    counts = dict.fromkeys(statuses, 0)
    for r in tx.all(
        "SELECT status, count(*) AS n FROM jobs WHERE queue = ? GROUP BY status", [queue]
    ):
        counts[str(r["status"])] = int(r["n"])
    return counts
