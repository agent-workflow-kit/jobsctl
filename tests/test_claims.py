"""The claim loop, spawn failures and orphaned claims (interface.md, Pass steps 3-4, Selection)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl.claims import (
    SPAWN_FAILURE_LIMIT,
    claim_jobs,
    fail_spawn,
    requeue_orphaned,
)
from jobsctl.config import Config
from jobsctl.store_db import Store
from tests.runner_helpers import add_queued, attempts_of, job_ids, job_row, put_starting
from tests.store_helpers import LATER, NOW

PASS = "boot:100:5"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state") as s:
        yield s


def config(**keys: object) -> Config:
    return Config.model_validate(keys)


def claim(store: Store, cfg: Config, *, limit: int | None = None, unavailable: bool = False):  # type: ignore[no-untyped-def]
    with store.write() as tx:
        found = claim_jobs(
            tx,
            cfg,
            now=LATER,
            claimed_by=PASS,
            env_file_unavailable=lambda: unavailable,
            limit=limit,
        )
    return found


def test_claims_up_to_the_queue_cap_and_records_the_attempt(store: Store) -> None:
    ids = job_ids(4)
    for i, job_id in enumerate(ids):
        add_queued(store, job_id, position=f"{i + 1:020d}")
    claims, records = claim(store, config(queues={"q": {"cap": 2}}))
    assert [c.job_id for c in claims] == ids[:2]
    assert len({c.claim_id for c in claims}) == 2
    for c in claims:
        row = job_row(store, c.job_id)
        assert (row["status"], row["attempt"], row["status_changed_at"]) == ("starting", 1, LATER)
        (attempt,) = attempts_of(store, c.job_id)
        assert attempt["claim_id"] == c.claim_id
        assert (attempt["status"], attempt["claimed_by"], attempt["claimed_at"]) == (
            "starting",
            PASS,
            LATER,
        )
        assert attempt["shim_identity"] is None and attempt["jobsctl_version"]
    assert {job_row(store, i)["status"] for i in ids[2:]} == {"queued"}
    assert [(r.event, r.fields["to"], r.fields["actor"]) for r in records] == [
        ("transition", "starting", "auto:jobsctl")
    ] * 2


def test_running_jobs_count_against_the_cap_and_a_second_claim_adds_nothing(store: Store) -> None:
    ids = job_ids(3)
    for i, job_id in enumerate(ids):
        add_queued(store, job_id, position=f"{i + 1:020d}")
    cfg = config(queues={"q": {"cap": 2}})
    first, _ = claim(store, cfg, limit=1)
    assert len(first) == 1
    second, _ = claim(store, cfg)
    assert len(second) == 1  # one slot was left
    third, _ = claim(store, cfg)
    assert third == []


def test_global_cap_counts_every_claim_across_queues(store: Store) -> None:
    for i, (queue, job_id) in enumerate(zip("abab", job_ids(4), strict=True)):
        add_queued(store, job_id, queue=queue, position=f"{i + 1:020d}")
    cfg = config(global_cap=3, default_cap=None)
    claims, _ = claim(store, cfg)
    assert len(claims) == 3


def test_queue_priority_then_enqueue_order(store: Store) -> None:
    low, high = job_ids(2)
    add_queued(store, low, queue="low", enqueued_at=NOW)
    add_queued(store, high, queue="high", enqueued_at=LATER)
    cfg = config(global_cap=1, queues={"high": {"priority": 5}})
    claims, _ = claim(store, cfg)
    assert [c.job_id for c in claims] == [high]


def test_limit_counts_claims_and_zero_claims_nothing(store: Store) -> None:
    for i, job_id in enumerate(job_ids(3)):
        add_queued(store, job_id, position=f"{i + 1:020d}")
    cfg = config(default_cap=None)
    assert len(claim(store, cfg, limit=2)[0]) == 2
    assert claim(store, cfg, limit=0)[0] == []


def test_a_not_ready_job_is_skipped_and_the_one_behind_it_starts(store: Store) -> None:
    first, second = job_ids(2)
    add_queued(store, first, position="1".zfill(20), not_before="2999-01-01T00:00:00.000Z")
    add_queued(store, second, position="2".zfill(20))
    claims, _ = claim(store, config())
    assert [c.job_id for c in claims] == [second]


def test_pass_env_jobs_wait_while_env_file_is_unavailable(store: Store) -> None:
    wants, plain = job_ids(2)
    add_queued(store, wants, position="1".zfill(20), pass_env='["TOKEN"]')
    add_queued(store, plain, position="2".zfill(20))
    cfg = config(default_cap=None)
    assert [c.job_id for c in claim(store, cfg, unavailable=True)[0]] == [plain]
    assert [c.job_id for c in claim(store, cfg, unavailable=False)[0]] == [wants]


def test_env_file_is_read_only_when_a_queued_job_needs_it(store: Store) -> None:
    add_queued(store, job_ids(1)[0])
    calls: list[int] = []
    with store.write() as tx:
        claim_jobs(
            tx, config(), now=LATER, claimed_by=PASS,
            env_file_unavailable=lambda: calls.append(1) or True, limit=None,
        )  # fmt: skip
    assert calls == []


def test_the_next_attempt_number_follows_the_last_kept_attempt(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, attempt=1)
    with store.write() as tx:
        tx.execute(
            "INSERT INTO attempts (job_id, n, claim_id, status, claimed_at, claimed_by, "
            "jobsctl_version) VALUES (?, 1, 'old', 'failed', ?, 'x', '0')",
            [job_id, NOW],
        )
    (claimed,), _ = claim(store, config())
    assert claimed.attempt == 2
    assert [a["n"] for a in attempts_of(store, job_id)] == [1, 2]


# -- spawn failures ----------------------------------------------------------------------------


def fail(store: Store, job_id: str, claim_id: str, cfg: Config | None = None):  # type: ignore[no-untyped-def]
    with store.write() as tx:
        return fail_spawn(tx, cfg or config(), job_id, claim_id, now=LATER, message="no python")


def test_a_spawn_failure_requeues_keeping_place_and_enqueue_time(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, position="5".zfill(20), enqueued_at=NOW)
    (c,), _ = claim(store, config())
    outcome, records = fail(store, job_id, c.claim_id)
    assert outcome is not None and not outcome.final
    row = job_row(store, job_id)
    assert (row["status"], row["attempt"], row["spawn_failures"]) == ("queued", 0, 1)
    assert (row["position"], row["enqueued_at"]) == ("5".zfill(20), NOW)
    assert attempts_of(store, job_id) == []
    assert [(r.event, r.level) for r in records] == [("shim_spawn_failed", "warn")]


def test_the_third_consecutive_failure_is_final_and_keeps_the_attempt(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    cfg = config(hooks={"post-fail": [{"cmd": "true"}]})
    records = []
    for n in range(1, SPAWN_FAILURE_LIMIT + 1):
        (c,), _ = claim(store, cfg)
        outcome, records = fail(store, job_id, c.claim_id, cfg)
        assert outcome is not None and outcome.final == (n == SPAWN_FAILURE_LIMIT)
    row = job_row(store, job_id)
    assert (row["status"], row["reason"], row["ended_at"], row["attempt"]) == (
        "failed",
        "spawn_failed",
        LATER,
        SPAWN_FAILURE_LIMIT - 2,  # two earlier attempts were deleted; the last one is kept
    )
    (kept,) = attempts_of(store, job_id)
    assert (kept["status"], kept["reason"], kept["exit"], kept["resolved_path"]) == (
        "failed",
        "spawn_failed",
        None,
        None,
    )
    assert [r.event for r in records] == ["shim_spawn_failed", "transition"]
    (event,) = store.query_all("SELECT hook, payload FROM hook_outbox")
    assert event["hook"] == "post-fail"
    payload = json.loads(event["payload"])
    assert payload["job"]["status"] == "failed" and payload["previous_status"] == "starting"


def test_a_job_canceled_meanwhile_keeps_its_attempt(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    (c,), _ = claim(store, config())
    with store.write() as tx:
        tx.execute(
            "UPDATE jobs SET status = 'canceled', reason = 'canceled' WHERE id = ?", [job_id]
        )
    outcome, records = fail(store, job_id, c.claim_id)
    assert outcome is None and records == []
    assert job_row(store, job_id)["status"] == "canceled"
    assert len(attempts_of(store, job_id)) == 1


def test_a_failure_for_another_claim_changes_nothing(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    claim(store, config())
    outcome, _ = fail(store, job_id, "someone-else")
    assert outcome is None
    assert job_row(store, job_id)["status"] == "starting"


def test_a_requeue_keeps_the_position_unless_a_newer_job_took_the_same_key(store: Store) -> None:
    first, newer = job_ids(2)
    add_queued(store, first, position="5".zfill(20))
    (c,), _ = claim(store, config())
    add_queued(store, newer, position="5".zfill(20))  # a tie, as add's tail key can produce
    fail(store, first, c.claim_id)
    assert job_row(store, newer)["position"] == "5".zfill(20)
    key = job_row(store, first)["position"]
    assert key > "5".zfill(20) and key.startswith("0000000000000000000")


# -- orphaned claims ---------------------------------------------------------------------------


def test_an_orphaned_claim_is_requeued_without_counting_a_spawn_failure(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, position="7".zfill(20), enqueued_at=NOW)
    (c,), _ = claim(store, config())
    with store.write() as tx:
        requeued, records = requeue_orphaned(tx, job_id, c.claim_id, now=LATER)
    assert requeued
    row = job_row(store, job_id)
    assert (row["status"], row["attempt"], row["spawn_failures"]) == ("queued", 0, 0)
    assert (row["position"], row["enqueued_at"]) == ("7".zfill(20), NOW)
    assert attempts_of(store, job_id) == []
    assert [(r.event, r.fields["finding"], r.fields["action"]) for r in records] == [
        ("reconcile", "claim_orphaned", "requeued")
    ]


def test_a_confirmed_claim_is_not_orphaned(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    put_starting(store, job_id, shim_confirmed_at=NOW)
    with store.write() as tx:
        requeued, records = requeue_orphaned(tx, job_id, "claim-1", now=LATER)
    assert not requeued and records == []
    assert job_row(store, job_id)["status"] == "starting"


def test_a_claim_that_moved_on_is_not_orphaned(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id)
    (c,), _ = claim(store, config())
    with store.write() as tx:
        tx.execute("UPDATE jobs SET status = 'running' WHERE id = ?", [job_id])
        assert requeue_orphaned(tx, job_id, c.claim_id, now=LATER)[0] is False
        assert requeue_orphaned(tx, job_id, "other", now=LATER)[0] is False


def test_a_claim_clears_not_before(store: Store) -> None:
    (job_id,) = job_ids(1)
    add_queued(store, job_id, not_before=NOW)
    claim(store, config())
    assert job_row(store, job_id)["not_before"] is None
