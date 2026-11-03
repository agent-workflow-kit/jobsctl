"""``RecordWriter``: record shapes against the schemas, safety, once-per-state warnings."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsctl import log_writer
from jobsctl.errors import JobsctlError
from jobsctl.log_records import (
    AUDIT_ACTIONS,
    AUDIT_RESULTS,
    LEVELS,
    LOG_EVENTS,
    LOG_FIELDS,
    MAX_RECORD_BYTES,
    RecordTooLarge,
    argv_sha256,
)
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import Store
from jobsctl.store_ops import counter_cas, counter_get
from tests.conftest import SCHEMAS, validate_against

pytestmark = pytest.mark.usefixtures("validated_records")

JOB = "jb-aaaaaaaa"
FIXED = dt.datetime(2026, 11, 3, 12, 0, 0, 123456, tzinfo=dt.UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state" / "jobsctl") as s:
        yield s


@pytest.fixture
def writer(store: Store) -> RecordWriter:
    return RecordWriter(store, clock=lambda: FIXED)


def records(store: Store, name: str) -> list[dict[str, Any]]:
    text = (store.paths.root / name).read_text(encoding="ascii")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


TRANSITION: dict[str, Any] = {
    "job_id": JOB,
    "queue": "default",
    "from": "queued",
    "to": "starting",
    "reason": None,
    "hold_reason": None,
    "attempt": 1,
    "actor": "auto:jobsctl",
    "decision_ids": ["d-1"],
}


# -- schema ------------------------------------------------------------------------------------


def test_key_sets_match_the_schemas() -> None:
    log = json.loads((SCHEMAS / "log.json").read_text())
    audit = json.loads((SCHEMAS / "audit.json").read_text())
    assert set(log["properties"]["event"]["enum"]) == LOG_EVENTS
    assert set(log["properties"]["level"]["enum"]) == LEVELS
    assert set(log["properties"]) - {"seq", "ts", "level", "event"} == LOG_FIELDS
    assert set(audit["properties"]["action"]["enum"]) == AUDIT_ACTIONS
    assert set(audit["properties"]["result"]["enum"]) == AUDIT_RESULTS


def test_log_records_validate(writer: RecordWriter, store: Store) -> None:
    writer.log(
        "transition",
        fields={
            **TRANSITION,
            "attempt_status": "failed",
            "attempt_reason": "exit",
            "retry_pending": False,
        },
    )
    writer.log(
        "reorder",
        fields={
            "job_id": JOB,
            "queue": "q",
            "actor": "human:me",
            "decision_ids": [],
            "details": {"move": "top"},
        },
    )
    writer.log(
        "hook_ran",
        fields={
            "job_id": JOB,
            "hook": "pre-start",
            "hook_id": "h",
            "outcome": "veto",
            "exit": 3,
            "decision_ids": ["x"],
            "stderr": "no",
        },
    )
    writer.log(
        "reconcile",
        level="warn",
        fields={"job_id": JOB, "finding": "shim_lost", "action": "requeued"},
    )
    writer.log(
        "label", fields={"job_id": JOB, "queue": "q", "actor": "human:me", "decision_ids": []}
    )
    writer.log("env_file_unsafe", level="warn", fields={"message": "mode 0644"})
    writer.log("unique_key_reused", fields={"job_id": JOB, "details": {"argv_sha256": "0" * 64}})
    for event in sorted(LOG_EVENTS - {"transition", "reorder", "hook_ran", "reconcile", "label"}):
        writer.log(event, level="error")
    for rec in records(store, "log.jsonl"):
        validate_against("log.json", rec)
    assert list(records(store, "log.jsonl")[0])[:4] == ["seq", "ts", "level", "event"]


def test_ts_is_utc_millisecond_precision(writer: RecordWriter, store: Store) -> None:
    writer.log("pass")
    assert records(store, "log.jsonl")[0]["ts"] == "2026-11-03T12:00:00.123Z"


def test_audit_records_validate(writer: RecordWriter, store: Store) -> None:
    inputs = {
        "argv_sha256": argv_sha256(["echo", "hi"]),
        "attempt": 2,
        "unique_key": None,
        "queue": "default",
    }
    for action, result in [
        ("cancel_force", "canceled"),
        ("cancel_held", "canceled"),
        ("release_override", "queued"),
        ("resolve", "done"),
    ]:
        writer.audit(
            action=action,
            actor="human:me",
            target=JOB,
            inputs=inputs,
            result=result,
            decision_id=["d.1", "a/b"],
        )
    writer.audit(
        action="retry",
        actor="human:me",
        target=JOB,
        result="queued",
        inputs={**inputs, "overrides": {"timeout": "none", "max_retries": 3}},
    )
    writer.audit(
        action="prune",
        actor="auto:jobsctl",
        target=None,
        result="deleted",
        inputs={"ids": [JOB], "older_than": 86400, "trigger": "retention"},
    )
    recs = records(store, "audit.jsonl")
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5, 6]
    for rec in recs:
        validate_against("audit.json", rec)
    assert list(recs[0]) == [
        "seq",
        "ts",
        "actor",
        "decision_id",
        "target",
        "action",
        "inputs",
        "result",
    ]


def test_argv_sha256_is_over_each_element_and_a_nul() -> None:
    import hashlib

    assert argv_sha256(["a", "bc"]) == hashlib.sha256(b"a\0bc\0").hexdigest()
    assert argv_sha256(["ab", "c"]) != argv_sha256(["a", "bc"])  # element boundaries count
    assert argv_sha256(["é"]) == hashlib.sha256("é".encode() + b"\0").hexdigest()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "nope", "target": JOB, "result": "done"},
        {"action": "retry", "target": JOB, "result": "bogus"},
        {"action": "prune", "target": JOB, "result": "deleted"},
        {"action": "prune", "target": None, "result": "queued"},
        {"action": "retry", "target": None, "result": "queued"},
        {"action": "retry", "target": "../etc", "result": "queued"},
        {"action": "retry", "target": JOB, "result": "queued", "actor": "root"},
        {"action": "retry", "target": JOB, "result": "queued", "decision_id": ["a b"]},
        {"action": "retry", "target": JOB, "result": "queued", "inputs": {"argv": ["x"]}},
        {"action": "retry", "target": JOB, "result": "queued", "inputs": {"n": {"env": {}}}},
    ],
)
def test_bad_audit_records_are_refused_before_any_write(
    writer: RecordWriter, store: Store, kwargs: dict[str, Any]
) -> None:
    base: dict[str, Any] = {"actor": "human:me", "inputs": {}, "decision_id": []}
    with pytest.raises(ValueError):
        writer.audit(**{**base, **kwargs})
    assert not (store.paths.root / "audit.jsonl").exists()
    assert counter_get_now(store, "seq:audit") == 0


def counter_get_now(store: Store, name: str) -> int:
    with store.read() as tx:
        return counter_get(tx, name)


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        ("nope", {}),
        ("pass", {"bogus": 1}),
        ("pass", {"seq": 99}),
        ("pass", {"ts": "x"}),
        ("pass", {"details": {"argv": ["x"]}}),
        ("pass", {"details": {"ok": [{"Env": {}}]}}),
        ("pass", {"details": {"stdout": "x"}}),
        ("pass", {"details": "text"}),
        ("pass", {"job_id": "nope"}),
        ("pass", {"actor": "someone"}),
        ("pass", {"decision_ids": ["bad id"]}),
        ("pass", {"details": {"n": math.nan}}),
        ("pass", {"details": {"o": object()}}),
        ("pass", {"details": {1: "x"}}),
    ],
)
def test_bad_log_records_are_refused_before_any_write(
    writer: RecordWriter, store: Store, event: str, fields: dict[str, Any]
) -> None:
    with pytest.raises((ValueError, TypeError)):
        writer.log(event, fields=fields)
    assert not (store.paths.root / "log.jsonl").exists()
    assert counter_get_now(store, "seq:log") == 0


def test_bad_level_is_refused(writer: RecordWriter) -> None:
    with pytest.raises(ValueError):
        writer.log("pass", level="debug")


# -- never inside a transaction ----------------------------------------------------------------


def test_no_append_while_a_write_transaction_is_open(writer: RecordWriter, store: Store) -> None:
    with pytest.raises(RuntimeError, match="transaction"):
        with store.write():
            writer.log("pass")
    with pytest.raises(RuntimeError, match="transaction"):
        with store.write():
            writer.audit(
                action="prune", actor="auto:jobsctl", target=None, inputs={}, result="deleted"
            )
    with pytest.raises(RuntimeError, match="transaction"):
        with store.write():
            writer.warn_once("k", "s", "config_invalid")
    with pytest.raises(RuntimeError, match="transaction"):
        with store.read():
            writer.log("pass")
    assert not (store.paths.root / "log.jsonl").exists()
    assert not (store.paths.root / "audit.jsonl").exists()


def test_every_append_happens_with_no_transaction_open(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real = log_writer.append_line

    def spy(fd: int, data: bytes, *, durable: bool) -> None:
        seen.append(store.conn.in_transaction)
        # No write lock is held either: another connection can start a write at once.
        with Store.open(store.paths.root, busy_timeout=0.2) as other, other.write():
            pass
        real(fd, data, durable=durable)

    monkeypatch.setattr(log_writer, "append_line", spy)
    with store.write() as tx:
        tx.after_commit(lambda: writer.log("pass"))
    writer.log("pass")
    assert seen == [False, False]


def test_a_rolled_back_transaction_logs_nothing(writer: RecordWriter, store: Store) -> None:
    with pytest.raises(KeyError), store.write() as tx:
        tx.after_commit(lambda: writer.log("pass"))
        raise KeyError("boom")
    assert not (store.paths.root / "log.jsonl").exists()


def test_after_commit_logging_is_in_commit_order(writer: RecordWriter, store: Store) -> None:
    with store.write() as tx:
        tx.after_commit(lambda: writer.log("pass", fields={"message": "one"}))
        tx.after_commit(lambda: writer.log("pass", fields={"message": "two"}))
    assert [(r["seq"], r["message"]) for r in records(store, "log.jsonl")] == [
        (1, "one"),
        (2, "two"),
    ]


# -- injection and secrets ---------------------------------------------------------------------

HOSTILE = [
    "line1\nline2",
    "cr\rrewrite",
    "\x1b[31mred\x1b[0m",
    "\x1b]0;title\x07",
    "\x9b31m",
    "bidi ‮ text",
    "sep and ",
    "nul\x00byte",
    "lone \ud800 surrogate",
    "undecodable \udcff byte",
    "emoji \U0001f600 ok",
    '"}\n{"seq":999,"event":"forged"}',
    "a" * 10_000,
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_cannot_add_lines_or_control_characters(
    writer: RecordWriter, store: Store, text: str
) -> None:
    writer.log(
        "config_invalid",
        level="error",
        fields={"message": text, "details": {text: [text], "n": {"k": text}}},
    )
    raw = (store.paths.root / "log.jsonl").read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert all(0x20 <= b < 0x7F for b in raw[:-1])  # printable ASCII only, so no ESC or CSI
    rec = json.loads(raw)
    validate_against("log.json", rec)
    assert rec["seq"] == 1 and rec["event"] == "config_invalid"
    assert "\x1b" not in rec["message"] and "\n" not in rec["message"]


@pytest.mark.parametrize(
    "inputs",
    [
        {"queue": "q\n\x1b[2Jy"},
        {"unique_key": 'k\n{"seq":9}'},
        {"argv_sha256": "0" * 63 + "\n"},
        {"note": "x\n\x1b[2Jy"},  # not an audit input
    ],
)
def test_hostile_text_in_audit_inputs_is_refused(
    writer: RecordWriter, store: Store, inputs: dict[str, Any]
) -> None:
    base = {"argv_sha256": "0" * 64, "attempt": 1, "queue": "q", "unique_key": None}
    with pytest.raises(ValueError):
        writer.audit(
            action="retry", actor="human:me", target=JOB, result="queued", inputs={**base, **inputs}
        )
    assert not (store.paths.root / "audit.jsonl").exists()


def test_masked_values_never_reach_the_files(store: Store) -> None:
    secret = "s3cr3t-token-value"
    writer = RecordWriter(store, masked_values=[secret, "short", secret])
    writer.log(
        "env_file_line_invalid",
        level="warn",
        fields={"message": f"bad {secret}!", "details": {"line": 3, secret: f"{secret}{secret}"}},
    )
    writer.audit(
        action="retry",
        actor="human:me",
        target=JOB,
        result="queued",
        decision_id=[],
        inputs={"argv_sha256": "0" * 64, "attempt": 1, "queue": "q", "unique_key": "k:1"},
    )
    writer.log("pass", fields={"message": "a short word"})
    for name in ("log.jsonl", "audit.jsonl"):
        assert secret.encode() not in (store.paths.root / name).read_bytes()
    first = records(store, "log.jsonl")[0]
    assert first["message"] == "bad ***!"
    assert first["details"]["***"] == "******"
    assert records(store, "log.jsonl")[1]["message"] == "a short word"  # under 8 chars: kept


def test_a_masked_value_split_by_the_sanitizer_is_still_masked(store: Store) -> None:
    secret = "tok​en-abcdef"  # holds a zero-width character the sanitizer escapes
    writer = RecordWriter(store, masked_values=[secret])
    writer.log("pass", fields={"message": f"x {secret} y"})
    assert records(store, "log.jsonl")[0]["message"] == "x *** y"


# -- size limits -------------------------------------------------------------------------------


def test_a_huge_log_record_loses_its_details(writer: RecordWriter, store: Store) -> None:
    writer.log("pass", fields={"message": "m", "details": {"k": "x" * MAX_RECORD_BYTES}})
    rec = records(store, "log.jsonl")[0]
    assert rec["details"] == {"truncated": True} and rec["message"] == "m"
    validate_against("log.json", rec)


def test_message_and_stderr_are_clipped(writer: RecordWriter, store: Store) -> None:
    writer.log(
        "hook_ran",
        fields={
            "job_id": JOB,
            "hook": "post-finish",
            "hook_id": "h",
            "outcome": "failed",
            "exit": 1,
            "decision_ids": [],
            "stderr": "e" * 100_000,
            "message": "m" * 100_000,
        },
    )
    rec = records(store, "log.jsonl")[0]
    assert len(rec["stderr"]) == 2048 and len(rec["message"]) == 4096
    validate_against("log.json", rec)


def test_an_oversized_audit_record_is_refused_without_taking_a_seq(
    writer: RecordWriter, store: Store
) -> None:
    ids = [JOB] * (MAX_RECORD_BYTES // 10)
    with pytest.raises(RecordTooLarge):
        writer.audit(
            action="prune",
            actor="auto:jobsctl",
            target=None,
            result="deleted",
            inputs={"ids": ids, "older_than": 1, "trigger": "command"},
        )
    assert counter_get_now(store, "seq:audit") == 0
    assert (
        writer.audit(
            action="prune",
            actor="auto:jobsctl",
            target=None,
            result="deleted",
            inputs={"ids": [JOB], "older_than": 1, "trigger": "command"},
        )
        == 1
    )


# -- files -------------------------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o022, 0o077, 0o277])
def test_files_are_created_private(writer: RecordWriter, store: Store, umask: int) -> None:
    old = os.umask(umask)
    try:
        writer.log("pass")
        writer.audit(
            action="prune",
            actor="auto:jobsctl",
            target=None,
            result="deleted",
            inputs={"ids": [JOB], "older_than": 1, "trigger": "command"},
        )
    finally:
        os.umask(old)
    for name in ("log.jsonl", "audit.jsonl"):
        st = os.stat(store.paths.root / name)
        assert stat.S_IMODE(st.st_mode) == 0o600 and stat.S_ISREG(st.st_mode)


def test_a_loose_existing_file_is_tightened(writer: RecordWriter, store: Store) -> None:
    path = store.paths.root / "log.jsonl"
    path.write_text("")
    path.chmod(0o666)
    writer.log("pass")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_symlinked_file_is_refused_and_its_target_untouched(
    writer: RecordWriter, store: Store, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (store.paths.root / "log.jsonl").symlink_to(victim)
    with pytest.raises(JobsctlError) as err:
        writer.log("pass")
    assert err.value.code == "internal"
    assert victim.read_text() == "keep"
    assert counter_get_now(store, "seq:log") == 0


def test_a_dangling_symlink_is_refused_without_creating_its_target(
    writer: RecordWriter, store: Store, tmp_path: Path
) -> None:
    (store.paths.root / "audit.jsonl").symlink_to(tmp_path / "created-by-attack")
    with pytest.raises(JobsctlError):
        writer.audit(
            action="prune",
            actor="auto:jobsctl",
            target=None,
            result="deleted",
            inputs={"ids": [JOB], "older_than": 1, "trigger": "command"},
        )
    assert not (tmp_path / "created-by-attack").exists()


def test_a_fifo_is_refused_without_blocking(writer: RecordWriter, store: Store) -> None:
    os.mkfifo(store.paths.root / "log.jsonl")
    with pytest.raises(JobsctlError):  # no reader: O_NONBLOCK keeps the open from hanging
        writer.log("pass")


def test_a_directory_is_refused(writer: RecordWriter, store: Store) -> None:
    (store.paths.root / "log.jsonl").mkdir()
    with pytest.raises(JobsctlError):
        writer.log("pass")


# -- failures and rotation ---------------------------------------------------------------------


def test_a_failed_append_gives_its_number_back(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = log_writer.append_line
    monkeypatch.setattr(
        log_writer,
        "append_line",
        lambda *a, **k: (_ for _ in ()).throw(JobsctlError("internal", "disk")),
    )
    with pytest.raises(JobsctlError):
        writer.log("pass")
    monkeypatch.setattr(log_writer, "append_line", real)
    assert writer.log("pass") == 1
    assert [r["seq"] for r in records(store, "log.jsonl")] == [1]


def test_a_failed_write_cuts_back_a_partial_line(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer.log("pass")
    real_write = os.write
    calls = {"n": 0}

    def flaky(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[:10])  # a short write
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", flaky)
    with pytest.raises(JobsctlError, match="cannot append"):
        writer.log("pass")
    monkeypatch.undo()
    assert writer.log("pass") == 2
    assert [r["seq"] for r in records(store, "log.jsonl")] == [1, 2]


def test_a_short_write_is_completed(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:7]))
    writer.log("pass")
    monkeypatch.undo()
    assert [r["seq"] for r in records(store, "log.jsonl")] == [1]


def test_a_torn_last_line_is_terminated_before_the_next_record(
    writer: RecordWriter, store: Store
) -> None:
    writer.log("pass")
    with open(store.paths.root / "log.jsonl", "ab") as f:
        f.write(b'{"seq":2,"ts":"2026-11-03T12:00')  # a crashed writer's fragment
    writer.log("pass", fields={"message": "next"})
    lines = (store.paths.root / "log.jsonl").read_bytes().split(b"\n")
    assert lines[1].startswith(b'{"seq":2,"ts":"2026-11-03T12:00') and not lines[1].endswith(b"}")
    assert json.loads(lines[2])["message"] == "next"


def test_the_counter_survives_external_rotation(writer: RecordWriter, store: Store) -> None:
    writer.log("pass")
    writer.log("pass")
    path = store.paths.root / "log.jsonl"
    path.rename(store.paths.root / "log.jsonl.1")
    assert writer.log("pass") == 3  # the new file starts at 3, not 1
    assert [r["seq"] for r in records(store, "log.jsonl")] == [3]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.unlink()  # removal too
    assert writer.log("pass") == 4


def test_another_writer_object_continues_the_numbering(writer: RecordWriter, store: Store) -> None:
    writer.log("pass")
    with Store.open(store.paths.root) as other:
        assert RecordWriter(other).log("pass") == 2


# -- once per state ----------------------------------------------------------------------------


def warn(writer: RecordWriter, state: str | None, key: str = "env_file_unsafe:/x") -> bool:
    return writer.warn_once(key, state, "env_file_unsafe", fields={"message": f"state {state}"})


def test_a_warning_is_written_once_and_again_only_after_a_change(
    writer: RecordWriter, store: Store
) -> None:
    assert warn(writer, "mode 0644") is True
    assert warn(writer, "mode 0644") is False
    assert warn(writer, "mode 0644") is False
    assert warn(writer, "mode 0666") is True  # the state changed
    assert warn(writer, "mode 0666") is False
    assert [r["message"] for r in records(store, "log.jsonl")] == [
        "state mode 0644",
        "state mode 0666",
    ]
    for rec in records(store, "log.jsonl"):
        validate_against("log.json", rec)
        assert rec["level"] == "warn"


def test_a_cleared_state_rearms_the_warning(writer: RecordWriter, store: Store) -> None:
    assert warn(writer, "bad") is True
    assert warn(writer, None) is False  # fixed: nothing is logged
    assert warn(writer, None) is False
    assert warn(writer, "bad") is True  # broken again, same state as before: logged again
    assert len(records(store, "log.jsonl")) == 2


def test_warning_keys_are_independent(writer: RecordWriter, store: Store) -> None:
    assert warn(writer, "bad", key="a") is True
    assert warn(writer, "bad", key="b") is True
    assert warn(writer, "bad", key="a") is False


def test_the_warning_state_is_shared_by_every_process_through_the_store(
    writer: RecordWriter, store: Store
) -> None:
    assert warn(writer, "bad") is True
    with Store.open(store.paths.root) as other:
        again = RecordWriter(other)
        assert warn(again, "bad") is False
        assert warn(again, "worse") is True
    assert warn(writer, "worse") is False


def test_warning_keys_and_states_may_hold_anything(writer: RecordWriter, store: Store) -> None:
    key = "path_entry_dropped:" + "\n\x1b[2J'; DROP TABLE counters;--" * 50
    assert warn(writer, "x" * 100_000 + "\n", key=key) is True
    assert warn(writer, "x" * 100_000 + "\n", key=key) is False
    with store.read() as tx:
        names = [r["name"] for r in tx.all("SELECT name FROM counters")]
    assert all(len(n) <= 64 for n in names)


@pytest.mark.parametrize(
    "exc", [JobsctlError("internal", "disk full"), OSError(28, "No space left on device")]
)
def test_a_warning_whose_append_fails_is_written_by_the_next_call(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    real = log_writer.append_line

    def fail(*_a: object, **_k: object) -> None:
        raise exc

    monkeypatch.setattr(log_writer, "append_line", fail)
    with pytest.raises(type(exc)):
        warn(writer, "bad")
    monkeypatch.setattr(log_writer, "append_line", real)
    assert warn(writer, "bad") is True  # the process survived: the earlier state was restored
    assert warn(writer, "bad") is False
    assert [r["message"] for r in records(store, "log.jsonl")] == ["state bad"]


def test_a_failed_append_restores_the_previous_state_not_zero(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert warn(writer, "first") is True
    real = log_writer.append_line
    monkeypatch.setattr(
        log_writer,
        "append_line",
        lambda *a, **k: (_ for _ in ()).throw(JobsctlError("internal", "x")),
    )
    with pytest.raises(JobsctlError):
        warn(writer, "second")
    monkeypatch.setattr(log_writer, "append_line", real)
    assert warn(writer, "first") is False  # still "first": not re-armed by the failure
    assert warn(writer, "second") is True


def test_an_error_after_a_completed_append_keeps_the_warning_state(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = log_writer.locked

    @contextlib.contextmanager
    def interrupted_on_release(path: Path) -> Iterator[int]:
        with real(path) as fd:
            yield fd
        raise KeyboardInterrupt  # after the line was written and the lock released

    monkeypatch.setattr(log_writer, "locked", interrupted_on_release)
    with pytest.raises(KeyboardInterrupt):
        warn(writer, "bad")
    monkeypatch.setattr(log_writer, "locked", real)
    assert warn(writer, "bad") is False  # the state stayed recorded: not logged twice
    assert [r["message"] for r in records(store, "log.jsonl")] == ["state bad"]


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        ("no_such_event", {"message": "m"}),
        ("env_file_unsafe", {"bogus": 1}),
        ("env_file_unsafe", {"details": {"argv": "x"}}),
        ("env_file_unsafe", {"job_id": "nope"}),
    ],
)
def test_a_refused_warning_does_not_record_its_state(
    writer: RecordWriter, store: Store, event: str, fields: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        writer.warn_once("k", "s", event, fields=fields)
    assert writer.warn_once("k", "s", "env_file_unsafe", fields={"message": "m"}) is True
    assert writer.warn_once("k", "s", "env_file_unsafe", fields={"message": "m"}) is False
    assert len(records(store, "log.jsonl")) == 1


def test_a_crash_after_recording_loses_the_warning_rather_than_repeating_it(
    writer: RecordWriter, store: Store
) -> None:
    # A dead process cannot restore the state: record it as warn_once does, then append nothing.
    with store.write() as tx:
        counter_cas(tx, log_writer.warn_counter_name("k"), 0, log_writer._state_fingerprint("s"))
    assert writer.warn_once("k", "s", "env_file_unsafe") is False
    assert not (store.paths.root / "log.jsonl").exists()


def test_warning_keys_and_states_with_lone_surrogates(writer: RecordWriter) -> None:
    assert warn(writer, "\ud800 state", key="\ud800 key") is True
    assert warn(writer, "\ud800 state", key="\ud800 key") is False


# -- review round 1 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "ARGV\x1b[0m",
        "sec\x1b[mret",
        "pass-env",
        "env-file",
        "STDERR",
        "ａrgv",  # fullwidth a
        "stdout ",
        " ENVIRON\t",
        "output​",
        "ta­il",  # soft hyphen (a format character)
        "val\nues",
    ],
)
def test_forbidden_keys_are_refused_after_normalization(
    writer: RecordWriter, store: Store, key: str
) -> None:
    with pytest.raises(ValueError, match="must not carry"):
        writer.log("pass", fields={"details": {"ok": {key: "x"}}})
    with pytest.raises(ValueError, match="must not carry"):
        writer.audit(
            action="retry",
            actor="human:me",
            target=JOB,
            result="queued",
            inputs={key: "x", "argv_sha256": "0" * 64, "attempt": 1, "queue": "q"},
        )
    assert not (store.paths.root / "log.jsonl").exists()
    assert not (store.paths.root / "audit.jsonl").exists()


def test_allowed_keys_close_to_forbidden_ones_are_kept(writer: RecordWriter, store: Store) -> None:
    writer.log("pass", fields={"details": {"argv_sha256": "0" * 64, "env_name": "X", "vars": 1}})
    assert set(records(store, "log.jsonl")[0]["details"]) == {"argv_sha256", "env_name", "vars"}


TRANSITION_EXTRA: dict[str, Any] = {
    "attempt_status": "failed",
    "attempt_reason": "exit",
    "retry_pending": True,
    "to": "queued",
}
HOOK_RAN: dict[str, Any] = {
    "job_id": JOB,
    "hook": "post-fail",
    "hook_id": "notify",
    "outcome": "failed",
    "exit": 1,
    "decision_ids": [],
}
LABEL: dict[str, Any] = {"job_id": JOB, "queue": "q", "actor": "human:me", "decision_ids": []}
RECONCILE: dict[str, Any] = {"job_id": None, "finding": "pruned", "action": "deleted"}
MINIMAL: dict[str, dict[str, Any]] = {
    "transition": TRANSITION,
    "reorder": LABEL,
    "label": LABEL,
    "hook_ran": HOOK_RAN,
    "reconcile": RECONCILE,
}


def test_every_event_has_a_minimal_valid_record(writer: RecordWriter, store: Store) -> None:
    for event in sorted(LOG_EVENTS):
        writer.log(event, fields=MINIMAL.get(event, {}))
    recs = records(store, "log.jsonl")
    assert [r["event"] for r in recs] == sorted(LOG_EVENTS)
    for rec in recs:
        validate_against("log.json", rec)
    writer.log("transition", fields={**TRANSITION, **TRANSITION_EXTRA})
    writer.log("hook_ran", fields={**HOOK_RAN, "outcome": "timeout", "exit": None, "stderr": "t"})
    for rec in records(store, "log.jsonl"):
        validate_against("log.json", rec)


def _without(fields: dict[str, Any], name: str) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k != name}


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        # transition: required, non-null, types and enums
        *[("transition", _without(TRANSITION, name)) for name in TRANSITION],
        *[
            ("transition", {**TRANSITION, name: None})
            for name in ("job_id", "queue", "from", "to", "attempt", "actor")
        ],
        ("transition", {**TRANSITION, "attempt": "x"}),
        ("transition", {**TRANSITION, "attempt": -1}),
        ("transition", {**TRANSITION, "attempt": True}),
        ("transition", {**TRANSITION, "reason": "bogus"}),
        ("transition", {**TRANSITION, "hold_reason": "later"}),
        ("transition", {**TRANSITION, "to": "paused"}),
        ("transition", {**TRANSITION, "from": "deleted"}),
        ("transition", {**TRANSITION, "queue": "Bad Queue"}),
        ("transition", {**TRANSITION, "decision_ids": "d-1"}),
        ("transition", {**TRANSITION, **TRANSITION_EXTRA, "retry_pending": "yes"}),
        ("transition", {**TRANSITION, **TRANSITION_EXTRA, "attempt_status": "running"}),
        ("transition", {**TRANSITION, **TRANSITION_EXTRA, "attempt_reason": "bogus"}),
        ("transition", {**TRANSITION, **TRANSITION_EXTRA, "to": "starting"}),
        ("transition", {**TRANSITION, **TRANSITION_EXTRA, "attempt_reason": "canceled"}),
        ("transition", {**TRANSITION, **TRANSITION_EXTRA, "attempt_status": "done"}),
        ("transition", {**TRANSITION, "attempt_status": "done"}),  # dependentRequired
        ("transition", {**TRANSITION, "attempt_reason": None}),
        ("transition", {**TRANSITION, "retry_pending": False}),
        ("transition", {**TRANSITION, "finding": "pruned"}),
        # reorder and label
        *[(ev, _without(LABEL, name)) for ev in ("reorder", "label") for name in LABEL],
        *[(ev, {**LABEL, name: None}) for ev in ("reorder", "label") for name in LABEL],
        ("label", {**LABEL, "actor": "root"}),
        ("reorder", {**LABEL, "attempt_status": "done"}),
        # hook_ran
        *[("hook_ran", _without(HOOK_RAN, name)) for name in HOOK_RAN],
        *[("hook_ran", {**HOOK_RAN, name: None}) for name in ("job_id", "hook", "hook_id")],
        ("hook_ran", {**HOOK_RAN, "outcome": None}),
        ("hook_ran", {**HOOK_RAN, "hook": "pre-flight"}),
        ("hook_ran", {**HOOK_RAN, "hook_id": ""}),
        ("hook_ran", {**HOOK_RAN, "outcome": "maybe"}),
        ("hook_ran", {**HOOK_RAN, "exit": 256}),
        ("hook_ran", {**HOOK_RAN, "exit": "1"}),
        ("hook_ran", {**HOOK_RAN, "stderr": 5}),
        ("hook_ran", {**HOOK_RAN, "outcome": "allow", "exit": 0, "stderr": "x"}),
        ("hook_ran", {**HOOK_RAN, "outcome": "timeout", "exit": 1}),
        ("pass", {"stderr": "x"}),
        ("pass", {"exit": 1}),
        ("pass", {"hook": "pre-start"}),
        # reconcile
        *[("reconcile", _without(RECONCILE, name)) for name in RECONCILE],
        ("reconcile", {**RECONCILE, "finding": None}),
        ("reconcile", {**RECONCILE, "action": "nuked"}),
        ("reconcile", {**RECONCILE, "finding": "gone"}),
        ("pass", {"finding": "pruned"}),
        ("pass", {"action": "deleted"}),
        # every event: field types
        ("pass", {"message": 5}),
        ("pass", {"queue": "x" * 65}),
        ("pass", {"attempt": 1.5}),
        ("pass", {"from": "nope"}),
        ("pass", {"reason": "bogus"}),
    ],
)
def test_records_that_break_the_log_schema_are_refused(
    writer: RecordWriter, store: Store, event: str, fields: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        writer.log(event, fields=fields)
    assert not (store.paths.root / "log.jsonl").exists()
    assert counter_get_now(store, "seq:log") == 0


AUDIT_INPUTS: dict[str, Any] = {
    "argv_sha256": "0" * 64,
    "attempt": 0,
    "unique_key": None,
    "queue": "q",
}
PRUNE_INPUTS: dict[str, Any] = {"ids": [JOB], "older_than": 60, "trigger": "command"}


@pytest.mark.parametrize(
    ("action", "inputs"),
    [
        *[("retry", _without(AUDIT_INPUTS, name)) for name in AUDIT_INPUTS],
        ("retry", {**AUDIT_INPUTS, "argv_sha256": "A" * 64}),
        ("retry", {**AUDIT_INPUTS, "attempt": -1}),
        ("retry", {**AUDIT_INPUTS, "attempt": False}),
        ("retry", {**AUDIT_INPUTS, "unique_key": "has space"}),
        ("retry", {**AUDIT_INPUTS, "unique_key": ""}),
        ("retry", {**AUDIT_INPUTS, "queue": None}),
        ("retry", {**AUDIT_INPUTS, "ids": [JOB]}),
        ("retry", {**AUDIT_INPUTS, "overrides": {}}),
        ("retry", {**AUDIT_INPUTS, "overrides": {"timeout": 0}}),
        ("retry", {**AUDIT_INPUTS, "overrides": {"timeout": "forever"}}),
        ("retry", {**AUDIT_INPUTS, "overrides": {"max_retries": 101}}),
        ("retry", {**AUDIT_INPUTS, "overrides": {"kill_grace": 0}}),
        ("retry", {**AUDIT_INPUTS, "overrides": {"cap": 1}}),
        ("resolve", {**AUDIT_INPUTS, "overrides": {"max_retries": 1}}),
        *[("prune", _without(PRUNE_INPUTS, name)) for name in PRUNE_INPUTS],
        ("prune", {**PRUNE_INPUTS, "ids": []}),
        ("prune", {**PRUNE_INPUTS, "ids": ["nope"]}),
        ("prune", {**PRUNE_INPUTS, "older_than": 0}),
        ("prune", {**PRUNE_INPUTS, "trigger": "cron"}),
        ("prune", {**PRUNE_INPUTS, "queue": "q"}),
    ],
)
def test_audit_inputs_that_break_the_schema_are_refused(
    writer: RecordWriter, store: Store, action: str, inputs: dict[str, Any]
) -> None:
    prune = action == "prune"
    with pytest.raises(ValueError):
        writer.audit(
            action=action,
            actor="human:me",
            target=None if prune else JOB,
            result="deleted" if prune else "done",
            inputs=inputs,
        )
    assert counter_get_now(store, "seq:audit") == 0


def test_audit_target_and_result_follow_the_schema(writer: RecordWriter) -> None:
    with pytest.raises(ValueError):  # deleted is only for prune
        writer.audit(
            action="resolve", actor="human:me", target=JOB, result="deleted", inputs=AUDIT_INPUTS
        )


def test_masking_leaves_identifier_and_enum_fields_intact(
    store: Store,
) -> None:
    writer = RecordWriter(store, masked_values=["starting", "deadbeef", "jobsctl-"])
    writer.log(
        "transition",
        fields={
            **TRANSITION,
            "queue": "deadbeef",
            "actor": "auto:deadbeef",
            "decision_ids": ["deadbeef", "starting"],
            "message": "now starting deadbeef",
            "details": {"k": "starting", "deadbeef": 1},
        },
    )
    writer.log("hook_ran", fields={**HOOK_RAN, "hook_id": "run deadbeef", "stderr": "deadbeef!"})
    writer.audit(
        action="retry",
        actor="human:deadbeef",
        target=JOB,
        result="starting",
        decision_id=["deadbeef"],
        inputs={**AUDIT_INPUTS, "argv_sha256": "deadbeef" * 8, "queue": "deadbeef"},
    )
    first, second = records(store, "log.jsonl")
    assert (first["to"], first["queue"], first["actor"]) == (
        "starting",
        "deadbeef",
        "auto:deadbeef",
    )
    assert first["decision_ids"] == ["deadbeef", "starting"]
    assert first["message"] == "now *** ***"
    assert first["details"] == {"k": "***", "***": 1}
    assert (second["hook_id"], second["stderr"]) == ("run ***", "***!")
    audit = records(store, "audit.jsonl")[0]
    assert audit["inputs"]["argv_sha256"] == "deadbeef" * 8
    assert (audit["actor"], audit["result"], audit["decision_id"]) == (
        "human:deadbeef",
        "starting",
        ["deadbeef"],
    )


def test_a_failed_write_never_extends_a_file_that_shrank_meanwhile(
    writer: RecordWriter, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer.log("pass", fields={"message": "x" * 500})
    path = store.paths.root / "log.jsonl"

    def shrink_then_fail(fd: int, data: bytes) -> int:
        os.truncate(path, 0)  # an external copytruncate between the size check and the write
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", shrink_then_fail)
    with pytest.raises(JobsctlError):
        writer.log("pass")
    monkeypatch.undo()
    assert path.stat().st_size == 0  # not padded back to the old length with NUL bytes


def test_details_keys_that_clean_to_the_same_text_are_kept_apart(store: Store) -> None:
    writer = RecordWriter(store, masked_values=["secretvalue1", "secretvalue2"])
    writer.log(
        "pass",
        fields={
            "details": {
                "a\n": 1,
                "a\\n": 2,
                "a\\n~2": 3,
                "secretvalue1": 4,
                "secretvalue2": 5,
                "n": {"x\r": 6, "x\\r": 7},
            }
        },
    )
    assert records(store, "log.jsonl")[0]["details"] == {
        "a\\n": 1,
        "a\\n~2": 2,
        "a\\n~2~2": 3,
        "***": 4,
        "***~2": 5,
        "n": {"x\\r": 6, "x\\r~2": 7},
    }


def test_argv_sha256_accepts_every_python_string() -> None:
    import hashlib

    assert argv_sha256(["\udcff"]) == hashlib.sha256(b"\xff\0").hexdigest()  # os.fsencode form
    assert argv_sha256(["\ud800"]) == hashlib.sha256(b"\xed\xa0\x80\0").hexdigest()
    assert (
        argv_sha256(["a", "\udcff\ud800"])
        == hashlib.sha256(  # surrogatepass for both
            b"a\0\xed\xb3\xbf\xed\xa0\x80\0"
        ).hexdigest()
    )


def test_free_text_fields_are_clipped_and_identifiers_are_grammar_checked(
    writer: RecordWriter, store: Store
) -> None:
    writer.log("hook_ran", fields={**HOOK_RAN, "hook_id": "h" * 300_000})
    assert len(records(store, "log.jsonl")[0]["hook_id"]) == 4096
    with pytest.raises(ValueError) as err:
        writer.log("unknown_queue_selector", level="warn", fields={"queue": "q" * 300_000})
    assert not isinstance(err.value, RecordTooLarge)  # refused by the grammar, not by the size
    writer.log("unknown_queue_selector", level="warn", fields={"details": {"q" * 300_000: 1}})
    assert records(store, "log.jsonl")[1]["details"] == {"truncated": True}
    assert counter_get_now(store, "seq:log") == 2
