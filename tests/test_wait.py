"""`wait` and `add --wait` (interface.md, wait, add): exit codes, timeout, interrupts, pruning."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from jobsctl import gateway
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.manual_helpers import (
    add_job,
    make_done,
    make_held,
    row,
    sql,
)

pytestmark = pytest.mark.usefixtures("validated_records")


class Clock:
    """A fake monotonic clock; `sleep` advances it and runs the test's hook for that tick."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.hook: Callable[[int], None] = lambda tick: None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.hook(len(self.sleeps))

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(gateway, "sleep", c.sleep)
    monkeypatch.setattr(gateway, "monotonic", c.monotonic)
    return c


def wait(capsys: pytest.CaptureFixture[str], *args: str) -> Result:
    return run_cli(capsys, "--json", "wait", *args)


def shown(res: Result, code: int, *, validate: bool = True) -> dict[str, Any]:
    """The printed job. `validate=False` for rows whose hand-built attempts are not realistic
    enough for the schema's cross-field rules (only the exit code and status are under test)."""
    assert res.code == code, res.out + res.err
    doc = res.envelope()
    if validate:
        validate_against("cli/show.json", doc)
    return doc


@pytest.mark.parametrize(("status", "code"), [("done", 0), ("failed", 12), ("canceled", 20)])
def test_a_terminal_job_returns_at_once_with_its_exit_code(
    capsys: pytest.CaptureFixture[str], clock: Clock, status: str, code: int
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, status)
    if status == "canceled":
        sql("UPDATE jobs SET reason = 'canceled' WHERE id = ?", job_id)
        sql("UPDATE attempts SET reason = 'canceled' WHERE job_id = ?", job_id)
    doc = shown(wait(capsys, job_id), code, validate=status != "canceled")
    assert doc["status"] == status and doc["id"] == job_id
    assert doc["attempts"][0]["status"] == status
    assert clock.sleeps == []


@pytest.mark.parametrize("reason", ["canceled", "forced", "vetoed", "resolved"])
def test_canceled_reasons_all_exit_20_and_resolved_follows_its_status(
    capsys: pytest.CaptureFixture[str], clock: Clock, reason: str
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "canceled")
    sql("UPDATE jobs SET reason = ? WHERE id = ?", reason, job_id)
    sql("UPDATE attempts SET reason = ? WHERE job_id = ?", reason, job_id)
    doc = shown(wait(capsys, job_id), 20, validate=False)
    assert doc["reason"] == reason


@pytest.mark.parametrize("status", ["done", "failed"])
def test_a_resolved_job_exits_by_the_status_it_was_resolved_as(
    capsys: pytest.CaptureFixture[str], clock: Clock, status: str
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, status)
    sql("UPDATE jobs SET reason = 'resolved', resolved_by = 'human:x' WHERE id = ?", job_id)
    shown(wait(capsys, job_id), {"done": 0, "failed": 12}[status], validate=False)


@pytest.mark.parametrize("hold", ["manual", "exit_13", "escalated"])
def test_a_held_job_exits_13_at_once(
    capsys: pytest.CaptureFixture[str], clock: Clock, hold: str
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, hold)
    doc = shown(wait(capsys, job_id), 13, validate=hold != "escalated")
    assert doc["hold_reason"] == hold and clock.sleeps == []


def test_a_job_that_ends_while_waiting_ends_the_wait(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    clock.hook = lambda tick: make_done(job_id, "failed") if tick == 3 else None
    shown(wait(capsys, job_id), 12)
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_a_held_job_is_printed_with_its_output(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    doc = shown(wait(capsys, job_id), 13)
    assert doc["output"]["tail"] == "out"


def test_a_job_waiting_for_its_automatic_retry_is_not_terminal(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_done(job_id, "failed")
    sql(
        "UPDATE jobs SET status = 'queued', reason = NULL, ended_at = NULL, "
        "not_before = '2099-01-01T00:00:00.000Z' WHERE id = ?",
        job_id,
    )

    def finish(tick: int) -> None:
        if tick == 5:
            sql(
                "UPDATE jobs SET status = 'done', reason = 'exit', not_before = NULL, "
                "ended_at = '2026-01-01T00:00:09.000Z', attempt = 2 WHERE id = ?",
                job_id,
            )

    clock.hook = finish
    doc = shown(wait(capsys, job_id), 0, validate=False)
    assert len(clock.sleeps) == 5 and doc["status"] == "done"


def test_the_timeout_prints_the_current_job_and_exits_22(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    doc = shown(wait(capsys, job_id, "--timeout", "3s"), 22)
    assert doc["status"] == "queued"
    assert sum(clock.sleeps) == 3.0 and max(clock.sleeps) <= 1.0
    assert "error" not in doc


def test_a_job_that_ends_exactly_at_the_deadline_is_reported_not_timed_out(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    clock.hook = lambda tick: make_done(job_id) if tick == 2 else None
    shown(wait(capsys, job_id, "--timeout", "2s"), 0)


def test_an_unknown_job_is_not_found_and_creates_no_state(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = wait(capsys, "jb-aaaaaaaa")
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"
    assert not resolve_state_dir().exists()


def test_a_job_pruned_while_waiting_is_not_found(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    clock.hook = lambda tick: sql("DELETE FROM jobs WHERE id = ?", job_id)
    res = wait(capsys, job_id)
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"


def test_a_malformed_id_is_a_usage_error(capsys: pytest.CaptureFixture[str], clock: Clock) -> None:
    assert wait(capsys, "jb-1").code == 2


def test_a_schema_upgrade_underneath_a_wait_is_version_skew(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)

    def upgrade(tick: int) -> None:
        conn = sqlite3.connect(resolve_state_dir() / "state.db")
        conn.execute("PRAGMA user_version = 99")
        conn.close()

    clock.hook = upgrade
    res = wait(capsys, job_id)
    assert res.code == 12 and res.envelope()["error"]["code"] == "version_skew"


def test_ctrl_c_prints_the_job_exits_130_and_leaves_it_queued(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)

    def interrupt(tick: int) -> None:
        raise KeyboardInterrupt

    clock.hook = interrupt
    doc = shown(wait(capsys, job_id), 130)
    assert doc["status"] == "queued" and row(job_id)["status"] == "queued"


def test_a_corrupt_stored_tail_still_gives_the_exit_code(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = x'ff', compressed = 1 WHERE job_id = ?", job_id)
    res = wait(capsys, job_id)
    doc = shown(res, 13)
    assert doc["output"] is None and "cannot be read" in res.err


def test_human_output_names_the_job_and_status_and_sanitizes_the_tail(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = ? WHERE job_id = ?", b"hi \x1b[31mred\n", job_id)
    res = run_cli(capsys, "wait", job_id)
    assert res.code == 13
    assert job_id in res.out and "held" in res.out and "\x1b" not in res.out


# -- add --wait ---------------------------------------------------------------------------


def add_wait(capsys: pytest.CaptureFixture[str], *flags: str) -> Result:
    return run_cli(
        capsys, "--json", "add", "--no-start", "--wait", *flags, "--cwd", "/work", "--", "x"
    )


def test_add_wait_creates_the_job_first_then_waits_and_embeds_the_detail(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    def finish(tick: int) -> None:
        job_id = sql_one("SELECT id FROM jobs")
        if tick == 2:
            make_done(job_id, "failed")

    clock.hook = finish
    res = add_wait(capsys)
    assert res.code == 12, res.out + res.err
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["created"] is True and doc["waited"] is True
    assert doc["job"]["status"] == "failed" and doc["job"]["attempts"]


def sql_one(statement: str) -> str:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    try:
        return str(conn.execute(statement).fetchone()[0])
    finally:
        conn.close()


def test_add_wait_ctrl_c_leaves_the_job_queued_and_reports_its_id(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    def interrupt(tick: int) -> None:
        raise KeyboardInterrupt

    clock.hook = interrupt
    res = add_wait(capsys)
    assert res.code == 130
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["job"]["status"] == "queued" and doc["waited"] is True
    assert row(doc["job"]["id"])["status"] == "queued"


def test_add_wait_timeout_exits_22_with_the_job(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = add_wait(capsys, "--wait-timeout", "2s")
    assert res.code == 22
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["job"]["status"] == "queued"


def test_add_wait_on_a_held_unique_key_exits_13_at_once(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys, "--unique-key", "k")
    make_held(job_id, "manual")
    res = add_wait(capsys, "--unique-key", "k")
    assert res.code == 13
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["created"] is False and doc["job"]["id"] == job_id
    assert doc["warnings"][0]["code"] == "existing_job_held"


def test_add_wait_usage_errors_still_come_first_and_write_nothing(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = run_cli(capsys, "--json", "add", "--wait", "--dry-run", "--", "x")
    assert res.code == 2 and not resolve_state_dir().exists()
    res = run_cli(capsys, "--json", "add", "--wait-timeout", "1s", "--", "x")
    assert res.code == 2 and not resolve_state_dir().exists()


def test_add_wait_human_prints_the_final_status(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    res = run_cli(
        capsys, "add", "--no-start", "--wait", "--wait-timeout", "1s", "--cwd", "/w", "--", "x"
    )
    assert res.code == 22 and "queued" in res.out and "jb-" in res.out


def test_a_retry_pending_job_with_a_timeout_exits_22(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z' WHERE id = ?", job_id)
    doc = shown(wait(capsys, job_id, "--timeout", "3s"), 22)
    assert doc["status"] == "queued" and sum(clock.sleeps) == 3.0


def test_wait_on_an_older_schema_is_refused_read_only_and_never_migrates(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    from jobsctl import store_db

    job_id = add_job(capsys)
    path = resolve_state_dir() / "state.db"
    newer = (*store_db.MIGRATIONS, ("CREATE TABLE migrated_marker (x)",))
    monkeypatch.setattr(store_db, "MIGRATIONS", newer)
    res = wait(capsys, job_id)
    err = res.envelope()["error"]
    assert res.code == 1 and err["code"] == "internal"
    assert "run any jobsctl command" in err["message"]
    with sqlite3.connect(path) as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'migrated_marker'"
        ).fetchone()


def test_a_signal_while_the_database_is_being_opened_still_prints_the_job(
    capsys: pytest.CaptureFixture[str], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.commands import wait as wait_module

    job_id = add_job(capsys)
    real_open = wait_module._open
    calls: list[int] = []

    def interrupted_open(ctx: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_open(ctx)

    monkeypatch.setattr(wait_module, "_open", interrupted_open)
    doc = shown(wait(capsys, job_id), 130)
    assert doc["id"] == job_id and doc["status"] == "queued"


def test_the_handlers_ignore_every_signal_after_the_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import signal

    from jobsctl.commands.wait import interrupts_as_keyboard_interrupt

    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    with interrupts_as_keyboard_interrupt():
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)  # no second exception
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGHUP)
    assert {s: signal.getsignal(s) for s in before} == before


def test_a_first_signal_after_the_body_finished_does_not_skip_the_restore() -> None:
    import inspect
    import signal
    import sys

    from jobsctl.commands.wait import interrupts_as_keyboard_interrupt

    source, first = inspect.getsourcelines(interrupts_as_keyboard_interrupt)
    target = first + next(i for i, line in enumerate(source) if "fired = True  # from" in line)
    code = interrupts_as_keyboard_interrupt.__wrapped__.__code__  # type: ignore[attr-defined]
    sent = False

    def local(frame: Any, event: str, arg: Any) -> Any:
        nonlocal sent
        if event == "line" and frame.f_lineno == target and not sent:
            sent = True
            signal.raise_signal(signal.SIGTERM)  # the first signal, at the vulnerable line
        return local

    def tracer(frame: Any, event: str, arg: Any) -> Any:
        return local if frame.f_code is code else None

    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    sys.settrace(tracer)
    try:
        with interrupts_as_keyboard_interrupt():
            pass
    finally:
        sys.settrace(None)
    assert sent
    assert {s: signal.getsignal(s) for s in before} == before


def test_a_handler_installed_outside_python_is_restored_as_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    from jobsctl.commands.wait import interrupts_as_keyboard_interrupt

    real_get = signal.getsignal
    real_set = signal.signal
    restored: list[Any] = []

    def fake_get(number: int) -> Any:
        return None if number == signal.SIGHUP else real_get(number)

    def fake_set(number: int, handler: Any) -> Any:
        if number == signal.SIGHUP:
            restored.append(handler)
            if handler is None:
                raise TypeError("signal handler must be SIG_IGN, SIG_DFL, or a callable")
            return None
        return real_set(number, handler)

    original = real_get(signal.SIGHUP)
    monkeypatch.setattr(signal, "getsignal", fake_get)
    monkeypatch.setattr(signal, "signal", fake_set)
    try:
        with interrupts_as_keyboard_interrupt():
            pass
    finally:
        monkeypatch.undo()
        real_set(signal.SIGHUP, original)
    assert restored[-1] is signal.SIG_DFL


def test_a_signal_ignored_at_entry_is_left_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import signal

    from jobsctl.commands.wait import interrupts_as_keyboard_interrupt

    old = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with interrupts_as_keyboard_interrupt():
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            signal.raise_signal(signal.SIGHUP)  # does not interrupt
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, old)


def test_an_unreadable_tail_warning_is_quiet_under_quiet_and_sanitized(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    sql("UPDATE outputs SET tail = x'ff', compressed = 1 WHERE job_id = ?", job_id)
    res = run_cli(capsys, "--json", "--quiet", "wait", job_id)
    assert res.code == 13 and res.err == "" and res.envelope()["output"] is None


def test_the_unreadable_tail_message_is_sanitized(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl import job_detail
    from jobsctl.errors import JobsctlError

    def boom(*args: Any) -> Any:
        raise JobsctlError("internal", "bad \x1b[31mred\nline")

    monkeypatch.setattr(job_detail, "load_tail", boom)
    assert job_detail.output_object(None, "jb-aaaaaaaa", 1) is None  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "\x1b" not in err and err.count("\n") == 1


# -- add --wait: interrupts, warnings, version skew ---------------------------------------


def test_add_wait_interrupted_while_requesting_the_pass_still_prints_the_job(
    capsys: pytest.CaptureFixture[str], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.commands import add as add_module

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(add_module, "request_pass_from_command", interrupted)
    res = run_cli(capsys, "--json", "add", "--wait", "--cwd", "/work", "--", "x")
    assert res.code == 130, res.out + res.err
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["waited"] is True and doc["job"]["id"] == sql_one("SELECT id FROM jobs")
    assert doc["job"]["status"] == "queued"


def test_add_wait_interrupted_before_any_snapshot_prints_the_committed_job(
    capsys: pytest.CaptureFixture[str], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.commands import add as add_module
    from jobsctl.commands import wait as wait_module

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    def unreadable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no snapshot")

    monkeypatch.setattr(add_module, "request_pass_from_command", interrupted)
    monkeypatch.setattr(wait_module, "_snapshot", unreadable)
    res = run_cli(capsys, "--json", "add", "--wait", "--cwd", "/work", "--", "x")
    assert res.code == 130, res.out + res.err
    doc = res.envelope()
    validate_against("cli/add.json", doc)
    assert doc["job"]["id"] == sql_one("SELECT id FROM jobs") and doc["created"] is True


def test_add_wait_human_prints_its_warnings_before_it_waits(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    seen: list[str] = []

    def hook(tick: int) -> None:
        seen.append(capsys.readouterr().err)
        make_done(sql_one("SELECT id FROM jobs"))

    clock.hook = hook
    res = run_cli(capsys, "add", "--no-start", "--wait", "--cwd", "/w", "--", "x")
    assert res.code == 0, res.out + res.err
    assert "created on first use" in seen[0] and "created on first use" not in res.err


def test_add_wait_version_skew_is_the_error_and_the_job_stays_committed(
    capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    def upgrade(tick: int) -> None:
        conn = sqlite3.connect(resolve_state_dir() / "state.db")
        conn.execute("PRAGMA user_version = 99")
        conn.close()

    clock.hook = upgrade
    res = add_wait(capsys)
    assert res.code == 12 and res.envelope()["error"]["code"] == "version_skew"
    assert sql_one("SELECT status FROM jobs") == "queued"  # committed before the wait began
