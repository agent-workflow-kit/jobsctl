"""Diagnostics files of internal processes: created on the first write, 0600, capped."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from jobsctl.diag import MAX_BYTES, DiagFile, install
from jobsctl.errors import JobsctlError
from jobsctl.log_files import open_append


def test_nothing_is_created_until_the_first_write(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    diag = DiagFile(path)
    diag.flush()
    diag.close()
    assert not path.exists()


def test_the_file_is_private_and_appended(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    old_umask = os.umask(0)
    try:
        diag = DiagFile(path)
        diag.write("one\n")
        diag.write("two ünï\n")
        diag.close()
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "one\ntwo ünï\n"


MARKER = "[diagnostics truncated at 1 MiB]\n"


def test_the_file_stops_at_the_cap_with_one_marker(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    diag = DiagFile(path, limit=100)
    for _ in range(50):
        diag.write("x" * 30)
    diag.close()
    text = path.read_text(encoding="utf-8")
    assert text.endswith(MARKER) and text.count("truncated") == 1
    assert text.startswith("x" * (100 - len(MARKER)))


def test_the_file_never_exceeds_the_cap_marker_included(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    diag = DiagFile(path, limit=100)
    diag.write("x" * 500)
    diag.close()
    assert path.stat().st_size <= 100


def test_a_cut_never_splits_a_multibyte_character(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    limit = len(MARKER) + 5  # room for 5 content bytes: two 3-byte characters do not fit twice
    diag = DiagFile(path, limit=limit)
    diag.write("\u20ac\u20ac\u20ac")  # 9 bytes
    diag.close()
    raw = path.read_bytes()
    text = raw.decode("utf-8")  # strict: raises on a split sequence
    assert text == "\u20ac" + MARKER
    assert len(raw) <= limit


def test_a_full_file_reopened_gets_no_second_marker(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    first = DiagFile(path, limit=100)
    first.write("x" * 500)
    first.close()
    size = path.stat().st_size
    second = DiagFile(path, limit=100)
    second.write("more")
    second.close()
    assert path.stat().st_size == size
    assert path.read_text(encoding="utf-8").count("truncated") == 1


def test_the_default_cap_is_one_mebibyte() -> None:
    assert MAX_BYTES == 1024 * 1024


def test_a_symlink_is_not_followed_and_the_process_carries_on(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "d.log"
    link.symlink_to(target)
    diag = DiagFile(link)
    assert diag.write("hello") == 5  # a refused file never fails the process
    assert target.read_text(encoding="utf-8") == "keep"


def test_a_hard_linked_file_is_refused_before_it_is_written_or_chmodded(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    victim.chmod(0o644)
    link = tmp_path / "d.log"
    os.link(victim, link)
    with pytest.raises(JobsctlError, match="unsafe"):
        os.close(open_append(link))
    assert DiagFile(link).write("hello") == 5  # the process carries on
    assert victim.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_a_missing_directory_is_survived(tmp_path: Path) -> None:
    assert DiagFile(tmp_path / "nope" / "d.log").write("x") == 1


def test_install_replaces_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    diag = install(tmp_path / "d.log")
    print("out")
    print("err", file=sys.stderr)
    diag.close()
    assert (tmp_path / "d.log").read_text(encoding="utf-8") == "out\nerr\n"
