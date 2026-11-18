"""`job_detail` (the job `show` and `wait` build) and `job_lookup` (the one job lookup)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from jobsctl import job_detail as detail
from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.job_lookup import find_job, job_by_unique_key
from jobsctl.job_view import attempt_object
from jobsctl.store_db import OpenMode, Store
from tests.view_helpers import ENDED, ran, seed_finished, seed_job

NOW = "2026-03-10T12:00:00.000Z"


def tail(text: bytes, **over: Any) -> dict[str, Any]:
    return {"tail": text, "bytes_seen": len(text), "truncated": 0, **over}


def seed_two_attempts() -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=2,
        ended_at=ENDED,
        attempts=[ran(), ran()],
        output=tail(b"second\n"),
        output_attempt=2,
    )
    with Store.open(mode=OpenMode.CLI) as store, store.write() as tx:
        tx.execute(
            "INSERT INTO outputs (job_id, n, tail, compressed, bytes_seen, truncated, updated_at)"
            " VALUES ('jb-aaaaaa22', 1, ?, 0, 6, 0, ?)",
            [b"first\n", NOW],
        )


def detail_of(job_id: str, **kw: Any) -> dict[str, Any]:
    with Store.open(mode=OpenMode.CLI) as store, store.read() as tx:
        row = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
        assert row is not None
        return detail.job_detail(
            tx, row, Config(), env_file_unavailable=False, now=NOW, quiet=True, **kw
        )


def test_default_output_is_the_current_attempts() -> None:
    seed_two_attempts()
    job = detail_of("jb-aaaaaa22")
    assert [a["attempt"] for a in job["attempts"]] == [1, 2]
    assert job["output"]["tail"] == "second\n"


def test_attempt_selects_that_attempts_output() -> None:
    seed_two_attempts()
    assert detail_of("jb-aaaaaa22", attempt=1)["output"]["tail"] == "first\n"


@pytest.mark.parametrize("n", [0, 3, -1])
def test_an_attempt_the_job_lacks_is_not_found(n: int) -> None:
    seed_two_attempts()
    with pytest.raises(JobsctlError) as err:
        detail_of("jb-aaaaaa22", attempt=n)
    assert err.value.code == "not_found"


def test_output_off_gives_null_output_but_keeps_attempts() -> None:
    seed_two_attempts()
    job = detail_of("jb-aaaaaa22", output="off")
    assert job["output"] is None and len(job["attempts"]) == 2


def test_a_queued_job_has_no_default_output() -> None:
    seed_job("jb-bbbbbb22")
    assert detail_of("jb-bbbbbb22")["output"] is None


def test_a_requeued_job_has_output_only_for_an_explicit_earlier_attempt() -> None:
    seed_job(
        "jb-eeeeee22",
        attempt=1,
        attempts=[ran()],
        output=tail(b"earlier\n"),
        output_attempt=1,
    )
    assert detail_of("jb-eeeeee22")["output"] is None
    assert detail_of("jb-eeeeee22", attempt=1)["output"]["tail"] == "earlier\n"


def test_an_invalid_attempt_is_not_found_with_output_off() -> None:
    seed_two_attempts()
    with pytest.raises(JobsctlError) as err:
        detail_of("jb-aaaaaa22", attempt=99, output="off")
    assert err.value.code == "not_found"
    assert err.value.message == "job jb-aaaaaa22 has no attempt 99"


def test_unreadable_tail_raise_vs_warn(capsys: pytest.CaptureFixture[str]) -> None:
    seed_finished("jb-cccccc22", status="failed", output=tail(b"zz", compressed=1))
    assert detail_of("jb-cccccc22")["output"] is None  # "warn", quiet: no stderr
    assert capsys.readouterr().err == ""
    with pytest.raises(JobsctlError) as err:
        detail_of("jb-cccccc22", output="raise")
    assert err.value.code == "internal"


def test_warn_mode_writes_the_warning_when_not_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_finished("jb-cccccc22", status="failed", output=tail(b"zz", compressed=1))
    with Store.open(mode=OpenMode.CLI) as store, store.read() as tx:
        assert detail.output_object(tx, "jb-cccccc22", 1) is None
    assert "cannot be read" in capsys.readouterr().err


def test_raise_mode_sanitizes_and_escapes_undecodable_bytes() -> None:
    seed_finished("jb-dddddd22", status="failed", output=tail(b"a\x1b[31mred\x1b[0m \xff\n"))
    assert detail_of("jb-dddddd22", output="raise")["output"]["tail"] == "ared \\xff\n"


def test_find_job_by_id_prefix_and_failures() -> None:
    seed_job("jb-aaaaaa22")
    seed_job("jb-aaaaaa33")
    with Store.open(mode=OpenMode.CLI) as store, store.read() as tx:
        assert find_job(tx, "jb-aaaaaa22")["id"] == "jb-aaaaaa22"
        assert find_job(tx, "jb-aaaaaa2")["id"] == "jb-aaaaaa22"
        for text, code in (("jb-aaaaaa", "ambiguous"), ("jb-zzzzzzzz", "not_found")):
            with pytest.raises(JobsctlError) as err:
                find_job(tx, text)
            assert err.value.code == code
        with pytest.raises(JobsctlError) as bad:
            find_job(tx, "not an id")
        assert bad.value.code == "usage"


def test_find_job_without_a_database_is_not_found() -> None:
    with pytest.raises(JobsctlError) as err:
        find_job(None, "jb-aaaaaa22")
    assert err.value.code == "not_found"


def test_unique_key_prefers_the_live_job_then_the_newest() -> None:
    seed_job(
        "jb-aaaaaa22",
        status="done",
        reason="exit",
        unique_key="k",
        created_at="2026-03-01T00:00:00.000Z",
        attempt=1,
        ended_at=ENDED,
        attempts=[ran("done", "exit", 0)],
    )
    seed_job(
        "jb-bbbbbb22",
        status="done",
        reason="exit",
        unique_key="k",
        created_at="2026-03-02T00:00:00.000Z",
        attempt=1,
        ended_at=ENDED,
        attempts=[ran("done", "exit", 0)],
    )
    with Store.open(mode=OpenMode.CLI) as store, store.read() as tx:
        assert job_by_unique_key(tx, "k")["id"] == "jb-bbbbbb22"
        with pytest.raises(JobsctlError) as err:
            job_by_unique_key(tx, "none")
        assert err.value.code == "not_found"


def test_attempt_object_covers_every_attempt_column_shape() -> None:
    seed_two_attempts()
    with Store.open(mode=OpenMode.CLI) as store, store.read() as tx:
        row = tx.one("SELECT * FROM attempts WHERE job_id = 'jb-aaaaaa22' AND n = 1")
    assert isinstance(row, sqlite3.Row)
    obj = attempt_object(row)
    assert obj["attempt"] == 1 and obj["exit"]["kind"] == "exited"
    assert set(obj) == {
        "attempt", "status", "reason", "hold_reason", "claimed_at", "running_at", "ended_at",
        "exit", "leftovers", "resolved_path", "effective", "escalated_hooks", "vetoed_by",
        "hook_messages", "decision_ids", "force_requested_by", "resolved_by", "note",
        "jobsctl_version",
    }  # fmt: skip
    json.dumps(obj)
