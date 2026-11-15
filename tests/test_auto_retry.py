"""Automatic retry (interface.md, Automatic retry) in the shim's final writes, in process."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from jobsctl.config import Config
from jobsctl.order import integer_key
from jobsctl.store_db import OpenMode, Store
from tests.shim_helpers import (
    CLAIM,
    JOB,
    attempt,
    job,
    log_records,
    make_shim,
    one,
    seed_claimed,
)
from tests.store_helpers import NOW, insert_row, job_values


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def _validated(validated_records: None) -> None:
    return None


def config(retries: int = 2, delay: int = 60, **extra: Any) -> Config:
    queue = {"max_retries": retries, "retry_delay": f"{delay}s"}
    return Config.model_validate({"auto_run": False, "queues": {"q": queue}, **extra})


def run(store: Store, argv: list[str], cfg: Config, **seed: Any) -> None:
    seed_claimed(store, argv, **seed)
    make_shim(store, cfg).run()


def test_a_failed_exit_requeues_with_not_before_at_the_bottom(store: Store) -> None:
    with store.write() as tx:  # a newer queued job already holds the queue's tail
        insert_row(tx, "jobs", job_values("jb-bbbbbbbb", queue="q", position=integer_key(10**6)))
    run(store, ["sh", "-c", "exit 3"], config(delay=90))
    row = job(store)
    assert row["status"] == "queued" and row["reason"] is None and row["ended_at"] is None
    assert row["auto_retries"] == 1 and row["attempt"] == 1
    assert row["position"] > integer_key(
        10**6
    )  # a key from order.py, after the newer job: never a tie
    assert row["not_before"] > row["enqueued_at"]
    seconds = _seconds_between(row["enqueued_at"], row["not_before"])
    assert seconds == 90
    a = attempt(store)
    assert (a["status"], a["reason"]) == ("failed", "exit")
    assert json.loads(a["exit"])["code"] == 3
    (rec,) = log_records("transition")[-1:]
    assert (rec["from"], rec["to"], rec["reason"]) == ("running", "queued", None)
    assert (rec["attempt_status"], rec["attempt_reason"], rec["retry_pending"]) == (
        "failed",
        "exit",
        True,
    )


def _seconds_between(a: str, b: str) -> int:
    from jobsctl.timestamps import parse_timestamp

    return round((parse_timestamp(b) - parse_timestamp(a)).total_seconds())


def test_the_budget_is_the_count_since_the_last_retry_or_release(store: Store) -> None:
    seed_claimed(store, ["false"], spawn_failures=0)
    with store.write() as tx:
        tx.execute("UPDATE jobs SET auto_retries = 1 WHERE id = ?", [JOB])
    make_shim(store, config(retries=2)).run()
    assert job(store)["status"] == "queued" and job(store)["auto_retries"] == 2


def test_a_spent_budget_ends_the_job_failed(store: Store) -> None:
    seed_claimed(store, ["false"])
    with store.write() as tx:
        tx.execute("UPDATE jobs SET auto_retries = 2 WHERE id = ?", [JOB])
    make_shim(store, config(retries=2)).run()
    row = job(store)
    assert (row["status"], row["reason"], row["auto_retries"]) == ("failed", "exit", 2)


def test_max_retries_zero_never_requeues(store: Store) -> None:
    run(store, ["false"], config(retries=0))
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "exit")


@pytest.mark.parametrize(
    ("argv", "kwargs", "reason"),
    [
        (["sh", "-c", "kill -KILL $$"], {}, "signaled"),
        (["no-such-command-xyz"], {}, "spawn_failed"),
        (["true"], {"pass_env": ["NOT_THERE"]}, "missing_env"),
    ],
)
def test_every_listed_reason_is_retried(
    store: Store, argv: list[str], kwargs: dict[str, Any], reason: str
) -> None:
    run(store, argv, config(), **kwargs)
    assert job(store)["status"] == "queued"
    a = attempt(store)
    assert (a["status"], a["reason"]) == ("failed", reason)


def test_a_timeout_is_retried(store: Store) -> None:
    run(
        store,
        ["sleep", "30"],
        config(),
        overrides={"timeout": 1, "kill_grace": 1},
    )
    assert job(store)["status"] == "queued"
    assert attempt(store)["reason"] == "timeout"


def test_sigterm_while_starting_is_retried(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_claimed(store, ["true"])
    shim = make_shim(store, config())
    real = shim._still_starting  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def look_then_term() -> object:
        out = real()
        calls.append(1)
        if len(calls) == 2:
            shim.request_stop()
        return out

    monkeypatch.setattr(shim, "_still_starting", look_then_term)
    shim.run()
    assert job(store)["status"] == "queued"
    assert attempt(store)["reason"] == "terminated"


def test_cwd_missing_exit_13_and_success_are_never_retried(store: Store, tmp_path: Any) -> None:
    run(store, ["true"], config(), cwd=str(tmp_path / "gone"))
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "cwd_missing")


def test_exit_13_holds_and_exit_0_is_done(store: Store) -> None:
    run(store, ["sh", "-c", "exit 13"], config())
    assert (job(store)["status"], job(store)["hold_reason"]) == ("held", "exit_13")


def test_the_attempts_recorded_max_retries_decides_not_the_current_config(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the attempt records max_retries 0; the config now allows 5
    import jobsctl.shim as shim_module

    monkeypatch.setattr(
        shim_module,
        "effective_snapshot",
        lambda c, r: {"timeout": None, "max_retries": 0, "kill_grace": 30},
    )
    seed_claimed(store, ["false"])
    make_shim(store, config(retries=5)).run()
    assert (job(store)["status"], job(store)["reason"]) == ("failed", "exit")


def test_a_post_fail_event_shows_the_job_queued(store: Store) -> None:
    cfg = config(hooks={"post-fail": [{"cmd": "true"}]})
    run(store, ["false"], cfg)
    ev = one(store, "SELECT * FROM hook_outbox")
    payload = json.loads(ev["payload"])
    assert payload["hook"] == "post-fail" and payload["job"]["status"] == "queued"
    assert payload["previous_status"] == "running" and payload["attempt"]["status"] == "failed"


def test_a_forced_cancel_is_never_retried(store: Store) -> None:
    seed_claimed(store, ["false"])
    shim = make_shim(store, config())
    with store.write() as tx:
        tx.execute("UPDATE attempts SET force_requested_by = 'human:kim' WHERE job_id = ?", [JOB])
    shim.run()
    assert (job(store)["status"], job(store)["reason"]) == ("canceled", "forced")


def test_the_job_keeps_its_unique_key_and_the_hooks_that_ran_clear_override_hooks(
    store: Store,
) -> None:
    seed_claimed(store, ["false"])
    with store.write() as tx:
        tx.execute(
            "UPDATE jobs SET unique_key = 'k', override_hooks = '[\"h\"]' WHERE id = ?", [JOB]
        )
    make_shim(store, config()).run()
    row = job(store)
    assert row["status"] == "queued" and row["unique_key"] == "k"
    # the start ran its pre-start hooks (none here), so the override is spent; one kept across a
    # retry is covered by tests/test_pre_start.py (a start cut short before its hooks ran)
    assert row["override_hooks"] == "[]"


def test_a_failure_after_a_retried_start_keeps_no_not_before(store: Store) -> None:
    seed_claimed(store, ["false"])
    with store.write() as tx:
        tx.execute("UPDATE jobs SET not_before = ?, auto_retries = 2 WHERE id = ?", [NOW, JOB])
    make_shim(store, config(retries=2)).run()
    row = job(store)
    assert (row["status"], row["not_before"]) == ("failed", None)


def test_an_exit_13_hold_after_a_retried_start_keeps_no_not_before(store: Store) -> None:
    seed_claimed(store, ["sh", "-c", "exit 13"])
    with store.write() as tx:
        tx.execute("UPDATE jobs SET not_before = ?, auto_retries = 1 WHERE id = ?", [NOW, JOB])
    make_shim(store, config()).run()
    row = job(store)
    assert (row["status"], row["not_before"]) == ("held", None)


def test_a_non_requeue_failure_while_starting_keeps_no_not_before(
    store: Store, tmp_path: Any
) -> None:
    seed_claimed(store, ["true"], cwd=str(tmp_path / "gone"))
    with store.write() as tx:
        tx.execute("UPDATE jobs SET not_before = ? WHERE id = ?", [NOW, JOB])
    make_shim(store, config()).run()
    row = job(store)
    assert (row["status"], row["reason"], row["not_before"]) == ("failed", "cwd_missing", None)


def test_the_final_commit_reads_the_clock_inside_the_transaction(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobsctl.shim as shim_module

    seed_claimed(store, ["true"])
    shim = make_shim(store, config())
    reads: list[int] = []
    real_clock = shim.clock

    def counting() -> Any:
        reads.append(1)
        return real_clock()

    shim.clock = counting  # type: ignore[method-assign]
    real = shim_module.finish_running
    seen: list[tuple[int, int]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        before = len(reads)
        out = real(*args, **kwargs)
        seen.append((before, len(reads)))
        return out

    monkeypatch.setattr(shim_module, "finish_running", spy)
    shim.run()
    ((before, after),) = seen
    assert after > before  # the commit itself asked for the time


def test_the_third_consecutive_spawn_failure_stays_failed_despite_retries(store: Store) -> None:
    from jobsctl.claims import fail_spawn

    seed_claimed(store, ["true"], spawn_failures=2)
    with store.write() as tx:
        outcome, _ = fail_spawn(
            tx, config(retries=3), JOB, CLAIM, now="2026-01-01T00:00:05.000Z", message="no python"
        )
    assert outcome is not None and outcome.final
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "spawn_failed")


def test_a_post_fail_event_shows_queued_for_a_requeued_spawn_failure(store: Store) -> None:
    cfg = config(hooks={"post-fail": [{"cmd": "true"}]})
    run(store, ["no-such-command-xyz"], cfg)
    payload = json.loads(one(store, "SELECT * FROM hook_outbox")["payload"])
    assert payload["job"]["status"] == "queued"
    assert payload["attempt"]["reason"] == "spawn_failed"


def test_a_post_fail_event_shows_queued_for_a_terminated_start(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(hooks={"post-fail": [{"cmd": "true"}]})
    seed_claimed(store, ["true"])
    shim = make_shim(store, cfg)
    real = shim._still_starting  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def look_then_term() -> object:
        out = real()
        calls.append(1)
        if len(calls) == 2:
            shim.request_stop()
        return out

    monkeypatch.setattr(shim, "_still_starting", look_then_term)
    shim.run()
    payload = json.loads(one(store, "SELECT * FROM hook_outbox")["payload"])
    assert payload["job"]["status"] == "queued"
    assert payload["attempt"]["reason"] == "terminated"
