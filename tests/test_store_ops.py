"""Compare-and-set helpers, outbox, counters, leases (single process; races are in
test_store_processes.py)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl.store_db import Store
from jobsctl.store_ops import (
    OutboxEvent,
    counter_cas,
    counter_get,
    counter_incr,
    insert_job,
    lease_release,
    lease_renew,
    outbox_mark_delivered,
    outbox_pending,
    take_lease,
    transition_job,
    transition_job_by_claim,
    update_attempt_by_claim,
)
from tests.store_helpers import LATER, NOW, attempt_values, insert_row, job_values


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state" / "jobsctl") as s:
        yield s


def event(event_id: str = "ev-1", **over: object) -> OutboxEvent:
    fields: dict[str, object] = {
        "event_id": event_id,
        "hook": "post-start",
        "payload": {"labels": ["a=b"], "unicode": "é‮", "n": 1},
        "created_at": NOW,
    }
    fields.update(over)
    return OutboxEvent(**fields)  # type: ignore[arg-type]


def status_of(store: Store, job_id: str = "jb-aaaaaaaa") -> tuple[str, int]:
    row = store.query_one("SELECT status, attempt FROM jobs WHERE id = ?", [job_id])
    assert row is not None
    return row["status"], row["attempt"]


# -- insert_job / unique key --------------------------------------------------------------------


def test_insert_job_with_the_same_live_unique_key_inserts_one_row(store: Store) -> None:
    for status in ("queued", "starting", "running", "held"):
        with store.write() as tx:
            tx.execute("DELETE FROM jobs")
            assert insert_job(tx, job_values("jb-aaaaaaaa", unique_key="k", status=status))
            assert not insert_job(tx, job_values("jb-bbbbbbbb", unique_key="k", status="queued"))
            assert tx.one("SELECT count(*) AS c FROM jobs")["c"] == 1  # type: ignore[index]


def test_a_terminal_job_does_not_block_its_unique_key(store: Store) -> None:
    for status in ("done", "failed", "canceled"):
        with store.write() as tx:
            tx.execute("DELETE FROM jobs")
            assert insert_job(tx, job_values("jb-aaaaaaaa", unique_key="k", status=status))
            assert insert_job(tx, job_values("jb-bbbbbbbb", unique_key="k", status="queued"))
            assert insert_job(tx, job_values("jb-cccccccc", unique_key="k", status=status))


def test_null_unique_keys_never_conflict_and_keys_are_case_sensitive(store: Store) -> None:
    with store.write() as tx:
        assert insert_job(tx, job_values("jb-aaaaaaaa"))
        assert insert_job(tx, job_values("jb-bbbbbbbb"))
        assert insert_job(tx, job_values("jb-cccccccc", unique_key="K"))
        assert insert_job(tx, job_values("jb-dddddddd", unique_key="k"))
        assert insert_job(tx, job_values("jb-eeeeeeee", unique_key="ḱ"))  # no normalization


def test_unique_keys_hold_across_queues(store: Store) -> None:
    with store.write() as tx:
        assert insert_job(tx, job_values("jb-aaaaaaaa", queue="one", unique_key="k"))
        assert not insert_job(tx, job_values("jb-bbbbbbbb", queue="two", unique_key="k"))


def test_insert_job_only_absorbs_the_unique_key_conflict(store: Store) -> None:
    with store.write() as tx:
        assert insert_job(tx, job_values("jb-aaaaaaaa"))
        with pytest.raises(sqlite3.IntegrityError):  # duplicate id is not absorbed
            insert_job(tx, job_values("jb-aaaaaaaa"))
    with store.write() as tx:
        with pytest.raises(sqlite3.IntegrityError):  # NOT NULL still enforced
            insert_job(tx, {"id": "jb-bbbbbbbb", "status": "queued"})
        with pytest.raises(sqlite3.IntegrityError):
            insert_job(tx, job_values("jb-cccccccc", pinned=2))


@pytest.mark.parametrize(
    "values",
    [{}, {"id": "x", "status": "queued", "nope": 1}, {"status": "weird"}, {"id; DROP": 1}],
)
def test_insert_job_rejects_unknown_columns_and_statuses(
    store: Store, values: dict[str, object]
) -> None:
    with store.write() as tx, pytest.raises(ValueError):
        insert_job(tx, values)


def test_injection_shaped_values_are_data(store: Store) -> None:
    nasty = "x'); DROP TABLE jobs; --"
    with store.write() as tx:
        assert insert_job(tx, job_values("jb-aaaaaaaa", unique_key=nasty, note=nasty))
    row = store.query_one("SELECT unique_key, note FROM jobs")
    assert row is not None and (row["unique_key"], row["note"]) == (nasty, nasty)


# -- transitions ----------------------------


def seed(store: Store, **over: Any) -> None:
    with store.write() as tx:
        insert_row(tx, "jobs", job_values(**over))


def test_transition_wins_once_and_applies_updates_and_events(store: Store) -> None:
    seed(store)
    with store.write() as tx:
        assert transition_job(
            tx,
            "jb-aaaaaaaa",
            expect_status="queued",
            expect_attempt=0,
            to_status="starting",
            updates={"attempt": 1, "status_changed_at": LATER},
            events=[event()],
        )
    assert status_of(store) == ("starting", 1)
    row = store.query_one("SELECT status_changed_at FROM jobs")
    assert row is not None and row["status_changed_at"] == LATER
    with store.write() as tx:
        pending = outbox_pending(tx, "jb-aaaaaaaa")
    assert [r["event_id"] for r in pending] == ["ev-1"]
    assert json.loads(pending[0]["payload"])["unicode"] == "é‮"


@pytest.mark.parametrize(
    ("expect_status", "expect_attempt"),
    [("running", 0), ("queued", 1), ("held", 5)],
)
def test_a_stale_expectation_changes_nothing_and_writes_no_events(
    store: Store, expect_status: str, expect_attempt: int
) -> None:
    seed(store)
    with store.write() as tx:
        assert not transition_job(
            tx,
            "jb-aaaaaaaa",
            expect_status=expect_status,
            expect_attempt=expect_attempt,
            to_status="done",
            updates={"note": "x"},
            events=[event()],
        )
        assert outbox_pending(tx, "jb-aaaaaaaa") == []
    assert status_of(store) == ("queued", 0)


def test_transition_of_an_unknown_job_is_a_lost_cas(store: Store) -> None:
    with store.write() as tx:
        assert not transition_job(
            tx, "jb-zzzzzzzz", expect_status="queued", expect_attempt=0, to_status="held"
        )


@pytest.mark.parametrize(
    "updates",
    [{"id": "jb-other"}, {"status": "done"}, {"nope": 1}, {"note = 'x', status": "done"},
     {"note\"": 1}],
)  # fmt: skip
def test_transition_rejects_forbidden_and_unknown_columns(
    store: Store, updates: dict[str, object]
) -> None:
    seed(store)
    with store.write() as tx, pytest.raises(ValueError):
        transition_job(
            tx,
            "jb-aaaaaaaa",
            expect_status="queued",
            expect_attempt=0,
            to_status="held",
            updates=updates,
        )


def test_transition_rejects_unknown_statuses(store: Store) -> None:
    seed(store)
    with store.write() as tx:
        with pytest.raises(ValueError):
            transition_job(
                tx, "jb-aaaaaaaa", expect_status="queued", expect_attempt=0, to_status="bogus"
            )
        with pytest.raises(ValueError):
            transition_job(tx, "jb-aaaaaaaa", expect_status="x", expect_attempt=0, to_status="held")


def test_a_failing_event_insert_rolls_back_the_transition(store: Store) -> None:
    seed(store)
    with pytest.raises(sqlite3.IntegrityError), store.write() as tx:
        transition_job(
            tx,
            "jb-aaaaaaaa",
            expect_status="queued",
            expect_attempt=0,
            to_status="held",
            events=[event("dup"), event("dup")],
        )
    assert status_of(store) == ("queued", 0)
    assert store.query_one("SELECT count(*) AS c FROM hook_outbox")["c"] == 0  # type: ignore[index]


def test_a_transition_into_a_live_unique_key_conflict_raises(store: Store) -> None:
    seed(store, id="jb-aaaaaaaa", unique_key="k", status="done")
    seed(store, id="jb-bbbbbbbb", unique_key="k", status="queued")
    with pytest.raises(sqlite3.IntegrityError), store.write() as tx:
        transition_job(
            tx, "jb-aaaaaaaa", expect_status="done", expect_attempt=0, to_status="queued"
        )


def test_claim_based_transition_and_attempt_update(store: Store) -> None:
    seed(store, status="starting", attempt=1)
    with store.write() as tx:
        insert_row(tx, "attempts", attempt_values("jb-aaaaaaaa", 1, "claim-1"))
    with store.write() as tx:
        assert not transition_job_by_claim(
            tx, "jb-aaaaaaaa", claim_id="claim-OLD", expect_status="starting", to_status="running"
        )
        assert not update_attempt_by_claim(tx, "jb-aaaaaaaa", "claim-OLD", {"status": "running"})
        assert update_attempt_by_claim(
            tx, "jb-aaaaaaaa", "claim-1", {"status": "running", "running_at": LATER}
        )
        assert transition_job_by_claim(
            tx,
            "jb-aaaaaaaa",
            claim_id="claim-1",
            expect_status="starting",
            to_status="running",
            events=[event()],
        )
        assert not transition_job_by_claim(  # status already moved on
            tx, "jb-aaaaaaaa", claim_id="claim-1", expect_status="starting", to_status="running"
        )
    assert status_of(store) == ("running", 1)
    row = store.query_one("SELECT status, running_at FROM attempts")
    assert row is not None and (row["status"], row["running_at"]) == ("running", LATER)


def test_a_stale_shim_loses_against_a_new_claim(store: Store) -> None:
    """The attempt was requeued and claimed again: the old claim_id matches nothing."""
    seed(store, status="starting", attempt=1)
    with store.write() as tx:
        insert_row(tx, "attempts", attempt_values("jb-aaaaaaaa", 1, "claim-1"))
        tx.execute("DELETE FROM attempts")  # claim_orphaned: attempt deleted, job requeued
        tx.execute("UPDATE jobs SET status = 'queued', attempt = 0")
        tx.execute("UPDATE jobs SET status = 'starting', attempt = 1")
        insert_row(tx, "attempts", attempt_values("jb-aaaaaaaa", 1, "claim-2"))
    with store.write() as tx:
        assert not update_attempt_by_claim(tx, "jb-aaaaaaaa", "claim-1", {"status": "running"})
        assert not transition_job_by_claim(
            tx, "jb-aaaaaaaa", claim_id="claim-1", expect_status="starting", to_status="running"
        )
    assert status_of(store) == ("starting", 1)


def test_attempt_update_only_touches_the_current_attempt_and_rejects_keys(
    store: Store,
) -> None:
    seed(store, status="queued", attempt=2)
    with store.write() as tx:
        insert_row(tx, "attempts", attempt_values("jb-aaaaaaaa", 1, "claim-1", status="failed"))
        insert_row(tx, "attempts", attempt_values("jb-aaaaaaaa", 2, "claim-1"))  # same claim id
        assert update_attempt_by_claim(tx, "jb-aaaaaaaa", "claim-1", {"note": "x"})
        for bad in ({"claim_id": "evil"}, {"n": 9}, {"job_id": "x"}, {"zzz": 1}):
            with pytest.raises(ValueError):
                update_attempt_by_claim(tx, "jb-aaaaaaaa", "claim-1", bad)
        with pytest.raises(ValueError):
            update_attempt_by_claim(tx, "jb-aaaaaaaa", "claim-1", {})
    rows = store.query_all("SELECT n, note FROM attempts ORDER BY n")
    assert [(r["n"], r["note"]) for r in rows] == [(1, None), (2, "x")]


# -- outbox ----------------------------


def test_outbox_order_async_filter_and_delivery(store: Store) -> None:
    seed(store)
    with store.write() as tx:
        from jobsctl.store_ops import outbox_insert

        s1 = outbox_insert(tx, "jb-aaaaaaaa", event("e1"))
        s2 = outbox_insert(tx, "jb-aaaaaaaa", event("e2", is_async=True))
        s3 = outbox_insert(tx, "jb-aaaaaaaa", event("e3"))
        assert s1 < s2 < s3
        assert [r["event_id"] for r in outbox_pending(tx, "jb-aaaaaaaa")] == ["e1", "e2", "e3"]
        assert [r["event_id"] for r in outbox_pending(tx, "jb-aaaaaaaa", is_async=True)] == ["e2"]
        assert [r["event_id"] for r in outbox_pending(tx, "jb-aaaaaaaa", is_async=False)] == [
            "e1",
            "e3",
        ]
        assert outbox_mark_delivered(tx, "e1", LATER)
        assert not outbox_mark_delivered(tx, "e1", LATER)
        assert not outbox_mark_delivered(tx, "nope", LATER)
        assert [r["event_id"] for r in outbox_pending(tx, "jb-aaaaaaaa")] == ["e2", "e3"]


def test_outbox_rows_follow_their_job_on_delete(store: Store) -> None:
    seed(store)
    with store.write() as tx:
        from jobsctl.store_ops import outbox_insert

        outbox_insert(tx, "jb-aaaaaaaa", event())
        insert_row(tx, "job_labels", {"job_id": "jb-aaaaaaaa", "key": "k", "value": "v"})
        insert_row(tx, "attempts", attempt_values("jb-aaaaaaaa", 1, "c"))
        tx.execute("DELETE FROM jobs")
        for table in ("hook_outbox", "job_labels", "attempts"):
            assert tx.one(f"SELECT count(*) AS c FROM {table}")["c"] == 0  # type: ignore[index]


def test_outbox_rejects_an_unknown_job(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError), store.write() as tx:
        from jobsctl.store_ops import outbox_insert

        outbox_insert(tx, "jb-zzzzzzzz", event())


# -- counters ----------------------------


def test_counters_start_at_zero_and_grow(store: Store) -> None:
    with store.write() as tx:
        assert counter_get(tx, "pass_requested") == 0
        assert [counter_incr(tx, "seq:log") for _ in range(3)] == [1, 2, 3]
        assert counter_incr(tx, "pass_requested", 5) == 5
        assert counter_get(tx, "pass_requested") == 5
        for bad in (0, -1):
            with pytest.raises(ValueError):
                counter_incr(tx, "x", bad)


def test_counter_increment_rolls_back_without_a_gap(store: Store) -> None:
    with store.write() as tx:
        assert counter_incr(tx, "seq:log") == 1
    with pytest.raises(RuntimeError), store.write() as tx:
        counter_incr(tx, "seq:log")
        raise RuntimeError
    with store.write() as tx:
        assert counter_incr(tx, "seq:log") == 2


def test_counter_cas(store: Store) -> None:
    with store.write() as tx:
        assert counter_cas(tx, "pass_requested", 0, 0)  # absent reads 0
        counter_incr(tx, "pass_requested", 3)
        assert not counter_cas(tx, "pass_requested", 2, 9)
        assert counter_get(tx, "pass_requested") == 3
        assert counter_cas(tx, "pass_requested", 3, 0)
        assert counter_get(tx, "pass_requested") == 0
        assert counter_cas(tx, "fresh", 0, 4)
        assert not counter_cas(tx, "other", 1, 2)
        assert counter_get(tx, "other") == 0


# -- leases ----------------------------

T0, T1, T2 = "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:30.000Z", "2026-01-01T00:01:00.000Z"
T3 = "2026-01-01T00:02:00.000Z"


def lease_row(store: Store, name: str = "runner") -> tuple[str, str] | None:
    row = store.query_one("SELECT holder_identity, expires_at FROM leases WHERE name = ?", [name])
    return None if row is None else (row["holder_identity"], row["expires_at"])


def test_take_a_free_lease_then_renew_and_release(store: Store) -> None:
    never = lambda _h: pytest.fail("no holder to check")  # noqa: E731
    assert take_lease(store, "runner", "A", now=T0, expires_at=T2, is_alive=never)
    assert lease_row(store) == ("A", T2)
    with store.write() as tx:
        assert lease_renew(tx, "runner", "A", T3)
        assert not lease_renew(tx, "runner", "B", T3)
    assert lease_row(store) == ("A", T3)
    with store.write() as tx:
        assert not lease_release(tx, "runner", "B")
        assert lease_release(tx, "runner", "A")
        assert not lease_release(tx, "runner", "A")
    assert lease_row(store) is None


def test_a_live_holder_is_never_displaced_even_after_expiry(store: Store) -> None:
    take_lease(store, "runner", "A", now=T0, expires_at=T1, is_alive=lambda _h: False)
    assert not take_lease(store, "runner", "B", now=T3, expires_at=T3, is_alive=lambda _h: True)
    assert lease_row(store) == ("A", T1)


def test_a_dead_holder_is_displaced_even_before_expiry(store: Store) -> None:
    take_lease(store, "runner", "A", now=T0, expires_at=T2, is_alive=lambda _h: False)
    assert take_lease(store, "runner", "B", now=T1, expires_at=T3, is_alive=lambda _h: False)
    assert lease_row(store) == ("B", T3)


def test_an_inconclusive_check_frees_the_lease_only_on_expiry(store: Store) -> None:
    take_lease(store, "runner", "A", now=T0, expires_at=T2, is_alive=lambda _h: None)
    assert not take_lease(store, "runner", "B", now=T1, expires_at=T3, is_alive=lambda _h: None)
    assert take_lease(store, "runner", "B", now=T2, expires_at=T3, is_alive=lambda _h: None)
    assert lease_row(store) == ("B", T3)


def test_the_holder_taking_again_refreshes_without_a_liveness_check(store: Store) -> None:
    take_lease(store, "deliver", "A", now=T0, expires_at=T1, is_alive=lambda _h: False)
    assert take_lease(
        store, "deliver", "A", now=T1, expires_at=T3, is_alive=lambda _h: pytest.fail("no check")
    )
    assert lease_row(store, "deliver") == ("A", T3)


def test_the_liveness_check_gets_the_observed_holder(store: Store) -> None:
    seen: list[str] = []
    take_lease(store, "job:jb-aaaaaaaa", "A", now=T0, expires_at=T2, is_alive=lambda _h: False)

    def alive(holder: str) -> bool:
        seen.append(holder)
        return False

    assert take_lease(store, "job:jb-aaaaaaaa", "B", now=T1, expires_at=T3, is_alive=alive)
    assert seen == ["A"]


def test_a_takeover_that_loses_the_race_does_not_steal(store: Store) -> None:
    """The row changes between the observation and the compare-and-set: the take loses."""
    take_lease(store, "runner", "A", now=T0, expires_at=T2, is_alive=lambda _h: False)

    def alive_then_racer_wins(_holder: str) -> bool:
        other = Store.open(store.paths.root)
        try:
            assert take_lease(
                other, "runner", "C", now=T1, expires_at=T3, is_alive=lambda _h: False
            )
        finally:
            other.close()
        return False  # B saw A dead, but C took over before B's compare-and-set

    assert not take_lease(
        store, "runner", "B", now=T1, expires_at=T3, is_alive=alive_then_racer_wins
    )
    assert lease_row(store) == ("C", T3)


@pytest.mark.parametrize("name", ["", "runner2", "job:", "job:A", "job:x y", "x'; --", "Runner"])
def test_lease_names_are_validated(store: Store, name: str) -> None:
    with pytest.raises(ValueError):
        take_lease(store, name, "A", now=T0, expires_at=T1, is_alive=lambda _h: False)
    with store.write() as tx:
        with pytest.raises(ValueError):
            lease_renew(tx, name, "A", T1)
        with pytest.raises(ValueError):
            lease_release(tx, name, "A")
