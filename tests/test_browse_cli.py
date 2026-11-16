"""`jobsctl browse` without a terminal or the extra, and the schema checks (no Textual needed).

These tests never skip: they cover the paths that must hold on an install without the `tui`
extra. The Textual-driven tests are in test_browse_tui.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from jobsctl import store_db
from jobsctl.commands import browse as browse_cmd
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import run_cli
from tests.manual_helpers import add_job, sql, table_dump


def test_ci_has_the_tui_extra_so_the_textual_tests_do_not_skip() -> None:
    if os.environ.get("CI"):
        import textual  # noqa: F401  (an ImportError here means CI stopped installing the extra)


def test_without_the_extra_browse_exits_2_tui_missing_with_the_install_hint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "textual", None)  # `import textual` raises ImportError
    res = run_cli(capsys, "--json", "browse")
    assert res.code == 2
    error = res.envelope()["error"]
    assert error["code"] == "tui_missing" and "jobsctl[tui]" in error["message"]
    assert "pip" not in error["message"]  # the package is not on PyPI
    validate_against("cli/error.json", res.envelope())


def test_the_missing_extra_is_reported_before_the_terminal_check(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setattr(browse_cmd, "_is_tty", lambda stream: False)
    res = run_cli(capsys, "browse")
    assert res.code == 2 and "jobsctl[tui]" in res.err


def test_only_the_browse_handler_imports_the_library() -> None:
    code = (
        "import sys; from jobsctl import commands, cli; commands.load_all(); "
        "import jobsctl.tui, jobsctl.tui.model, jobsctl.tui.actions, jobsctl.tui.result; "
        "bad = [m for m in sys.modules if m == 'textual' or m.startswith('textual.')]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_without_a_terminal_browse_is_a_usage_error_not_a_hang(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("textual")  # the terminal check comes after the extra check
    res = run_cli(capsys, "--json", "browse")
    assert res.code == 2 and res.envelope()["error"]["code"] == "usage"
    assert "terminal" in res.envelope()["error"]["message"]


def test_the_real_entry_point_with_stdin_and_stdout_redirected_exits_at_once(
    tmp_path: Path,
) -> None:
    pytest.importorskip("textual")
    out = tmp_path / "out"
    with out.open("wb") as sink:
        done = subprocess.run(
            [sys.executable, "-I", "-m", "jobsctl", "browse"],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    assert done.returncode == 2
    assert b"terminal" in done.stderr and out.read_bytes() == b""


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browse_cmd, "_is_tty", lambda stream: True)


def test_a_newer_schema_is_version_skew_exit_12_before_any_screen(
    capsys: pytest.CaptureFixture[str], tty: None
) -> None:
    pytest.importorskip("textual")
    add_job(capsys)
    sql("PRAGMA user_version = 9999")
    before = table_dump()
    res = run_cli(capsys, "--json", "browse")
    assert res.code == 12 and res.envelope()["error"]["code"] == "version_skew"
    assert table_dump() == before


def test_an_older_schema_is_refused_like_a_read_only_command_and_not_migrated(
    capsys: pytest.CaptureFixture[str], tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("textual")
    add_job(capsys)
    # this jobsctl "knows" one migration more than the database has
    monkeypatch.setattr(store_db, "MIGRATIONS", (*store_db.MIGRATIONS, ("SELECT 1",)))
    before = table_dump()
    res = run_cli(capsys, "--json", "browse")
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    assert "older" in res.envelope()["error"]["message"]
    assert table_dump() == before
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store_db.MIGRATIONS) - 1
    conn.close()


def test_the_result_file_schema_accepts_what_the_app_writes(tmp_path: Path) -> None:
    from jobsctl.tui.result import write_result

    path = tmp_path / "result.json"
    write_result(str(path), "open", ["jb-abcdefgh", "jb-ijklmnop"])
    document = json.loads(path.read_text())
    assert document == {"intent": "open", "selected": ["jb-abcdefgh", "jb-ijklmnop"]}
    validate_against("cli/browse-result.json", document)
    assert path.stat().st_mode & 0o777 == 0o600
    write_result(str(path), "back", [])  # an existing file is overwritten whole
    assert json.loads(path.read_text()) == {"intent": "back", "selected": []}


def test_the_result_file_is_never_written_through_a_symlink_or_into_a_fifo(
    tmp_path: Path,
) -> None:
    from jobsctl.errors import JobsctlError
    from jobsctl.tui.result import write_result

    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(JobsctlError):
        write_result(str(link), "back", [])
    assert target.read_text() == "keep"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(JobsctlError):  # no reader: refused, not blocked
        write_result(str(fifo), "back", [])
    with pytest.raises(JobsctlError):
        write_result(str(tmp_path / "missing" / "dir" / "file"), "back", [])
    with pytest.raises(JobsctlError):
        write_result("\x00", "back", [])


def test_an_existing_result_file_is_made_0600_on_write(tmp_path: Path) -> None:
    from jobsctl.tui.result import write_result

    path = tmp_path / "result.json"
    path.write_text("old")
    path.chmod(0o644)
    write_result(str(path), "back", [])
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_bad_result_file_is_refused_before_the_screen_starts(
    capsys: pytest.CaptureFixture[str],
    tty: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("textual")
    from jobsctl.tui import app as tui_app

    def must_not_run(self: object) -> None:
        raise AssertionError("the session started")

    monkeypatch.setattr(tui_app.BrowseApp, "run", must_not_run)
    add_job(capsys)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(victim)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for bad in (link, fifo, tmp_path):
        res = run_cli(capsys, "--json", "browse", "--result-file", str(bad))
        assert res.code == 1 and res.envelope()["error"]["code"] == "internal", bad
        assert "result file" in res.envelope()["error"]["message"]
    assert victim.read_text() == "keep"
