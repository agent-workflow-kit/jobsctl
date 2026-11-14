"""Diagnostics files of internal processes: created on the first write, 0600, capped."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from jobsctl.diag import MAX_BYTES, DiagFile, install


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


def test_the_file_stops_at_the_cap_with_one_marker(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    diag = DiagFile(path, limit=100)
    for _ in range(50):
        diag.write("x" * 30)
    diag.close()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("x" * 100) and text.endswith("[diagnostics truncated at 1 MiB]\n")
    assert text.count("truncated") == 1


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
