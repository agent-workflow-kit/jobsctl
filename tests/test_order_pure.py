"""Position keys, selection and capacity as pure functions (no database)."""

from __future__ import annotations

import random

import pytest

from jobsctl.order import (
    Candidate,
    QueueState,
    Snapshot,
    blocked_by,
    global_capacity,
    integer_key,
    key_between,
    queue_capacity,
    select_next,
)

NOW = "2026-01-01T00:00:10.000Z"
PAST = "2026-01-01T00:00:00.000Z"
FUTURE = "2026-01-01T01:00:00.000Z"


# -- position keys ---------------------------------------------------------------------------


def test_key_between_is_strictly_between_and_canonical() -> None:
    lo, hi = integer_key(5), integer_key(6)
    mid = key_between(lo, hi)
    assert lo < mid < hi
    assert mid == "00000000000000000005.5"


def test_key_between_unbounded_sides() -> None:
    assert key_between(None, None) == integer_key(10**15)
    assert key_between(None, "00000000000000000005.5") == integer_key(4)
    assert key_between("00000000000000000005.5", None) == integer_key(6)


def test_key_between_rejects_equal_or_reversed() -> None:
    with pytest.raises(ValueError):
        key_between(integer_key(5), integer_key(5))
    with pytest.raises(ValueError):
        key_between(integer_key(6), integer_key(5))


def test_key_below_zero_overflows() -> None:
    with pytest.raises(OverflowError):
        key_between(None, integer_key(0))


def test_many_insertions_after_the_same_neighbour_stay_ordered() -> None:
    lo, hi = integer_key(100), integer_key(101)
    keys = [lo, hi]
    for _ in range(500):  # always insert right after lo: the worst case for key growth
        keys.insert(1, key_between(keys[0], keys[1]))
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_many_insertions_before_the_same_neighbour_stay_ordered() -> None:
    keys = [integer_key(100), integer_key(101)]
    for _ in range(500):
        keys.insert(len(keys) - 1, key_between(keys[-2], keys[-1]))
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_random_insertions_keep_text_order_equal_to_list_order() -> None:
    rng = random.Random(7)
    keys = [integer_key(1000), integer_key(1001), integer_key(1002)]
    for _ in range(2000):
        i = rng.randrange(len(keys) + 1)
        lo = keys[i - 1] if i > 0 else None
        hi = keys[i] if i < len(keys) else None
        keys.insert(i, key_between(lo, hi))
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_lexical_order_of_mixed_fraction_lengths() -> None:
    # "x.25" < "x.5" < "x.75"; "x" < "x.1"; "x.9" < "x+1".
    ks = ["00000000000000000007", "00000000000000000007.1", "00000000000000000007.25",
          "00000000000000000007.5", "00000000000000000007.75", "00000000000000000007.9",
          "00000000000000000008"]  # fmt: skip
    assert ks == sorted(ks)
    for lo, hi in zip(ks, ks[1:], strict=False):
        assert lo < key_between(lo, hi) < hi


# -- blocked_by ------------------------------------------------------------------------------


def test_blocked_by_first_reason_wins() -> None:
    from jobsctl.config import Config

    cfg = Config(default_cap=0, global_cap=0)

    def reason(**over: object) -> str | None:
        base: dict[str, object] = {
            "status": "queued",
            "queue": "q",
            "not_before": None,
            "has_pass_env": False,
            "env_file_unavailable": False,
            "now": NOW,
        }
        return blocked_by(cfg, **{**base, **over})  # type: ignore[arg-type]

    assert reason(not_before=FUTURE, has_pass_env=True, env_file_unavailable=True) == "not_before"
    assert reason(has_pass_env=True, env_file_unavailable=True) == "env_file_unavailable"
    assert reason() == "queue_paused"
    assert reason(not_before=PAST) == "queue_paused"
    assert reason(env_file_unavailable=True) == "queue_paused"  # no pass_env names: not skipped
    assert reason(status="held") is None
    open_queue = Config(default_cap=1, global_cap=0)
    assert blocked_by(
        open_queue, status="queued", queue="q", not_before=None, has_pass_env=False,
        env_file_unavailable=False, now=NOW,
    ) == "global_paused"  # fmt: skip
    assert blocked_by(
        Config(default_cap=None), status="queued", queue="q", not_before=None,
        has_pass_env=False, env_file_unavailable=False, now=NOW,
    ) is None  # fmt: skip


# -- selection -------------------------------------------------------------------------------


def job(job_id: str, at: str = PAST, not_before: str | None = None, env: bool = False) -> Candidate:
    return Candidate(f"jb-{job_id:a<8}", at, not_before, env)


def queue(
    name: str, jobs: list[Candidate], cap: int | None = 1, priority: int = 0, running: int = 0
) -> QueueState:
    return QueueState(name, cap, priority, running, tuple(jobs))


def pick(
    queues: list[QueueState], global_cap: int | None = None, unavailable: bool = False
) -> tuple[str, str] | None:
    sel = select_next(
        Snapshot(global_cap, tuple(queues)), now=NOW, env_file_unavailable=unavailable
    )
    return None if sel is None else (sel.queue, sel.job_id)


def test_nothing_queued_selects_nothing() -> None:
    assert pick([]) is None
    assert pick([queue("a", [])]) is None


def test_head_of_the_only_queue() -> None:
    assert pick([queue("a", [job("one"), job("two")])]) == ("a", "jb-oneaaaaa")


def test_higher_queue_priority_wins_regardless_of_age() -> None:
    low = queue("a", [job("old", at="2025-01-01T00:00:00.000Z")], priority=0)
    high = queue("b", [job("new", at=PAST)], priority=5)
    assert pick([low, high]) == ("b", "jb-newaaaaa")


def test_priority_tie_goes_to_the_oldest_head() -> None:
    a = queue("a", [job("x", at="2026-01-01T00:00:02.000Z")])
    b = queue("b", [job("y", at="2026-01-01T00:00:01.000Z")])
    assert pick([a, b]) == ("b", "jb-yaaaaaaa")


def test_age_tie_goes_to_the_lowest_job_id_not_queue_name() -> None:
    a = queue("a", [job("zzz")])
    b = queue("b", [job("aaa")])
    assert pick([a, b]) == ("b", "jb-aaaaaaaa")


def test_head_is_the_first_ready_job_not_the_first_job() -> None:
    blocked = job("early", at="2025-01-01T00:00:00.000Z", not_before=FUTURE)
    other = queue("b", [job("mid", at="2025-06-01T00:00:00.000Z")])
    a = queue("a", [blocked, job("late", at="2026-01-01T00:00:05.000Z")])
    # a's head is "late" (enqueued after b's): the waiting job's age does not count.
    assert pick([a, other]) == ("b", "jb-midaaaaa")


def test_not_before_in_the_past_or_now_is_ready() -> None:
    assert pick([queue("a", [job("one", not_before=NOW)])]) == ("a", "jb-oneaaaaa")
    assert pick([queue("a", [job("one", not_before=FUTURE)])]) is None


def test_pass_env_jobs_are_skipped_when_env_file_is_unavailable() -> None:
    q = queue("a", [job("first", env=True), job("second")])
    assert pick([q], unavailable=True) == ("a", "jb-secondaa")
    assert pick([q], unavailable=False) == ("a", "jb-firstaaa")
    assert pick([queue("a", [job("first", env=True)])], unavailable=True) is None


def test_queue_at_cap_is_not_eligible_and_null_cap_is_unbounded() -> None:
    assert pick([queue("a", [job("one")], cap=1, running=1)]) is None
    assert pick([queue("a", [job("one")], cap=0)]) is None
    assert pick([queue("a", [job("one")], cap=None, running=50)]) == ("a", "jb-oneaaaaa")
    full = queue("a", [job("one")], cap=1, running=1)
    free = queue("b", [job("two", at="2027-01-01T00:00:00.000Z")], cap=1)
    assert pick([full, free]) == ("b", "jb-twoaaaaa")


def test_cap_lowered_below_running_blocks_without_error() -> None:
    assert pick([queue("a", [job("one")], cap=1, running=3)]) is None


def test_global_cap_counts_running_across_queues() -> None:
    a = queue("a", [job("one")], cap=None, running=1)
    b = queue("b", [job("two")], cap=None, running=1)
    assert pick([a, b], global_cap=2) is None
    assert pick([a, b], global_cap=3) is not None
    assert pick([a, b], global_cap=0) is None
    assert pick([a, b], global_cap=None) is not None


def test_selection_is_repeatable_with_claims_applied() -> None:
    a = queue("a", [job("a1"), job("a2")], cap=None)
    snap = Snapshot(2, (a,))
    claimed = []
    for _ in range(3):
        sel = select_next(snap, now=NOW, env_file_unavailable=False)
        if sel is None:
            break
        claimed.append(sel.job_id)
        q = snap.queues[0]
        rest = tuple(c for c in q.queued if c.id != sel.job_id)
        snap = Snapshot(2, (QueueState(q.name, q.cap, q.priority, q.running + 1, rest),))
    assert claimed == ["jb-a1aaaaaa", "jb-a2aaaaaa"]  # stopped by global_cap 2


# -- capacity --------------------------------------------------------------------------------


def snap(global_cap: int | None, *queues: QueueState) -> Snapshot:
    return Snapshot(global_cap, queues)


def cap_of(s: Snapshot, name: str, default_cap: int | None = 1, known: bool = True):  # noqa: ANN201
    return queue_capacity(s, name, known=known, default_cap=default_cap, now=NOW)


def test_free_counts_running_and_all_queued_including_waiting() -> None:
    s = snap(None, queue("a", [job("x"), job("y", not_before=FUTURE)], cap=5, running=1))
    c = cap_of(s, "a")
    assert (c.running, c.queued, c.free, c.effective_free) == (1, 2, 2, 2)


def test_free_never_negative() -> None:
    s = snap(None, queue("a", [job("x")], cap=1, running=3))
    assert cap_of(s, "a").free == 0


def test_null_cap_is_unbounded_and_effective_free_follows_global() -> None:
    s = snap(4, queue("a", [job("x")], cap=None, running=1))
    c = cap_of(s, "a")
    assert c.free is None
    assert c.effective_free == 2  # global: 4 - 1 running - 1 ready
    s = snap(None, queue("a", [job("x")], cap=None, running=1))
    assert cap_of(s, "a").effective_free is None


def test_effective_free_is_lowest_of_queue_and_global() -> None:
    q = queue("a", [], cap=5, running=0)
    other = queue("b", [], cap=None, running=3)
    s = snap(4, q, other)
    c = cap_of(s, "a")
    assert c.free == 5 and c.effective_free == 1  # global 4 - 3 running
    assert cap_of(snap(10, q, other), "a").effective_free == 5
    for g in (None, 0, 1, 7):
        c = cap_of(snap(g, q, other), "a")
        assert c.free is None or c.effective_free is None or c.effective_free <= c.free


def test_global_queued_is_bounded_by_each_queues_room() -> None:
    a = queue("a", [job("1"), job("2"), job("3")], cap=2, running=1)  # room 1 -> counts 1
    b = queue("b", [job("4"), job("5", not_before=FUTURE)], cap=None)  # ready 1
    c = queue("c", [job("6")], cap=0)  # no room -> 0
    g = global_capacity(snap(10, a, b, c), now=NOW)
    assert (g.running, g.queued, g.free) == (1, 2, 7)


def test_global_queued_counts_env_skipped_jobs_as_ready() -> None:
    a = queue("a", [job("1", env=True)], cap=None)
    assert global_capacity(snap(3, a), now=NOW).queued == 1


def test_unknown_queue_reports_default_cap_and_global_limit() -> None:
    s = snap(1, queue("busy", [], cap=None, running=1))
    c = cap_of(s, "nowhere", default_cap=3, known=False)
    assert (c.known, c.cap, c.running, c.queued, c.free) == (False, 3, 0, 0, 3)
    assert c.effective_free == 0  # the global cap is used up by another queue


def test_unknown_queue_with_null_default_and_no_global() -> None:
    c = cap_of(snap(None), "nowhere", default_cap=None, known=False)
    assert (c.cap, c.free, c.effective_free) == (None, None, None)


def test_global_paused_has_zero_effective_free() -> None:
    s = snap(0, queue("a", [], cap=None))
    assert cap_of(s, "a").effective_free == 0
    assert global_capacity(s, now=NOW).free == 0
