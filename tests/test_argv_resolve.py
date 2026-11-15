"""Resolving argv[0] against the job environment's PATH."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jobsctl.argv_resolve import resolve_argv0


def make_exe(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


def test_a_bare_name_is_found_on_the_job_path_in_order(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    make_exe(first / "tool")
    make_exe(second / "tool")
    env = {"PATH": f"{first}:{second}"}
    assert resolve_argv0("tool", env, "/") == str(first / "tool")


def test_the_process_path_is_not_consulted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_exe(tmp_path / "bin" / "tool")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert resolve_argv0("tool", {"PATH": "/nonexistent"}, "/") is None
    assert resolve_argv0("tool", {}, "/") is None


def test_empty_and_relative_entries_are_skipped(tmp_path: Path) -> None:
    make_exe(tmp_path / "tool")
    make_exe(tmp_path / "rel" / "tool")
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert resolve_argv0("tool", {"PATH": f":.:rel:{tmp_path}"}, "/") == str(tmp_path / "tool")
        assert resolve_argv0("tool", {"PATH": ":.:rel"}, "/") is None
    finally:
        os.chdir(old)


def test_non_executables_and_directories_are_skipped(tmp_path: Path) -> None:
    make_exe(tmp_path / "a" / "tool", 0o644)
    (tmp_path / "b" / "tool").mkdir(parents=True)
    make_exe(tmp_path / "c" / "tool")
    env = {"PATH": f"{tmp_path / 'a'}:{tmp_path / 'b'}:{tmp_path / 'c'}"}
    assert resolve_argv0("tool", env, "/") == str(tmp_path / "c" / "tool")


def test_a_path_with_a_slash_is_used_as_is(tmp_path: Path) -> None:
    exe = make_exe(tmp_path / "x")
    assert resolve_argv0(str(exe), {"PATH": ""}, "/") == str(exe)
    assert resolve_argv0(str(tmp_path / "missing"), {}, "/") is None
    assert resolve_argv0(str(tmp_path), {}, "/") is None  # a directory


def test_a_relative_path_with_a_slash_is_taken_relative_to_the_job_cwd(tmp_path: Path) -> None:
    make_exe(tmp_path / "bin" / "run.sh")
    assert resolve_argv0("./bin/run.sh", {}, str(tmp_path)) == str(tmp_path / "./bin/run.sh")
    assert resolve_argv0("./nope", {}, str(tmp_path)) is None


@pytest.mark.parametrize("name", ["", "a\x00b", "no-such-command-xyz"])
def test_hostile_names_do_not_resolve(name: str, tmp_path: Path) -> None:
    assert resolve_argv0(name, {"PATH": str(tmp_path)}, "/") is None
