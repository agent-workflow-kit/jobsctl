"""env_file loading: format, availability checks, hook-environment filtering, process hardening."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from jobsctl import env_file as ef
from jobsctl import gateway

SRC = str(Path(__file__).resolve().parent.parent / "src")


@pytest.fixture
def secret_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    d.mkdir(mode=0o700)
    return d


def write(directory: Path, content: bytes | str, mode: int = 0o600) -> Path:
    path = directory / "env"
    data = content.encode() if isinstance(content, str) else content
    path.write_bytes(data)
    path.chmod(mode)
    return path


def load(path: Path | str | None, trust_from: Path | None = None) -> ef.EnvFile:
    # tmp_path lives under a world-writable /tmp, so the ancestor walk starts below it.
    return ef.load_env_file(None if path is None else str(path), check_ancestors_from=trust_from)


def load_in(directory: Path, content: bytes | str, mode: int = 0o600) -> ef.EnvFile:
    return load(write(directory, content, mode), directory.parent)


def codes(result: ef.EnvFile) -> list[tuple[str, int | None]]:
    return [(w.code, w.line) for w in result.warnings]


# format


def test_not_configured_is_not_unavailable() -> None:
    result = load(None)
    assert result.status == "not_configured"
    assert result.values == {}
    assert result.warnings == ()


def test_parses_names_values_blank_lines_and_comments(secret_dir: Path) -> None:
    result = load_in(secret_dir, "# comment\n\nTOKEN=abc=def\nEMPTY=\n_X1=v w \n")
    assert result.status == "available"
    assert result.values == {"TOKEN": "abc=def", "EMPTY": "", "_X1": "v w "}
    assert result.warnings == ()


def test_no_trailing_newline_and_crlf(secret_dir: Path) -> None:
    result = load_in(secret_dir, b"A=one\r\nB=two\r\r\nC=three\r")
    assert result.values == {"A": "one", "B": "two\r", "C": "three"}


def test_no_quoting_expansion_or_export(secret_dir: Path) -> None:
    result = load_in(secret_dir, "A=\"quoted\"\nB=$HOME\nexport C=1\nD='x'\n")
    assert result.values == {"A": '"quoted"', "B": "$HOME", "D": "'x'"}
    assert codes(result) == [("env_file_line_invalid", 3)]


def test_last_duplicate_wins(secret_dir: Path) -> None:
    assert load_in(secret_dir, "A=1\nA=2\nA=3\n").values == {"A": "3"}


def test_invalid_lines_are_reported_by_number_only(secret_dir: Path) -> None:
    body = "GOOD=ok\nnoequals-supersecretvalue\n=novalue\n1BAD=x\nBA D=x\n  # indented\n"
    result = load_in(secret_dir, body)
    assert result.values == {"GOOD": "ok"}
    assert codes(result) == [("env_file_line_invalid", n) for n in (2, 3, 4, 5, 6)]
    assert "supersecretvalue" not in repr(result)
    assert all(w.variable is None for w in result.warnings)


def test_nul_byte_line_ignored(secret_dir: Path) -> None:
    result = load_in(secret_dir, b"A=ok\nB=bad\x00value\nC=fine\n")
    assert result.values == {"A": "ok", "C": "fine"}
    assert codes(result) == [("env_file_line_invalid", 2)]


def test_invalid_utf8_line_ignored(secret_dir: Path) -> None:
    result = load_in(secret_dir, b"A=ok\nB=\xff\xfe\nC=fine\n")
    assert result.values == {"A": "ok", "C": "fine"}
    assert codes(result) == [("env_file_line_invalid", 2)]


def test_bom_makes_first_line_invalid(secret_dir: Path) -> None:
    result = load_in(secret_dir, b"\xef\xbb\xbfA=1\nB=2\n")
    assert result.values == {"B": "2"}
    assert codes(result) == [("env_file_line_invalid", 1)]


def test_unicode_separators_do_not_split_lines(secret_dir: Path) -> None:
    result = load_in(secret_dir, "A=one two\x0bthree\x85four\n")
    assert result.values == {"A": "one two\x0bthree\x85four"}


def test_name_grammar_boundary(secret_dir: Path) -> None:
    ok = "N" * 128
    result = load_in(secret_dir, f"{ok}=1\n{'N' * 129}=2\n")
    assert result.values == {ok: "1"}
    assert codes(result) == [("env_file_line_invalid", 2)]


def test_large_value_within_limit(secret_dir: Path) -> None:
    value = "x" * (1024 * 1024 - 10)
    assert load_in(secret_dir, f"BIG={value}\n").values == {"BIG": value}


def test_values_hidden_from_repr(secret_dir: Path) -> None:
    result = load_in(secret_dir, "TOKEN=hunter2hunter2\n")
    assert "hunter2" not in repr(result)
    assert "hunter2" not in str(result)


# availability


def test_over_one_mebibyte_unavailable(secret_dir: Path) -> None:
    result = load_in(secret_dir, "A=" + "x" * (1024 * 1024))
    assert result.status == "unavailable"
    assert result.values == {}
    assert [w.code for w in result.warnings] == ["env_file_unsafe"]


def test_exactly_one_mebibyte_available(secret_dir: Path) -> None:
    body = "A=" + "x" * (1024 * 1024 - 3) + "\n"
    assert len(body) == 1024 * 1024
    assert load_in(secret_dir, body).status == "available"


@pytest.mark.parametrize("mode", [0o640, 0o620, 0o604, 0o602, 0o660, 0o666, 0o644])
def test_group_or_other_access_unavailable(secret_dir: Path, mode: int) -> None:
    result = load_in(secret_dir, "A=1\n", mode)
    assert result.status == "unavailable"
    assert result.values == {}


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700, 0o500])
def test_private_modes_available(secret_dir: Path, mode: int) -> None:
    assert load_in(secret_dir, "A=1\n", mode).status == "available"


def test_missing_file_unavailable(secret_dir: Path) -> None:
    result = load(secret_dir / "nope", secret_dir.parent)
    assert result.status == "unavailable"
    assert [w.code for w in result.warnings] == ["env_file_unsafe"]


def test_symlinked_file_unavailable(secret_dir: Path) -> None:
    target = write(secret_dir, "A=1\n")
    link = secret_dir / "link"
    link.symlink_to(target)
    assert load(link, secret_dir.parent).status == "unavailable"


def test_fifo_unavailable_and_does_not_block(secret_dir: Path) -> None:
    fifo = secret_dir / "fifo"
    os.mkfifo(fifo, 0o600)
    result = load(fifo, secret_dir.parent)
    assert result.status == "unavailable"


def test_directory_and_device_unavailable(secret_dir: Path) -> None:
    sub = secret_dir / "sub"
    sub.mkdir(mode=0o700)
    assert load(sub, secret_dir.parent).status == "unavailable"
    assert load("/dev/null").status == "unavailable"


def test_file_owned_by_someone_else_unavailable(
    secret_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(secret_dir, "A=1\n")
    monkeypatch.setattr(ef, "_euid", lambda: os.geteuid() + 1)
    assert load(path, secret_dir.parent).status == "unavailable"


def test_group_writable_ancestor_unavailable(secret_dir: Path) -> None:
    path = write(secret_dir, "A=1\n")
    secret_dir.chmod(0o770)
    assert load(path, secret_dir.parent).status == "unavailable"
    secret_dir.chmod(0o702)
    assert load(path, secret_dir.parent).status == "unavailable"
    secret_dir.chmod(0o750)  # group read is fine for a directory
    assert load(path, secret_dir.parent).status == "available"


def test_group_writable_grandparent_unavailable(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True, mode=0o700)
    path = write(inner, "A=1\n")
    outer.chmod(0o770)
    assert load(path, tmp_path).status == "unavailable"


def test_symlinked_ancestor_unavailable(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    write(real, "A=1\n")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert load(link / "env", tmp_path).status == "unavailable"
    assert load(real / "env", tmp_path).status == "available"


def test_ancestor_trust_rules() -> None:
    me = 1000
    assert ef._dir_trusted(stat.S_IFDIR | 0o700, me, me)
    assert ef._dir_trusted(stat.S_IFDIR | 0o755, 0, me)  # root-owned
    assert not ef._dir_trusted(stat.S_IFDIR | 0o700, me + 1, me)  # another non-root user
    assert not ef._dir_trusted(stat.S_IFDIR | 0o775, me, me)  # group-writable
    assert not ef._dir_trusted(stat.S_IFDIR | 0o757, me, me)  # other-writable
    assert not ef._dir_trusted(stat.S_IFDIR | 0o1777, 0, me)  # sticky does not excuse it
    assert not ef._dir_trusted(stat.S_IFLNK | 0o777, me, me)
    assert not ef._dir_trusted(stat.S_IFREG | 0o600, me, me)


def test_file_trust_rules() -> None:
    me = 1000
    assert ef._file_trusted(stat.S_IFREG | 0o600, me, 10, me)
    assert not ef._file_trusted(stat.S_IFREG | 0o600, 0, 10, me)  # even root-owned
    assert not ef._file_trusted(stat.S_IFREG | 0o640, me, 10, me)
    assert not ef._file_trusted(stat.S_IFREG | 0o600, me, ef.MAX_BYTES + 1, me)
    assert not ef._file_trusted(stat.S_IFIFO | 0o600, me, 0, me)


@pytest.mark.parametrize("bad", ["relative/env", "", "/a/../b/env", "/a/./b"])
def test_non_absolute_or_dotted_paths_unavailable(bad: str) -> None:
    assert load(bad).status == "unavailable"


def test_world_writable_tmp_ancestor_is_unsafe(tmp_path: Path) -> None:
    if not (Path("/tmp").stat().st_mode & 0o022):
        pytest.skip("/tmp is not group/other writable here")
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    path = write(d, "A=1\n")
    assert str(path).startswith("/tmp/") or pytest.skip("tmp_path is not under /tmp")
    assert ef.load_env_file(str(path)).status == "unavailable"


def test_unsafe_warning_has_reason_but_no_content(secret_dir: Path) -> None:
    result = load_in(secret_dir, "TOKEN=hunter2hunter2\n", 0o644)
    (warning,) = result.warnings
    assert warning.code == "env_file_unsafe"
    assert warning.reason == "mode"
    assert "hunter2" not in repr(warning)


def test_state_key_changes_with_state(secret_dir: Path) -> None:
    good = load_in(secret_dir, "A=1\n")
    same = load_in(secret_dir, "A=2\n")
    bad = load_in(secret_dir, "A=1\n", 0o644)
    assert good.state_key == same.state_key
    assert good.state_key != bad.state_key


# hook environment


def test_hook_values_drop_ignored_names(secret_dir: Path) -> None:
    names = [
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "JOBSCTL_X",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "PYTHON",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "IFS",
    ]
    body = "KEEP=1\n" + "".join(f"{n}=x\n" for n in names) + "XDG_DATA_HOME=ok\nPATHS=ok\n"
    result = load_in(secret_dir, body)
    values, warnings = result.hook_values()
    assert values == {"KEEP": "1", "XDG_DATA_HOME": "ok", "PATHS": "ok"}
    assert [(w.code, w.line) for w in warnings] == [
        ("env_file_line_ignored", n) for n in range(2, 2 + len(names))
    ]
    # the full set stays available for --pass-env
    assert result.values["PATH"] == "x"


def test_hook_values_unavailable_is_empty(secret_dir: Path) -> None:
    result = load_in(secret_dir, "A=1\n", 0o644)
    assert result.hook_values() == ({}, ())


def test_hook_ignored_line_is_last_duplicate(secret_dir: Path) -> None:
    result = load_in(secret_dir, "IFS=1\nIFS=2\n")
    _, warnings = result.hook_values()
    assert [(w.code, w.line) for w in warnings] == [("env_file_line_ignored", 2)]


# dumpable


def test_prctl_dumpable_cleared_in_loading_process(secret_dir: Path) -> None:
    path = write(secret_dir, "A=1\n")
    code = (
        "import sys, ctypes; sys.path.insert(0, sys.argv[1]);"
        "from jobsctl import env_file as ef;"
        "libc = ctypes.CDLL(None, use_errno=True);"
        "before = libc.prctl(3, 0, 0, 0, 0);"
        "ef.load_env_file(sys.argv[2], check_ancestors_from=sys.argv[3]);"
        "print(before, libc.prctl(3, 0, 0, 0, 0))"
    )
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux only")
    done = gateway.run(
        [sys.executable, "-c", code, SRC, str(path), str(secret_dir.parent)], timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1", "0"]


def test_not_configured_does_not_need_hardening_but_is_safe() -> None:
    assert load(None).status == "not_configured"
