"""retry and resolve racing each other, cancel and hold, in real processes (same harness as
test_manual_races: each process leads its own session and is killed by that exact group)."""

from __future__ import annotations

import pytest

from tests.manual_helpers import (
    add_job,
    attempt_row,
    audit_records,
    log_records,
    make_done,
    make_held,
    row,
)
from tests.test_manual_races import Pool, changed, cli
from tests.test_manual_races import config as config  # noqa: F401  (fixture)
from tests.test_manual_races import pool as pool  # noqa: F401  (fixture)


def chain(job_id: str, start: str) -> list[tuple[str, str]]:
    """The job's logged transitions in the order they chain from `start`. Records are appended
    after each commit by different processes, so file order can differ from commit order: the
    chain is rebuilt by following `from` -> `to`."""
    pending = [(r["from"], r["to"]) for r in log_records("transition") if r["job_id"] == job_id]
    last, found = start, []
    while pending:
        step = next(p for p in pending if p[0] == last)  # StopIteration: a broken chain
        pending.remove(step)
        found.append(step)
        last = step[1]
    assert last == row(job_id)["status"]
    return found


@pytest.mark.parametrize("round_", range(4))
def test_many_retries_commit_exactly_once(
    capsys: pytest.CaptureFixture[str],
    pool: Pool,
    config: str,
    round_: int,  # noqa: F811
) -> None:
    job_id = add_job(capsys, config=config)
    make_done(job_id, "failed")
    workers = [cli(pool, config, "retry", job_id) for _ in range(5)]
    workers += [cli(pool, config, "retry", job_id, "--attempt", "1") for _ in range(5)]
    pool.go()
    results = [w.result() for w in workers]
    # a plain retry that arrives after the first one finds a queued job: not_terminal (20)
    assert all(r["exit"] in (0, 20) for r in results)
    winners = [r for r in results if r["exit"] == 0 and changed(r)]
    assert len(winners) == 1
    assert all(r["exit"] == 0 for r in results[5:])  # --attempt makes it idempotent
    assert chain(job_id, "failed") == [("failed", "queued")]
    assert len(audit_records()) == 1 and row(job_id)["retried_from_attempt"] == 1


def test_attempt_retries_are_all_successful_and_idempotent(
    capsys: pytest.CaptureFixture[str],
    pool: Pool,
    config: str,  # noqa: F811
) -> None:
    job_id = add_job(capsys, config=config)
    make_done(job_id, "failed")
    workers = [cli(pool, config, "retry", job_id, "--attempt", "1") for _ in range(6)]
    pool.go()
    results = [w.result() for w in workers]
    assert [r["exit"] for r in results] == [0] * 6
    assert sum(changed(r) for r in results) == 1


@pytest.mark.parametrize("round_", range(4))
def test_retry_racing_resolve_on_an_exit_13_hold(
    capsys: pytest.CaptureFixture[str],
    pool: Pool,
    config: str,
    round_: int,  # noqa: F811
) -> None:
    job_id = add_job(capsys, config=config)
    make_held(job_id, "exit_13")
    retrier = cli(pool, config, "retry", job_id)
    resolver = cli(pool, config, "resolve", job_id, "--as", "failed")
    pool.go()
    retry_result, resolve_result = retrier.result(), resolver.result()
    assert retry_result["exit"] == 0 and changed(retry_result)  # a held or resolved job retries
    steps = chain(job_id, "held")
    if resolve_result["exit"] == 0:  # resolve first: held -> failed -> queued
        assert steps == [("held", "failed"), ("failed", "queued")]
        assert attempt_row(job_id)["status"] == "failed"
    else:  # retry first: the job is queued, so resolve is refused (not_held)
        assert resolve_result["exit"] == 20 and steps == [("held", "queued")]
        assert attempt_row(job_id)["status"] == "held"


@pytest.mark.parametrize("round_", range(4))
def test_retry_racing_cancel_of_an_exit_13_hold(
    capsys: pytest.CaptureFixture[str],
    pool: Pool,
    config: str,
    round_: int,  # noqa: F811
) -> None:
    job_id = add_job(capsys, config=config)
    make_held(job_id, "exit_13")
    retrier = cli(pool, config, "retry", job_id)
    canceler = cli(pool, config, "cancel", job_id)
    pool.go()
    retry_result, cancel_result = retrier.result(), canceler.result()
    assert retry_result["exit"] == 0 and cancel_result["exit"] == 0
    steps = chain(job_id, "held")
    assert steps in (
        [("held", "canceled"), ("canceled", "queued")],  # the cancel ended the held attempt first
        [("held", "queued"), ("queued", "canceled")],  # the retry came first
    )
    # the cancel ended the held attempt in the first order; in the second, the cancel of the
    # queued job whose attempt still read `held` ends it too
    assert attempt_row(job_id)["status"] == "canceled"


@pytest.mark.parametrize("round_", range(3))
def test_retry_racing_hold_leaves_a_consistent_job(
    capsys: pytest.CaptureFixture[str],
    pool: Pool,
    config: str,
    round_: int,  # noqa: F811
) -> None:
    job_id = add_job(capsys, config=config)
    make_done(job_id, "failed")
    retrier = cli(pool, config, "retry", job_id)
    holder = cli(pool, config, "hold", job_id)
    pool.go()
    retry_result, hold_result = retrier.result(), holder.result()
    assert retry_result["exit"] == 0 and changed(retry_result)
    # hold of a terminal job is a no-op; after the retry it holds the queued job
    assert hold_result["exit"] == 0
    assert chain(job_id, "failed") in (
        [("failed", "queued")],
        [("failed", "queued"), ("queued", "held")],
    )
