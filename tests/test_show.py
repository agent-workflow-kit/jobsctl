"""`jobsctl show`: selection, attempts, output tails, the current-attempt rule, sanitizing."""

from __future__ import annotations

import datetime as dt
import zlib
from typing import Any

import pytest

from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.view_helpers import (
    CLAIMED,
    ENDED,
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


def show(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    res = run_cli(capsys, "--json", "show", *args)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/show.json", doc)
    return doc


def fail(capsys: pytest.CaptureFixture[str], *args: str) -> Result:
    res = run_cli(capsys, "--json", "show", *args)
    assert res.code != 0
    return res


def finished(**over: Any) -> dict[str, Any]:
    return ran(**over)


def tail(text: bytes | str, **over: Any) -> dict[str, Any]:
    return {"tail": text, "bytes_seen": len(text), "truncated": 0, **over}


# -- selection -------------------------------------------------------------------------------


def test_show_by_id_and_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22")
    seed_job("jb-bbbbbb22")
    assert show(capsys, "jb-aaaaaa22")["id"] == "jb-aaaaaa22"
    assert show(capsys, "aaaa")["id"] == "jb-aaaaaa22"
    assert show(capsys, "jb-bbbb")["id"] == "jb-bbbbbb22"


def test_unknown_ambiguous_and_malformed_ids(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22")
    seed_job("jb-aaaaaa23")
    res = fail(capsys, "jb-cccccc22")
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"
    res = fail(capsys, "aaaa")
    assert res.code == 21 and res.envelope()["error"]["candidates"] == [
        "jb-aaaaaa22",
        "jb-aaaaaa23",
    ]
    assert fail(capsys, "!!").code == 2


def test_id_xor_unique_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert fail(capsys).code == 2
    assert fail(capsys, "jb-aaaaaa22", "--unique-key", "K").code == 2


def test_unique_key_prefers_the_non_terminal_holder(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", unique_key="K", created_at=ago(5 * H))
    seed_finished("jb-aaaaaa23", "failed", "exit", ago(H), unique_key="K", created_at=ago(1 * H))
    assert show(capsys, "--unique-key", "K")["id"] == "jb-aaaaaa22"


def test_unique_key_without_holder_picks_the_most_recently_created(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for job_id, created in (("jb-aaaaaa22", 5), ("jb-aaaaaa23", 1), ("jb-aaaaaa24", 3)):
        seed_finished(job_id, "done", "exit", ago(H), unique_key="K", created_at=ago(created * H))
    assert show(capsys, "--unique-key", "K")["id"] == "jb-aaaaaa23"


def test_unique_key_created_at_tie_takes_the_later_inserted_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for job_id in ("jb-aaaaaa24", "jb-aaaaaa22"):
        seed_finished(job_id, "done", "exit", ago(H), unique_key="K", created_at=ago(H))
    assert show(capsys, "--unique-key", "K")["id"] == "jb-aaaaaa22"


def test_unique_key_not_found_is_exit_10(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", unique_key="other")
    res = fail(capsys, "--unique-key", "K")
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"


def test_show_on_an_empty_state_is_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    assert fail(capsys, "jb-aaaaaa22").code == 10


# -- attempts and output ---------------------------------------------------------------------


def test_attempts_oldest_first_and_empty_before_the_first_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job("jb-aaaaaa22")
    doc = show(capsys, "jb-aaaaaa22")
    assert doc["attempts"] == [] and doc["output"] is None
    seed_job(
        "jb-aaaaaa23",
        status="done",
        reason="exit",
        attempt=3,
        ended_at=ENDED,
        attempts=[
            finished(),
            finished(),
            finished(status="done", code=0),
        ],
    )
    got = show(capsys, "jb-aaaaaa23")["attempts"]
    assert [a["attempt"] for a in got] == [1, 2, 3]
    assert [a["status"] for a in got] == ["failed", "failed", "done"]


def test_output_is_the_current_attempts_tail(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=2,
        ended_at=ENDED,
        attempts=[finished(), finished()],
        output=tail(b"two\n", truncated=1),
        output_attempt=2,
    )
    doc = show(capsys, "jb-aaaaaa22")
    assert doc["output"] == {
        "tail": "two\n",
        "bytes_seen": 4,
        "truncated": True,
        "updated_at": "2026-03-10T11:00:00.000Z",
    }


def test_attempt_flag_selects_another_attempts_tail(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=2,
        ended_at=ENDED,
        attempts=[finished(), finished()],
        output=tail(b"one\n"),
        output_attempt=1,
    )
    assert show(capsys, "jb-aaaaaa22")["output"] is None  # the current attempt has none
    assert show(capsys, "jb-aaaaaa22", "--attempt", "1")["output"]["tail"] == "one\n"
    assert show(capsys, "jb-aaaaaa22", "--attempt", "2")["output"] is None


def test_attempt_that_does_not_exist_is_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=1,
        ended_at=ENDED,
        attempts=[finished()],
    )
    for flags in (("--attempt", "2"), ("--attempt", "2", "--no-output")):
        res = fail(capsys, "jb-aaaaaa22", *flags)
        assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"
    seed_job("jb-aaaaaa23")
    assert fail(capsys, "jb-aaaaaa23", "--attempt", "1").code == 10


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_attempt_below_one_is_a_usage_error(capsys: pytest.CaptureFixture[str], value: str) -> None:
    seed_job("jb-aaaaaa22")
    assert fail(capsys, "jb-aaaaaa22", "--attempt", value).code == 2


def test_attempt_not_found_after_id_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    assert fail(capsys, "jb-aaaaaa22", "--attempt", "9").code == 10


def test_no_output_gives_null_output(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=1,
        ended_at=ENDED,
        attempts=[finished()],
        output=tail(b"UNIQUETAIL"),
    )
    assert show(capsys, "jb-aaaaaa22", "--no-output")["output"] is None
    assert "UNIQUETAIL" not in run_cli(capsys, "show", "jb-aaaaaa22", "--no-output").out
    assert "UNIQUETAIL" in run_cli(capsys, "show", "jb-aaaaaa22").out


def test_requeued_job_with_an_old_attempt_shows_it_by_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job("jb-aaaaaa22", attempt=1, attempts=[finished()], output=tail(b"old\n"))
    doc = show(capsys, "jb-aaaaaa22")
    assert doc["output"] is None and len(doc["attempts"]) == 1
    assert show(capsys, "jb-aaaaaa22", "--attempt", "1")["output"]["tail"] == "old\n"


def test_undecodable_tail_bytes_print_as_backslash_x(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=1,
        ended_at=ENDED,
        attempts=[finished()],
        output=tail(b"ok \xff\xfe end\n"),
    )
    assert show(capsys, "jb-aaaaaa22")["output"]["tail"] == "ok \\xff\\xfe end\n"


def zipped(data: bytes, **over: Any) -> dict[str, Any]:
    """An `outputs` row the way output capture stores a tail above 4 KiB."""
    return tail(zlib.compress(data), compressed=1, bytes_seen=len(data) + 7, truncated=1, **over)


FAILED_ATTEMPT = {"status": "failed", "reason": "exit", "attempt": 1, "ended_at": ENDED}


def test_a_compressed_tail_is_shown_in_json(capsys: pytest.CaptureFixture[str]) -> None:
    text = b"abcdefghij\n" * 1000
    seed_job("jb-aaaaaa22", **FAILED_ATTEMPT, attempts=[finished()], output=zipped(text))
    out = show(capsys, "jb-aaaaaa22")["output"]
    assert out["tail"] == text.decode()
    assert (out["bytes_seen"], out["truncated"]) == (len(text) + 7, True)
    assert out["updated_at"] == "2026-03-10T11:00:00.000Z"


def test_a_compressed_tail_is_shown_in_human_output(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", **FAILED_ATTEMPT, attempts=[finished()], output=zipped(b"zed\n" * 2000))
    res = run_cli(capsys, "show", "jb-aaaaaa22")
    assert res.code == 0 and res.out.count("zed") == 2000


def test_a_compressed_tail_is_shown_by_attempt_flag(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="queued",
        attempt=2,
        attempts=[finished(), finished()],
        output=zipped(b"first\n" * 900),
        output_attempt=1,
    )
    assert show(capsys, "jb-aaaaaa22")["output"] is None
    assert show(capsys, "jb-aaaaaa22", "--attempt", "1")["output"]["tail"] == "first\n" * 900


def test_an_undecodable_byte_in_a_compressed_tail_prints_as_backslash_x(
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = b"ok \xff\xfe end\n" + b"x" * 5000
    seed_job("jb-aaaaaa22", **FAILED_ATTEMPT, attempts=[finished()], output=zipped(data))
    assert show(capsys, "jb-aaaaaa22")["output"]["tail"] == "ok \\xff\\xfe end\n" + "x" * 5000


def test_a_compressed_tail_is_sanitized_again(capsys: pytest.CaptureFixture[str]) -> None:
    data = b"\x1b[31mred\x1b[0m \x1b]0;title\x07rest\n" + b"x" * 5000
    seed_job("jb-aaaaaa22", **FAILED_ATTEMPT, attempts=[finished()], output=zipped(data))
    assert show(capsys, "jb-aaaaaa22")["output"]["tail"] == "red rest\n" + "x" * 5000
    assert "\x1b" not in run_cli(capsys, "show", "jb-aaaaaa22").out


@pytest.mark.parametrize(
    ("blob", "compressed"),
    [
        (b"not zlib at all", 1),
        (zlib.compress(b"x" * 6000)[:-6], 1),
        (zlib.compress(b"x" * 6000) + b"trailing", 1),
        (zlib.compress(b"x" * 300000), 1),
        (b"a" * 262145, 0),
        ("text", 0),
    ],
    ids=[
        "garbage",
        "truncated",
        "trailing-data",
        "expands-beyond-maximum",
        "uncompressed-beyond-maximum",
        "non-blob",
    ],
)
def test_an_unreadable_stored_tail_is_an_internal_error(
    capsys: pytest.CaptureFixture[str], blob: bytes | str, compressed: int
) -> None:
    seed_job(
        "jb-aaaaaa22",
        **FAILED_ATTEMPT,
        attempts=[finished()],
        output=tail(blob, compressed=compressed),
    )
    res = fail(capsys, "jb-aaaaaa22")
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    assert run_cli(capsys, "show", "--no-output", "jb-aaaaaa22").code == 0


# -- the current-attempt rule ----------------------------------------------------------------

ESCALATED = {
    "status": "held",
    "hold_reason": "escalated",
    "claimed_at": CLAIMED,
    "ended_at": ENDED,
    "escalated_hooks": ["h"],
    "hook_messages": [{"hook_id": "h", "outcome": "escalate", "message": "ask"}],
}
FAILED_JOB = {"status": "failed", "reason": "exit", "attempt": 1, "ended_at": ENDED}
CANCELED_JOB = {"status": "canceled", "reason": "canceled", "attempt": 1, "ended_at": ENDED}

CASES: list[tuple[str, dict[str, Any], dict[str, Any], bool]] = [
    ("queued after a retry", {"status": "queued", "attempt": 1}, finished(), False),
    ("manual hold", {"status": "held", "hold_reason": "manual", "attempt": 1}, finished(), False),
    (
        "exit 13 hold",
        {"status": "held", "hold_reason": "exit_13", "attempt": 1},
        finished(status="held", reason=None, code=13, hold_reason="exit_13"),
        True,
    ),
    (
        "escalated hold",
        {"status": "held", "hold_reason": "escalated", "attempt": 1},
        ESCALATED,
        True,
    ),
    ("running", {"status": "running", "attempt": 1}, running_attempt(), True),
    ("starting", {"status": "starting", "attempt": 1}, starting_attempt(), True),
    ("failed", FAILED_JOB, finished(), True),
    (
        "resolved from a hold",
        {**FAILED_JOB, "status": "done", "reason": "resolved", "resolved_by": "human:me"},
        finished(status="done", reason="resolved", code=13, resolved_by="human:me"),
        True,
    ),
    ("canceled while queued after a failed attempt", CANCELED_JOB, finished(), False),
    (
        "canceled while queued after a forced attempt",
        CANCELED_JOB,
        finished(status="canceled", reason="forced", force_requested_by="human:me"),
        False,
    ),
    (
        "canceled while queued after a retry of a canceled attempt",
        {**CANCELED_JOB, "retried_from_attempt": 1},
        starting_attempt(status="canceled", reason="canceled", ended_at=ENDED),
        False,
    ),
    (
        "canceled in starting",
        CANCELED_JOB,
        starting_attempt(status="canceled", reason="canceled", ended_at=ENDED),
        True,
    ),
]


@pytest.mark.parametrize(
    ("name", "job", "attempt", "has_current"), CASES, ids=[c[0] for c in CASES]
)
def test_current_attempt_rule(
    capsys: pytest.CaptureFixture[str],
    name: str,
    job: dict[str, Any],
    attempt: dict[str, Any],
    has_current: bool,
) -> None:
    seed_job("jb-aaaaaa22", attempts=[attempt], **job)
    doc = show(capsys, "jb-aaaaaa22")
    assert (doc["claimed_at"] is not None) is has_current
    assert doc["exit"] == (attempt.get("exit") if has_current else None)
    assert len(doc["attempts"]) == 1  # the attempts list is complete either way


def test_list_and_show_agree_on_the_current_attempt(capsys: pytest.CaptureFixture[str]) -> None:
    for i, (_, job, attempt, _) in enumerate(CASES):
        seed_job(f"jb-aaaaa{chr(97 + i)}22", attempts=[attempt], **job)
    listed = {j["id"]: j for j in run_cli_json(capsys, "list", "--all")["jobs"]}
    for job_id, row in listed.items():
        shown = show(capsys, job_id)
        assert {k: v for k, v in shown.items() if k not in ("attempts", "output")} == row


def run_cli_json(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    res = run_cli(capsys, "--json", *argv)
    assert res.code == 0, res.out
    return res.envelope()


# -- fields ----------------------------------------------------------------------------------


def test_attempt_fields_and_golden(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="held",
        hold_reason="escalated",
        attempt=2,
        ended_at=None,
        labels=[("owner", "ticket-work")],
        argv=["run", "--x"],
        cwd="/work",
        created_at="2026-03-10T09:00:00.000Z",
        enqueued_at="2026-03-10T09:00:00.000Z",
        status_changed_at="2026-03-10T11:00:00.000Z",
        attempts=[
            finished(
                status="failed",
                reason="exit",
                decision_ids=["d1"],
                resolved_path="/bin/run",
                effective={"timeout": None, "max_retries": 0, "kill_grace": 10},
            ),
            {
                **ESCALATED,
                "escalated_hooks": ["pre-start:0"],
                "hook_messages": [
                    {"hook_id": "pre-start:0", "outcome": "escalate", "message": "ask a human"}
                ],
            },
        ],
        output=tail(b"line 1\nline 2\n"),
        output_attempt=2,
    )
    doc = show(capsys, "jb-aaaaaa22")
    assert doc["attempts"][0]["effective"] == {"timeout": None, "max_retries": 0, "kill_grace": 10}
    assert doc["attempts"][1]["hook_messages"][0]["outcome"] == "escalate"
    check_golden("show.json", doc)


def test_queued_job_shows_rank_and_blocked_by(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22")
    seed_job("jb-aaaaaa23", not_before=ago(-H))
    seed_job("jb-aaaaaa24", position="m0000")
    doc = show(capsys, "jb-aaaaaa23")
    assert doc["rank"] == 3 and doc["blocked_by"] == "not_before"


def test_hostile_argv_json_form_is_the_documented_one(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", argv=["\udcff", "\\xff", "\ud800"])
    assert show(capsys, "jb-aaaaaa22")["argv"] == ["\\xff", "\\xff", "\\ud800"]


# -- sanitizing ------------------------------------------------------------------------------

EVIL_TAIL = (
    b"plain\n\x1b[31mred\x1b[0m\n\x1b]0;title\x07\x1b]8;;http://x\x07link\x1b]8;;\x07\n"
    b"c1 \xc2\x9b31m \xc2\x85 \xc2\x9d0;t\xc2\x9c\n"
    b"bidi \xe2\x80\xae evil \xe2\x81\xa6 sep \xe2\x80\xa8 zw \xe2\x80\x8b \xef\xbb\xbf\n"
    b"progress 10%\rprogress 100%\n"
    b"lone \xed\xa0\x80 bad \xff\n"
    b"forged\nid: jb-fffffff2\n"
    b"unterminated \x1b]0;never ends"
)
BAD_CHARS = (
    "\x1b",
    "\x9b",
    "\x9d",
    "\x9c",
    "\x85",
    "\x07",
    "\u202e",
    "\u2066",
    "\u2028",
    "\u200b",
    "\ufeff",
)


def _seed_evil() -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason="exit",
        attempt=1,
        ended_at=ENDED,
        argv=["a\x1b[2Jb", "\udcff"],
        cwd="/w\x1b]0;x\x07",
        note="n\x1b[31m\u202e\u2028\x00",
        labels=[("k", "v")],
        attempts=[
            finished(
                note="a\x1b[31m\u202e",
                hook_messages=[{"hook_id": "h:0", "outcome": "veto", "message": "m\x1b[1m\u202e"}],
            )
        ],
        output=tail(EVIL_TAIL),
    )


def test_human_show_sanitizes_every_field_and_the_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_evil()
    res = run_cli(capsys, "show", "jb-aaaaaa22")
    assert res.code == 0
    for bad in BAD_CHARS + ("\x00", "\x7f"):
        assert bad not in res.out, repr(bad)
    res.out.encode("utf-8")
    assert "progress 100%" in res.out and "progress 10%" not in res.out
    assert "unterminated" in res.out and "never ends" not in res.out
    # a forged `id:` line inside the tail never starts a line of the report
    assert not any(line.startswith("id: jb-fffffff2") for line in res.out.splitlines())
    assert res.err == ""


def test_json_tail_is_sanitized_too(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_evil()
    res = run_cli(capsys, "--json", "show", "jb-aaaaaa22")
    text = res.envelope()["output"]["tail"]
    for bad in BAD_CHARS:
        assert bad not in text, repr(bad)
    assert "\\u202e" in text and "\\xff" in text
    assert res.out.isascii()


def test_human_show_of_a_queued_job_is_plain(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", argv=["echo", "hello world"], labels=[("a", "1")])
    res = run_cli(capsys, "show", "jb-aaaaaa22")
    assert res.code == 0 and "jb-aaaaaa22" in res.out and "queued" in res.out


def test_quiet_does_not_hide_the_requested_data(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22")
    assert "jb-aaaaaa22" in run_cli(capsys, "--quiet", "show", "jb-aaaaaa22").out
    assert "jb-aaaaaa22" in run_cli(capsys, "--quiet", "list").out


# -- every string field is sanitized in human output ------------------------------------------

FORGE = "\x1b[31mX\nforged: y"


def _assert_no_forgery(out: str) -> None:
    assert "\x1b" not in out
    assert not any(line.startswith("forged") for line in out.splitlines())


def test_human_show_sanitizes_reason_hold_reason_and_timestamps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_job(
        "jb-aaaaaa22",
        status="failed",
        reason=FORGE,
        attempt=1,
        ended_at=FORGE,
        created_at=FORGE,
        enqueued_at=FORGE,
        status_changed_at=FORGE,
        not_before=FORGE,
        attempts=[
            finished(
                reason=FORGE,
                hold_reason=FORGE,
                claimed_at=FORGE,
                status="failed",
                hook_messages=[{"hook_id": "h:0", "outcome": FORGE, "message": "m"}],
            )
        ],
    )
    res = run_cli(capsys, "show", "jb-aaaaaa22")
    assert res.code == 0, res.err
    _assert_no_forgery(res.out)


def test_human_list_sanitizes_reason_and_hold_reason(capsys: pytest.CaptureFixture[str]) -> None:
    seed_job("jb-aaaaaa22", status="failed", reason=FORGE, attempt=1, ended_at=ENDED)
    seed_job("jb-bbbbbb22", status="held", hold_reason=FORGE)
    res = run_cli(capsys, "list", "--all")
    assert res.code == 0, res.err
    _assert_no_forgery(res.out)
    assert len(res.out.splitlines()) == 2


def test_exit_text_sanitizes_its_fields() -> None:
    from jobsctl.job_text import exit_text

    for record in (
        {"kind": FORGE, "code": None, "signal": None, "core": False},
        {"kind": "exited", "code": FORGE, "signal": None, "core": False},
        {"kind": "signaled", "code": None, "signal": FORGE, "core": False},
    ):
        _assert_no_forgery(exit_text(record))


def test_a_bad_attempt_is_not_found_before_the_env_file_is_read(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the env file was read before the attempt was checked")

    monkeypatch.setattr("jobsctl.commands.show.load_env_file", boom)
    seed_job("jb-pepepe22", pass_env=["TOKEN"], attempt=1, attempts=[ran()])
    res = run_cli(capsys, "--json", "show", "jb-pepepe22", "--attempt", "99")
    assert res.code == 10 and res.envelope()["error"]["code"] == "not_found"
