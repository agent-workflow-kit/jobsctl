"""The shim's output pump, leftovers, result file and final commit retries (in process)."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl import gateway
from jobsctl import shim as shim_module
from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.proc_host import GroupScan, default_host
from jobsctl.result_file import read_result
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.helpers import run_cli
from tests.kill_guard import allow
from tests.shim_helpers import (
    JOB,
    attempt,
    exit_of,
    job,
    log_records,
    make_shim,
    one,
    seed_claimed,
    shims_dir,
    tail_of,
)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def _validated(validated_records: None) -> None:
    return None


# -- output ---------------------------------------------------------------------------------------


def test_a_flood_of_output_keeps_a_bounded_tail_and_counts_every_byte(store: Store) -> None:
    seed_claimed(store, ["sh", "-c", "yes 0123456789abcdef | head -c 20000000"], queue="q")  # 20 MB
    cfg = Config.model_validate({"tail_bytes": 4096, "tail_lines": 50})
    make_shim(store, cfg).run()
    assert job(store)["status"] == "done"
    row = one(store, "SELECT * FROM outputs WHERE job_id = ?", [JOB])
    assert row["bytes_seen"] == 20_000_000 and row["truncated"] == 1
    tail = tail_of(store) or ""
    assert len(tail.encode()) <= 4096 and tail.count("\n") <= 50
    assert tail.endswith("0123456789")  # head -c cut the last line; the final tail keeps it


def test_the_live_tail_is_flushed_while_the_job_runs(store: Store) -> None:
    seed_claimed(
        store,
        ["sh", "-c", "echo first; sleep 3; echo second"],
    )
    cfg = Config.model_validate({"tail_flush_interval": "1s"})
    seen: list[str] = []

    def watch() -> None:
        with Store.open() as other:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                text = tail_of(other)
                if text is not None and "first" in text:
                    seen.append(text)
                    return
                time.sleep(0.05)

    t = threading.Thread(target=watch)
    t.start()
    make_shim(store, cfg).run()
    t.join()
    assert seen and "second" not in seen[0]  # flushed live, before the job ended
    assert tail_of(store) == "first\nsecond\n"  # the final tail has everything


def test_a_live_flush_withholds_the_last_partial_line_and_the_final_tail_includes_it(
    store: Store,
) -> None:
    seed_claimed(store, ["sh", "-c", "printf 'done line\\npartial'; sleep 2.5"])
    cfg = Config.model_validate({"tail_flush_interval": "1s"})
    live: list[str] = []

    def watch() -> None:
        with Store.open() as other:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                text = tail_of(other)
                if text:
                    live.append(text)
                    return
                time.sleep(0.05)

    t = threading.Thread(target=watch)
    t.start()
    make_shim(store, cfg).run()
    t.join()
    assert live == ["done line\n"]
    assert tail_of(store) == "done line\npartial"


def test_hostile_output_is_sanitized_in_the_stored_tail(store: Store) -> None:
    seed_claimed(
        store,
        [
            "sh",
            "-c",
            "printf '\\033]0;title\\007ok\\033[31m red\\033[0m \\001 \\342\\200\\256 bidi\\n'",
        ],
    )
    make_shim(store).run()
    text = tail_of(store) or ""
    assert "\x1b" not in text and "\x07" not in text and "‮" not in text
    assert "ok red" in text and "\\u0001" in text and "\\u202e" in text


def test_invalid_utf8_output_uses_backslashreplace(store: Store) -> None:
    seed_claimed(store, ["sh", "-c", "printf 'a\\377b\\n'"])
    make_shim(store).run()
    assert tail_of(store) == "a\\xffb\n"


def test_a_job_with_no_output_has_an_empty_tail_row(store: Store) -> None:
    seed_claimed(store, ["true"])
    make_shim(store).run()
    row = one(store, "SELECT * FROM outputs WHERE job_id = ?", [JOB])
    assert row["bytes_seen"] == 0 and row["truncated"] == 0 and tail_of(store) == ""


# -- leftovers ------------------------------------------------------------------------------------


def test_leftovers_are_counted_logged_and_never_signaled(store: Store, tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    seed_claimed(store, ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; exit 0"])
    started = time.monotonic()
    make_shim(store).run()
    pid = int(pidfile.read_text())
    allow(pid)  # reparented away from us; the PID this test's job printed
    try:
        # the background sleep held the output pipe open: the 2 s drain ended, not end of file
        assert time.monotonic() - started < 15
        assert job(store)["status"] == "done" and attempt(store)["leftovers"] == 1
        os.kill(pid, 0)  # still alive: counted, not signaled
        (rec,) = log_records("leftovers")
        assert rec["level"] == "warn" and rec["details"]["count"] == 1
    finally:
        os.kill(pid, signal.SIGKILL)  # the PID this test's job printed


def test_a_clean_exit_records_zero_leftovers_and_logs_nothing(store: Store) -> None:
    seed_claimed(store, ["true"])
    make_shim(store).run()
    assert attempt(store)["leftovers"] == 0
    # an incomplete scan (a loaded host) may add a count-0 info record; no process was left
    assert [r for r in log_records("leftovers") if r["details"]["count"]] == []


class _IncompleteScanHost(type(default_host())):  # type: ignore[misc]
    """The real host whose `/proc` scans always report one unreadable entry (a loaded host, or
    `hidepid`): what they find is unchanged, but they are never complete."""

    def scan_group(self, pgid: int) -> GroupScan:
        got = super().scan_group(pgid)
        return GroupScan(got.members, got.unreadable + 1, got.hidden)

    def scan_children(self, ppid: int) -> GroupScan:
        got = super().scan_children(ppid)
        return GroupScan(got.members, got.unreadable + 1, got.hidden)


@pytest.mark.parametrize(
    ("script", "kind"), [("exit 3", "exited"), ("kill -KILL $$", "signaled")]
)
def test_an_incomplete_scan_records_zero_and_the_show_document_validates(
    store: Store, capsys: pytest.CaptureFixture[str], script: str, kind: str
) -> None:
    seed_claimed(store, ["sh", "-c", script])
    make_shim(store, host=_IncompleteScanHost()).run()
    row = attempt(store)
    assert exit_of(store)["kind"] == kind  # type: ignore[index]
    assert row["leftovers"] == 0 and type(row["leftovers"]) is int
    res = run_cli(capsys, "--json", "show", JOB)
    doc = res.envelope()
    validate_against("cli/show.json", doc)  # the schema demands an integer for exited/signaled
    assert doc["leftovers"] == 0
    (rec,) = log_records("leftovers")  # the incompleteness stays observable
    assert rec["level"] == "info" and rec["details"] == {"count": 0, "complete": False}
    validate_record_file(resolve_state_dir() / "log.jsonl", "log.json")


def test_an_incomplete_scan_that_finds_a_process_records_the_count_and_warns(
    store: Store, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pids"
    seed_claimed(store, ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; exit 0"])
    make_shim(store, host=_IncompleteScanHost()).run()
    pid = int(pidfile.read_text())
    allow(pid)
    try:
        assert attempt(store)["leftovers"] == 1
        (rec,) = log_records("leftovers")
        assert rec["level"] == "warn" and rec["details"] == {"count": 1, "complete": False}
    finally:
        os.kill(pid, signal.SIGKILL)


# -- the result file and the final commit ---------------------------------------------------------


def test_the_result_file_exists_before_the_final_commit_and_is_removed_after(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_claimed(store, ["sh", "-c", "echo out; exit 3"])
    seen: list[object] = []
    real = shim_module.finish_running

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(read_result(shims_dir(), JOB, 1))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shim_module, "finish_running", spy)
    make_shim(store).run()
    (record,) = seen
    assert record is not None
    assert (record.status, record.reason) == ("failed", "exit")  # type: ignore[attr-defined]
    assert record.exit.code == 3 and record.tail.text == "out\n"  # type: ignore[attr-defined]
    assert record.claim_id == one(store, "SELECT claim_id FROM attempts")["claim_id"]  # type: ignore[attr-defined]
    assert list(shims_dir().glob("*.result*")) == []


def test_the_final_commit_is_retried_with_backoff_while_the_database_is_busy(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_claimed(store, ["true"])
    calls: list[int] = []
    real = shim_module.finish_running

    def flaky(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) < 4:
            raise JobsctlError("busy", "state.db is busy")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    sleeps: list[float] = []
    monkeypatch.setattr(shim_module, "finish_running", flaky)
    monkeypatch.setattr(gateway, "sleep", lambda s: sleeps.append(s) or None)
    make_shim(store).run()
    assert len(calls) == 4 and job(store)["status"] == "done"
    backoff = [s for s in sleeps if s >= 0.2]
    assert backoff == sorted(backoff) and backoff[-1] > backoff[0]  # grows
    assert list(shims_dir().glob("*.result*")) == []


def test_a_final_commit_that_never_succeeds_leaves_the_result_file_for_reconcile(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_claimed(store, ["sh", "-c", "echo kept; exit 0"])
    monkeypatch.setattr(shim_module, "FINAL_COMMIT_SECONDS", 0.5)

    def always_busy(*args: object, **kwargs: object) -> object:
        raise JobsctlError("busy", "state.db is busy")

    monkeypatch.setattr(shim_module, "finish_running", always_busy)
    assert make_shim(store).run() == 1
    assert job(store)["status"] == "running"
    record = read_result(shims_dir(), JOB, 1)
    assert record is not None and record.status == "done" and record.tail.text == "kept\n"


def test_a_planted_symlink_at_the_result_name_is_not_written_through(
    store: Store, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    os.symlink(victim, shims_dir() / f"{JOB}.1.result")
    seed_claimed(store, ["true"])
    make_shim(store).run()
    assert victim.read_text() == "precious" and job(store)["status"] == "done"
    assert not os.path.lexists(shims_dir() / f"{JOB}.1.result")


def test_a_result_file_that_cannot_be_written_does_not_stop_the_commit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.result_file import ResultFileError

    seed_claimed(store, ["true"])

    def boom(*args: object, **kwargs: object) -> str:
        raise ResultFileError("disk full")

    monkeypatch.setattr(shim_module, "write_result", boom)
    make_shim(store).run()
    assert job(store)["status"] == "done"


def test_an_attempt_finalized_by_someone_else_is_left_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile finalized the attempt from the result file while the shim retried: the shim's
    final transaction matches nothing and writes nothing."""
    seed_claimed(store, ["true"])
    real = shim_module.finish_running

    def after_reconcile(*args: object, **kwargs: object) -> object:
        with Store.open() as other, other.write() as tx:
            tx.execute("UPDATE jobs SET status = 'failed', reason = 'lost' WHERE id = ?", [JOB])
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shim_module, "finish_running", after_reconcile)
    make_shim(store).run()
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "lost")
    assert exit_of(store) is None
