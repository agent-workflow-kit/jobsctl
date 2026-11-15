"""`resolve` and its note reader (interface.md, `resolve`, Hold reasons, Status transitions)."""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jobsctl.note_file import read_note
from tests.helpers import Result, run_cli
from tests.manual_helpers import (
    add_job,
    attempt_row,
    audit_records,
    change,
    config_file,
    log_records,
    make_done,
    make_held,
    make_running,
    make_starting,
    outbox,
    refusal,
    row,
    sql,
    table_dump,
)

pytestmark = pytest.mark.usefixtures("validated_records")

HOOKS = {
    "hooks": {
        "post-finish": [{"cmd": "true"}],
        "post-fail": [{"cmd": "true"}],
        "post-cancel": [{"cmd": "true"}],
    }
}


def resolve_cli(
    capsys: pytest.CaptureFixture[str], *flags: str, config: str | None = None
) -> Result:
    cfg = ("--config", config) if config else ()
    return run_cli(capsys, *cfg, "--json", "resolve", *flags)


@pytest.mark.parametrize("as_", ["done", "failed", "canceled"])
@pytest.mark.parametrize("hold", ["exit_13", "escalated", "manual"])
def test_a_held_job_is_closed_for_good(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, as_: str, hold: str
) -> None:
    cfg = config_file(tmp_path, **HOOKS)
    job_id = add_job(capsys, config=cfg)
    make_held(job_id, hold)
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z' WHERE id = ?", job_id)
    note = tmp_path / "note.txt"
    note.write_text("agent resumed by hand\n", encoding="utf-8")
    flags = ("--as", as_, "--note-file", str(note), "--actor", "human:kim", "--decision-id", "d1")
    doc = change(resolve_cli(capsys, job_id, *flags, config=cfg))
    job = doc["job"]
    assert doc["changed"] is True and job["status"] == as_ and job["reason"] == "resolved"
    assert job["hold_reason"] is None and job["not_before"] is None and job["ended_at"]
    assert job["resolved_by"] == "human:kim" and job["note"] == "agent resumed by hand\n"
    stored = row(job_id)
    assert (stored["resolved_by"], stored["override_hooks"]) == ("human:kim", "[]")
    if hold == "manual":  # a manual hold involves no attempt
        assert stored["attempt"] == 0
    else:
        att = attempt_row(job_id)
        assert (att["status"], att["reason"], att["hold_reason"]) == (as_, "resolved", None)
        assert (att["resolved_by"], att["note"]) == ("human:kim", "agent resumed by hand\n")
        (record,) = log_records("transition")
        assert (record["attempt_status"], record["attempt_reason"]) == (as_, "resolved")
        assert record["retry_pending"] is False
    (event,) = outbox()
    assert (
        event["hook"]
        == {"done": "post-finish", "failed": "post-fail", "canceled": "post-cancel"}[as_]
    )
    assert event["payload"]["previous_status"] == "held"
    (audit,) = audit_records()
    assert (
        audit["action"] == "resolve" and audit["result"] == as_ and audit["decision_id"] == ["d1"]
    )
    assert "note" not in audit["inputs"] and "overrides" not in audit["inputs"]


def test_the_default_status_is_done_and_no_note_is_null(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    doc = change(resolve_cli(capsys, job_id))
    assert doc["job"]["status"] == "done" and doc["job"]["note"] is None


def test_resolving_the_same_status_again_is_a_noop(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    change(resolve_cli(capsys, job_id, "--as", "failed", "--actor", "human:a"))
    before = table_dump()
    again = change(resolve_cli(capsys, job_id, "--as", "failed", "--actor", "human:b"))
    assert again["changed"] is False and again["job"]["resolved_by"] == "human:a"
    assert table_dump() == before and len(audit_records()) == 1


def test_a_resolved_job_with_another_status_is_not_held(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    change(resolve_cli(capsys, job_id, "--as", "failed"))
    doc = refusal(resolve_cli(capsys, job_id, "--as", "done"), "not_held")
    assert doc["error"]["details"] == {"status": "failed"}


@pytest.mark.parametrize("setup", [None, "starting", "running", "done", "failed", "canceled"])
def test_any_other_job_is_refused_not_held_with_its_status(
    capsys: pytest.CaptureFixture[str], setup: str | None
) -> None:
    job_id = add_job(capsys)
    if setup == "starting":
        make_starting(job_id)
    elif setup == "running":
        make_running(job_id)
    elif setup in ("done", "failed"):
        make_done(job_id, setup)
    elif setup == "canceled":
        make_done(job_id, "failed")
        sql("UPDATE jobs SET status = 'canceled', reason = 'canceled' WHERE id = ?", job_id)
        sql("UPDATE attempts SET status = 'canceled', reason = 'canceled' WHERE job_id = ?", job_id)
    before = table_dump()
    doc = refusal(resolve_cli(capsys, job_id), "not_held")
    assert doc["error"]["details"] == {"status": setup or "queued"}
    assert table_dump() == before and audit_records() == []


def test_a_resolved_done_job_from_a_natural_exit_is_not_a_noop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_done(job_id)  # reason `exit`, not `resolved`
    refusal(resolve_cli(capsys, job_id), "not_held")


def test_unknown_job(capsys: pytest.CaptureFixture[str]) -> None:
    assert resolve_cli(capsys, "jb-aaaaaaaa").code == 10


def test_a_released_exit_13_job_that_is_held_again_ends_its_held_attempt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    run_cli(capsys, "--json", "retry", job_id)
    run_cli(capsys, "--json", "hold", job_id)
    assert attempt_row(job_id)["status"] == "held"
    change(resolve_cli(capsys, job_id, "--as", "canceled"))
    assert attempt_row(job_id)["status"] == "canceled"


def test_dry_run_projects_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")
    note = tmp_path / "n"
    note.write_text("later", encoding="utf-8")
    before = table_dump()
    doc = change(
        resolve_cli(capsys, job_id, "--as", "failed", "--note-file", str(note), "--dry-run")
    )
    assert doc["dry_run"] is True and doc["changed"] is True
    assert doc["job"]["status"] == "failed" and doc["job"]["note"] == "later"
    assert table_dump() == before and audit_records() == [] and outbox() == []
    refusal(resolve_cli(capsys, add_job(capsys), "--dry-run"), "not_held")


def test_no_human_gate(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated")
    assert resolve_cli(capsys, job_id, "--actor", "auto:ci").code == 0


# -- the note -----------------------------------------------------------------------------------


def test_stdin_note(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    class Stdin(io.TextIOWrapper):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", Stdin(io.BytesIO("héllo".encode())))
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    assert change(resolve_cli(capsys, job_id, "--note-file", "-"))["job"]["note"] == "héllo"


def test_a_tty_stdin_is_read_until_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.TextIOWrapper):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty(io.BytesIO(b"typed note")))
    assert read_note("-") == "typed note"


def test_exactly_4096_bytes_pass_and_4097_exit_2(tmp_path: Path) -> None:
    ok, big = tmp_path / "ok", tmp_path / "big"
    ok.write_bytes(b"a" * 4096)
    big.write_bytes(b"a" * 4097)
    assert read_note(str(ok)) == "a" * 4096
    with pytest.raises(Exception) as err:
        read_note(str(big))
    assert getattr(err.value, "code", "") == "usage"


def test_a_huge_file_is_read_only_up_to_the_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    huge = tmp_path / "huge"
    with huge.open("wb") as f:
        f.truncate(50 * 1024 * 1024 * 1024)  # sparse: 50 GiB
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    res = resolve_cli(capsys, job_id, "--note-file", str(huge))
    assert res.code == 2 and "4096" in res.out
    assert row(job_id)["status"] == "held"


def test_a_note_over_the_limit_leaves_the_job_unchanged(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    big = tmp_path / "big"
    big.write_text("x" * 5000)
    before = table_dump()
    assert resolve_cli(capsys, job_id, "--note-file", str(big)).code == 2
    assert table_dump() == before


def test_usage_errors_come_before_not_found(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    big = tmp_path / "big"
    big.write_text("x" * 5000)
    assert resolve_cli(capsys, "jb-aaaaaaaa", "--note-file", str(big)).code == 2


@pytest.mark.parametrize("data", [b"\xff\xfe bad", b"ok \xc3"])
def test_a_note_that_is_not_utf8_is_refused_without_echoing_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, data: bytes
) -> None:
    f = tmp_path / "n"
    f.write_bytes(data)
    res = resolve_cli(capsys, "jb-aaaaaaaa", "--note-file", str(f))
    assert res.code == 2 and "bad" not in res.out and "bad" not in res.err


def test_an_empty_note_is_an_empty_string(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    f = tmp_path / "n"
    f.write_bytes(b"")
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    assert change(resolve_cli(capsys, job_id, "--note-file", str(f)))["job"]["note"] == ""


def test_control_characters_and_nul_are_stored_as_given_and_sanitized_for_humans(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    f = tmp_path / "n"
    f.write_bytes(b"a\x00b\x1b[31mred\x1b[0m\n")
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    change(resolve_cli(capsys, job_id, "--note-file", str(f)))
    assert row(job_id)["note"] == "a\x00b\x1b[31mred\x1b[0m\n"


def test_a_note_with_a_secret_looking_value_is_stored_untouched(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # The note is free text (not a grammar field) and not masked; the spec only bounds it.
    f = tmp_path / "n"
    f.write_text("token=abcdefgh12345678")
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    change(resolve_cli(capsys, job_id, "--note-file", str(f)))
    assert row(job_id)["note"] == "token=abcdefgh12345678"
    assert "abcdefgh12345678" not in str(audit_records())


def test_a_missing_path_does_not_echo_anything_but_the_reason(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    res = resolve_cli(capsys, "jb-aaaaaaaa", "--note-file", str(tmp_path / "no-such"))
    assert res.code == 2 and "no-such" not in res.out


def test_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Exception) as err:
        read_note(str(tmp_path))
    assert getattr(err.value, "code", "") == "usage"


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(Exception) as err:
        read_note(str(fifo))  # no writer: a blocking open would hang here
    assert getattr(err.value, "code", "") == "usage"


def test_a_device_is_refused() -> None:
    with pytest.raises(Exception) as err:
        read_note("/dev/zero")
    assert getattr(err.value, "code", "") == "usage"


def test_a_symlink_to_a_regular_file_is_followed_and_a_symlink_to_a_fifo_refused(
    tmp_path: Path,
) -> None:
    target = tmp_path / "t"
    target.write_text("via link")
    link = tmp_path / "l"
    link.symlink_to(target)
    assert read_note(str(link)) == "via link"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    bad = tmp_path / "lf"
    bad.symlink_to(fifo)
    with pytest.raises(Exception) as err:
        read_note(str(bad))
    assert getattr(err.value, "code", "") == "usage"


def test_a_unix_socket_and_a_dangling_link_and_nul_path_are_refused(tmp_path: Path) -> None:
    sock_path = tmp_path / "s"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock_path))
    dangling = tmp_path / "d"
    dangling.symlink_to(tmp_path / "gone")
    try:
        for path in (str(sock_path), str(dangling), "a\x00b", ""):
            with pytest.raises(Exception) as err:
                read_note(path)
            assert getattr(err.value, "code", "") == "usage"
    finally:
        s.close()


def test_a_stdin_that_cannot_be_read_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def read(self, _: int) -> bytes:
            raise IsADirectoryError(21, "Is a directory")

    class Stdin:
        buffer = Broken()

    monkeypatch.setattr(sys, "stdin", Stdin())
    with pytest.raises(Exception) as err:
        read_note("-")
    assert getattr(err.value, "code", "") == "usage"


def test_a_stdin_pipe_that_never_closes_is_still_bounded_by_the_limit() -> None:
    """A writer that keeps the pipe open after 5000 bytes: the reader stops at 4097 bytes."""
    code = (
        "import sys; from jobsctl.note_file import read_note\n"
        "try: read_note('-')\n"
        "except Exception as e: print(e.code)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    pid = proc.pid  # the process group is its pid; signal exactly this one
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write("x" * 5000)
        proc.stdin.flush()  # stdin stays open
        timer = threading.Timer(60, lambda: os.killpg(pid, signal.SIGKILL))
        timer.start()
        try:
            assert proc.stdout.readline().strip() == "usage"
        finally:
            timer.cancel()
    finally:
        if proc.poll() is None:
            os.killpg(pid, signal.SIGKILL)
        proc.stdin.close() if proc.stdin and not proc.stdin.closed else None
        proc.wait(timeout=30)
        if proc.stdout:
            proc.stdout.close()


def test_human_output_sanitizes_the_note(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    f = tmp_path / "n"
    f.write_bytes(b"\x1b]0;title\x07done")
    job_id = add_job(capsys)
    make_held(job_id, "manual")
    res = run_cli(capsys, "resolve", job_id, "--note-file", str(f))
    assert res.code == 0 and "\x1b" not in res.out
