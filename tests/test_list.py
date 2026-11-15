"""`jobsctl list`: ordering, filters, the 24-hour view filter, limit, golden output, sanitizing."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.view_helpers import (
    ago,
    check_golden,
    freeze_clock,
    ran,
    running_attempt,
    seed_finished,
    seed_job,
    starting_attempt,
)

H = dt.timedelta(hours=1)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch)


def ls(capsys: pytest.CaptureFixture[str], *flags: str) -> dict[str, Any]:
    res = run_cli(capsys, "--json", "list", *flags)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/list.json", doc)
    return doc


def ids(doc: dict[str, Any]) -> list[str]:
    return [j["id"] for j in doc["jobs"]]


def seed_all_groups() -> None:
    def sj(job_id: str, **over: Any) -> None:
        seed_job(job_id, queue=over.pop("queue", "a"), **over)

    def sf(job_id: str, *args: Any) -> None:
        seed_finished(job_id, *args, queue="a")

    # queue a: three queued (pinned, high priority, plain), starting, running, held, terminal
    sj("jb-aaaaaa22", priority=0, position="m1")
    sj("jb-aaaaaa23", priority=5, position="m2")
    sj("jb-aaaaaa24", priority=-3, pinned=1, position="m3")
    sj(
        "jb-aaaaaa25",
        status="starting",
        attempt=1,
        enqueued_at=ago(2 * H),
        attempts=[starting_attempt()],
    )
    sj(
        "jb-aaaaaa26",
        status="running",
        attempt=1,
        enqueued_at=ago(3 * H),
        attempts=[running_attempt()],
    )
    sj("jb-aaaaaa27", status="held", hold_reason="manual", enqueued_at=ago(1 * H))
    sf("jb-aaaaaa32", "done", "exit", ago(1 * H))
    sf("jb-aaaaaa33", "failed", "exit", ago(2 * H))
    sj("jb-aaaaaa34", status="canceled", reason="canceled", ended_at=ago(2 * H))
    sj("jb-bbbbbb22", queue="b")


ALL_GROUPS = [
    "jb-aaaaaa24",  # queued: pinned beats priority
    "jb-aaaaaa23",
    "jb-aaaaaa22",
    "jb-aaaaaa26",  # starting/running/held by enqueued_at, then id
    "jb-aaaaaa25",
    "jb-aaaaaa27",
    "jb-aaaaaa32",  # terminal: ended_at newest first, ties by id
    "jb-aaaaaa33",
    "jb-aaaaaa34",
    "jb-bbbbbb22",  # queue b sorts after queue a
]


def test_empty_store_lists_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert ls(capsys) == {"jobs": [], "truncated": False}


def test_order_across_queues_and_every_status_group(capsys: pytest.CaptureFixture[str]) -> None:
    seed_all_groups()
    assert ids(ls(capsys)) == ALL_GROUPS


def test_ranks_come_from_the_order_module(capsys: pytest.CaptureFixture[str]) -> None:
    seed_all_groups()
    by_id = {j["id"]: j for j in ls(capsys)["jobs"]}
    assert [by_id[i]["rank"] for i in ALL_GROUPS[:3]] == [1, 2, 3]
    assert by_id["jb-aaaaaa27"]["rank"] is None and by_id["jb-aaaaaa26"]["rank"] is None
    assert by_id["jb-bbbbbb22"]["rank"] == 1


def test_terminal_jobs_older_than_24_hours_are_omitted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    edge = dt.timedelta(hours=24)
    ms = dt.timedelta(milliseconds=1)
    seed_finished("jb-aaaaaa22", "done", "exit", ago(edge))  # exactly 24 h: kept
    seed_finished("jb-aaaaaa23", "done", "exit", ago(edge + ms))
    seed_finished("jb-aaaaaa24", "failed", "exit", ago(edge - ms))
    assert set(ids(ls(capsys))) == {"jb-aaaaaa22", "jb-aaaaaa24"}
    assert len(ids(ls(capsys, "--all"))) == 3
    both = ids(ls(capsys, "--status", "done", "--status", "failed"))
    assert set(both) == {"jb-aaaaaa22", "jb-aaaaaa23", "jb-aaaaaa24"}  # --status lifts the filter


def test_the_view_filter_never_hides_live_jobs(capsys: pytest.CaptureFixture[str]) -> None:
    old = ago(dt.timedelta(days=30))
    seed_job("jb-aaaaaa22", created_at=old, enqueued_at=old, status_changed_at=old)
    seed_job("jb-aaaaaa23", status="held", hold_reason="manual", created_at=old)
    assert len(ids(ls(capsys))) == 2


def test_status_filters_combine_with_or(capsys: pytest.CaptureFixture[str]) -> None:
    seed_all_groups()
    got = ids(ls(capsys, "--status", "running", "--status", "held"))
    assert got == ["jb-aaaaaa26", "jb-aaaaaa27"]
    assert ids(ls(capsys, "--status", "queued", "--queue", "b")) == ["jb-bbbbbb22"]


def test_unknown_status_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--status", "bogus")
    assert res.code == 2 and res.envelope()["error"]["code"] == "usage"


def test_labels_combine_with_and_and_unique_key_filters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job("jb-aaaaaa22", labels=[("a", "1"), ("b", "2")], unique_key="K1")
    seed_job("jb-aaaaaa23", labels=[("a", "1")], unique_key="K2")
    seed_job("jb-aaaaaa24", labels=[("a", "1"), ("a", "9"), ("b", "2")])
    assert ids(ls(capsys, "--label", "a=1")) == ["jb-aaaaaa22", "jb-aaaaaa23", "jb-aaaaaa24"]
    assert ids(ls(capsys, "--label", "a=1", "--label", "b=2")) == ["jb-aaaaaa22", "jb-aaaaaa24"]
    assert ids(ls(capsys, "--label", "a=9", "--label", "b=2")) == ["jb-aaaaaa24"]
    assert ids(ls(capsys, "--label", "a=2")) == []
    assert ids(ls(capsys, "--unique-key", "K2")) == ["jb-aaaaaa23"]
    assert ids(ls(capsys, "--unique-key", "nope")) == []


def test_unique_key_filter_lists_terminal_holders_of_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_finished("jb-aaaaaa22", "done", "exit", ago(H), unique_key="K")
    seed_job("jb-aaaaaa23", unique_key="K")
    assert ids(ls(capsys, "--unique-key", "K")) == ["jb-aaaaaa23", "jb-aaaaaa22"]


def test_limit_and_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    seed_all_groups()
    cut = ls(capsys, "--limit", "4")
    assert ids(cut) == ALL_GROUPS[:4] and cut["truncated"] is True
    exact = ls(capsys, "--limit", str(len(ALL_GROUPS)))
    assert ids(exact) == ALL_GROUPS and exact["truncated"] is False
    assert ls(capsys, "--limit", "1000")["truncated"] is False
    assert ls(capsys)["truncated"] is False


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_limit_below_one_is_a_usage_error(capsys: pytest.CaptureFixture[str], value: str) -> None:
    res = run_cli(capsys, "--json", "list", "--limit", value)
    assert res.code == 2


def test_list_jobs_have_no_attempts_or_output(capsys: pytest.CaptureFixture[str]) -> None:
    seed_all_groups()
    assert all("attempts" not in j and "output" not in j for j in ls(capsys)["jobs"])


def test_list_blocked_by_uses_the_clock(capsys: pytest.CaptureFixture[str]) -> None:
    future = ago(-H)
    seed_job("jb-aaaaaa22", not_before=future, attempt=1, attempts=[ran()])
    seed_job("jb-aaaaaa23", not_before=ago(H))
    by_id = {j["id"]: j for j in ls(capsys)["jobs"]}
    assert by_id["jb-aaaaaa22"]["blocked_by"] == "not_before"
    assert by_id["jb-aaaaaa23"]["blocked_by"] is None


def test_golden_json(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        argv=["echo", "hi"],
        cwd="/work",
        env={"MODE": "fast"},
        pass_env=["TOKEN"],
        labels=[("owner", "ticket-work"), ("a", "1")],
        unique_key="KEY-1",
        priority=2,
        overrides={"timeout": "none", "max_retries": 1},
        decision_ids=["d1"],
        created_at="2026-03-10T10:00:00.000Z",
        enqueued_at="2026-03-10T10:00:00.000Z",
        status_changed_at="2026-03-10T10:00:00.000Z",
    )
    seed_finished(
        "jb-aaaaaa23",
        "failed",
        "exit",
        "2026-03-10T11:00:00.000Z",
        created_at="2026-03-10T09:00:00.000Z",
        enqueued_at="2026-03-10T09:00:00.000Z",
        status_changed_at="2026-03-10T11:00:00.000Z",
    )
    check_golden("list.json", ls(capsys))


# -- human output ----------------------------------------------------------------------------

HOSTILE = "a\x1b[31mred\x1b]0;title\x07\x9b1;2m\u202eevil\u2028\x00\x7f\x85\u200bz"


def test_human_output_is_sanitized(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        queue="q",
        argv=["echo", HOSTILE, "\udcff", "\ud800", "line\nbreak\rcr"],
        cwd="/w/" + HOSTILE,
        unique_key=None,
        labels=[("k", "v")],
        status="canceled",
        reason="resolved",
        resolved_by="human:me",
        ended_at=ago(H),
        note=HOSTILE,
    )
    res = run_cli(capsys, "list")
    assert res.code == 0
    out = res.out
    for bad in ("\x1b", "\x9b", "\x07", "\x00", "\x7f", "\x85", "\u202e", "\u2028", "\u200b"):
        assert bad not in out
    assert "jb-aaaaaa22" in out
    assert out.count("\n") == 1  # one job, one line: nothing forges a line
    out.encode("utf-8")  # no lone surrogate survives


def test_human_list_is_empty_for_no_jobs(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list")
    assert res.code == 0 and res.out == ""


def test_hostile_argv_prints_the_documented_backslash_x_form(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job("jb-aaaaaa22", argv=["x", "\udcff", "\\xff"])
    job = ls(capsys)["jobs"][0]
    assert job["argv"] == ["x", "\\xff", "\\xff"]  # documented: not distinguishable
    assert json.dumps(job).isascii()


def test_raw_output_is_ascii_json(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", argv=["\u65e5\u672c\u8a9e", "\u202e"])
    res: Result = run_cli(capsys, "--json", "list")
    assert res.out.isascii() and res.envelope()["jobs"][0]["argv"] == [
        "\u65e5\u672c\u8a9e",
        "\u202e",
    ]


def test_human_line_quotes_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", argv=["echo", "a b", "it's", ""])
    res = run_cli(capsys, "list")
    assert res.out.endswith("-- echo 'a b' 'it'\"'\"'s' ''\n")
    assert "rank=1" in res.out and "queue=q" in res.out
