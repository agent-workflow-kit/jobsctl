"""``jobsctl export``: filters, torn files, hostile files, rotation, the CLI surface."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl import gateway
from jobsctl.log_export import export_lines, read_records, since_cutoff
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import Store
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import run_cli

pytestmark = pytest.mark.usefixtures("validated_records")

T0 = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(resolve_state_dir()) as s:
        yield s


def write_log(store: Store, n: int, step: dt.timedelta = dt.timedelta(hours=1)) -> None:
    """Records 1..n with ts T0, T0+step, ..."""
    for i in range(n):
        RecordWriter(store, clock=lambda i=i: T0 + i * step).log(
            "pass", fields={"message": f"r{i + 1}"}
        )


def seqs(lines: Iterator[str]) -> list[int]:
    return [json.loads(line)["seq"] for line in lines]


def test_export_prints_every_record_in_order(store: Store) -> None:
    write_log(store, 5)
    out = list(export_lines(store.paths.root / "log.jsonl"))
    assert seqs(iter(out)) == [1, 2, 3, 4, 5]
    for line in out:
        validate_against("log.json", json.loads(line))


def test_after_seq_is_exclusive(store: Store) -> None:
    write_log(store, 5)
    path = store.paths.root / "log.jsonl"
    assert seqs(export_lines(path, after_seq=3)) == [4, 5]
    assert seqs(export_lines(path, after_seq=0)) == [1, 2, 3, 4, 5]
    assert seqs(export_lines(path, after_seq=5)) == []
    assert seqs(export_lines(path, after_seq=10**30)) == []


def test_since_is_inclusive(store: Store) -> None:
    write_log(store, 5)
    path = store.paths.root / "log.jsonl"
    assert seqs(export_lines(path, since=T0 + dt.timedelta(hours=2))) == [3, 4, 5]
    just_after = T0 + dt.timedelta(hours=2, microseconds=500)  # sub-millisecond: record 3 is before
    assert seqs(export_lines(path, since=just_after)) == [4, 5]
    assert seqs(export_lines(path, since=T0 - dt.timedelta(days=1))) == [1, 2, 3, 4, 5]
    assert seqs(export_lines(path, since=T0 + dt.timedelta(days=1))) == []


def test_since_and_after_seq_combine_with_and(store: Store) -> None:
    write_log(store, 6)
    path = store.paths.root / "log.jsonl"
    since = T0 + dt.timedelta(hours=2)  # records 3..6
    assert seqs(export_lines(path, since=since, after_seq=4)) == [5, 6]
    assert seqs(export_lines(path, since=since, after_seq=1)) == [3, 4, 5, 6]
    assert seqs(export_lines(path, since=T0, after_seq=5)) == [6]


def test_rotation_keeps_seq_increasing_and_export_reads_only_the_current_file(
    store: Store,
) -> None:
    write_log(store, 3)
    path = store.paths.root / "log.jsonl"
    path.rename(store.paths.root / "log.jsonl.1")
    write_log(store, 2)
    assert seqs(export_lines(path)) == [4, 5]
    assert seqs(export_lines(path, after_seq=4)) == [5]


def test_missing_file_is_empty(store: Store) -> None:
    assert list(export_lines(store.paths.root / "audit.jsonl")) == []


def test_torn_foreign_and_hostile_lines_are_skipped(store: Store, tmp_path: Path) -> None:
    good = '{"seq":1,"ts":"2026-11-01T12:00:00.000Z","level":"info","event":"pass"}'
    evil = r'{"seq":2,"ts":"2026-11-01T12:00:00.000Z","message":"\u001b[2J\u202e"}'
    lines = [
        good,
        '{"seq":2,"ts":"2026-11-01T12:00',  # torn fragment
        "",
        "not json",
        "[1,2]",
        "null",
        '{"seq":"3","ts":"2026-11-01T12:00:00.000Z"}',
        '{"seq":true,"ts":"2026-11-01T12:00:00.000Z"}',
        '{"seq":0,"ts":"2026-11-01T12:00:00.000Z"}',
        '{"seq":4}',
        '{"seq":5,"ts":"yesterday"}',
        '{"seq":6,"ts":"2026-13-45T25:61:61.000Z"}',
        "[" * 100000,
        "\x00\xff\xfe",
        evil,
    ]
    path = store.paths.root / "log.jsonl"
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"))
    out = list(export_lines(path))
    assert seqs(iter(out)) == [1, 2]
    assert out[1].isascii() and "\x1b" not in out[1]  # re-serialized: no raw control characters


def test_a_last_line_without_its_newline_is_not_printed(store: Store) -> None:
    path = store.paths.root / "log.jsonl"
    path.write_text(
        '{"seq":1,"ts":"2026-11-01T12:00:00.000Z"}\n{"seq":2,"ts":"2026-11-01T12:00:00.000Z"}'
    )
    assert seqs(export_lines(path)) == [1]


def test_an_overlong_line_is_skipped_without_reading_it_into_memory(store: Store) -> None:
    path = store.paths.root / "log.jsonl"
    big = b'{"seq":2,"ts":"2026-11-01T12:00:00.000Z","x":"' + b"a" * (3 * 1024 * 1024) + b'"}\n'
    ok = b'{"seq":1,"ts":"2026-11-01T12:00:00.000Z"}\n'
    path.write_bytes(ok + big + ok.replace(b'"seq":1', b'"seq":3'))
    assert seqs(export_lines(path)) == [1, 3]


def test_a_symlink_or_fifo_is_refused(store: Store, tmp_path: Path) -> None:
    from jobsctl.errors import JobsctlError

    target = tmp_path / "other.jsonl"
    target.write_text('{"seq":1,"ts":"2026-11-01T12:00:00.000Z"}\n')
    (store.paths.root / "log.jsonl").symlink_to(target)
    with pytest.raises(JobsctlError):
        list(read_records(store.paths.root / "log.jsonl"))
    os.mkfifo(store.paths.root / "audit.jsonl")
    with pytest.raises(JobsctlError):
        list(read_records(store.paths.root / "audit.jsonl"))


def test_export_while_a_writer_is_appending_never_sees_a_torn_record(store: Store) -> None:
    import threading

    stop = threading.Event()

    def spam() -> None:
        with Store.open(store.paths.root) as other:
            w = RecordWriter(other)
            while not stop.is_set():
                w.log("pass", fields={"message": "x" * 2000})

    thread = threading.Thread(target=spam)
    thread.start()
    try:
        for _ in range(30):
            got = seqs(export_lines(store.paths.root / "log.jsonl"))
            assert got == list(range(1, len(got) + 1))
    finally:
        stop.set()
        thread.join(timeout=30)


def test_since_cutoff_goes_back_from_now() -> None:
    assert since_cutoff(3600, T0) == T0 - dt.timedelta(hours=1)
    with pytest.raises(OverflowError):
        since_cutoff(10**9 * 86400, T0)


# -- the command -------------------------------------------------------------------------------


def cli_lines(capsys: pytest.CaptureFixture[str], *argv: str) -> list[dict[str, object]]:
    res = run_cli(capsys, *argv)
    assert res.code == 0, res.err
    assert res.err == ""
    return [json.loads(line) for line in res.out.splitlines()]


def test_export_log_through_the_cli(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    write_log(store, 3)
    got = cli_lines(capsys, "export", "--log")
    assert [r["seq"] for r in got] == [1, 2, 3]
    for r in got:
        validate_against("log.json", r)
    assert cli_lines(capsys, "--json", "export", "--log") == got  # --json has no effect
    assert [r["seq"] for r in cli_lines(capsys, "export", "--log", "--after-seq", "1")] == [2, 3]
    assert [
        r["seq"]
        for r in cli_lines(
            capsys, "export", "--log", "--since", "2026-11-01T13:00:00Z", "--after-seq", "2"
        )
    ] == [3]


def test_export_audit_through_the_cli(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    w = RecordWriter(store, clock=lambda: T0)
    w.audit(
        action="prune",
        actor="auto:jobsctl",
        target=None,
        result="deleted",
        inputs={"ids": ["jb-aaaaaaaa"], "older_than": 60, "trigger": "command"},
    )
    w.log("pass")
    got = cli_lines(capsys, "export", "--audit")
    assert len(got) == 1 and got[0]["action"] == "prune"
    validate_against("audit.json", got[0])


def test_export_since_duration(
    store: Store, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(store, 4)  # T0 .. T0 + 3h
    now = T0 + dt.timedelta(hours=3, minutes=30)
    monkeypatch.setattr(gateway, "utc_now", lambda: now)
    assert [r["seq"] for r in cli_lines(capsys, "export", "--log", "--since", "90m")] == [3, 4]
    assert [r["seq"] for r in cli_lines(capsys, "export", "--log", "--since", "20m")] == []
    assert [r["seq"] for r in cli_lines(capsys, "export", "--log", "--since", "30m")] == [
        4
    ]  # inclusive
    assert [r["seq"] for r in cli_lines(capsys, "export", "--log", "--since", "1d")] == [1, 2, 3, 4]


def test_export_since_before_year_one_is_a_usage_error(
    store: Store, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "utc_now", lambda: dt.datetime(5, 1, 1, tzinfo=dt.UTC))
    res = run_cli(capsys, "--json", "export", "--log", "--since", "36500d")
    assert res.code == 2
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["code"] == "usage"


def test_export_without_a_state_directory_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "export", "--log")
    assert (res.code, res.out, res.err) == (0, "", "")
    assert not resolve_state_dir().exists()  # it creates nothing


@pytest.mark.parametrize("number", ["NaN", "-Infinity", "Infinity", "1e999", "-1e999", "9" * 5000])
def test_lines_with_numbers_json_cannot_print_are_skipped(
    store: Store, capsys: pytest.CaptureFixture[str], number: str
) -> None:
    path = store.paths.root / "log.jsonl"
    bad = '{"seq":1,"ts":"2026-11-01T12:00:00.000Z","x":' + number + "}\n"
    nested = '{"seq":2,"ts":"2026-11-01T12:00:00.000Z","d":{"x":[' + number + "]}}\n"
    good = '{"seq":3,"ts":"2026-11-01T12:00:00.000Z","level":"info","event":"pass"}\n'
    path.write_text(bad + nested + good)
    assert [r["seq"] for r in cli_lines(capsys, "export", "--log")] == [3]
