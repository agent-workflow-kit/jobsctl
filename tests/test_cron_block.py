"""The pure parts of ``cron install`` (interface.md, cron install): quoting, PATH filtering,
the entry line and splicing the managed block into a crontab, byte for byte."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from jobsctl import cron
from jobsctl.errors import JobsctlError

ENTRY = b"* * * * * PATH='/usr/bin' '/opt/jobsctl' run --quiet"
BLOCK = b"# jobsctl managed\n" + ENTRY + b"\n"


# -- quoting ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "'plain'"),
        ("a b", "'a b'"),
        ("it's", "'it'\\''s'"),
        ("50%", "'50\\%'"),
        ("$HOME `id` $(id)", "'$HOME `id` $(id)'"),
        ("", "''"),
        ("back\\slash", "'back\\slash'"),
        ("é ünï", "'é ünï'"),
    ],
)
def test_quote(value: str, expected: str) -> None:
    assert cron.quote(value) == expected


@pytest.mark.parametrize(
    "value",
    ["plain", "a b", "it's", "'; rm -rf ~; '", '$HOME `id` $(id) "q"', "x\\y", "é", "a;b|c&d>e"],
)
def test_quote_round_trips_through_a_shell(value: str) -> None:
    """Quoted text is one literal shell word (cron hands the line to /bin/sh -c)."""
    assert shlex.split(cron.quote(value)) == [value]
    out = subprocess.run(
        ["/bin/sh", "-c", f"printf %s {cron.quote(value)}"], capture_output=True, check=True
    )
    assert out.stdout == value.encode()


def test_quoted_percent_is_cron_escaped_and_shell_literal() -> None:
    """Cron turns ``\\%`` into ``%`` before the shell sees it; emulate that step."""
    quoted = cron.quote("a%b\\%c")
    assert "%" not in quoted.replace("\\%", "")
    after_cron = quoted.replace("\\%", "%")
    out = subprocess.run(
        ["/bin/sh", "-c", f"printf %s {after_cron}"], capture_output=True, check=True
    )
    assert out.stdout == b"a%b\\%c"


# -- PATH filtering ---------------------------------------------------------------------------


def _dirs(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for name in names:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        out.append(str(d))
    return out


def test_filter_path_keeps_existing_absolute_entries_in_order(tmp_path: Path) -> None:
    a, b = _dirs(tmp_path, "a", "b")
    kept, warnings = cron.filter_path([b, a], home=str(tmp_path / "h"), temp_roots=())
    assert kept == [b, a] and warnings == []


def test_filter_path_drops_with_a_reason_each(tmp_path: Path) -> None:
    (good,) = _dirs(tmp_path, "good")
    scratch = str(tmp_path / "scratch")
    entries = ["", "rel/dir", str(tmp_path / "missing"), scratch + "/x", "/run/user/1000/fnm", good]
    kept, warnings = cron.filter_path(
        entries, home=str(tmp_path), temp_roots=(scratch, "/run/user")
    )
    assert kept == [good]
    got = [(w.get("path"), w["reason"]) for w in warnings]
    assert got == [
        (None, "empty"),
        ("rel/dir", "relative"),
        (str(tmp_path / "missing"), "missing"),
        (scratch + "/x", "temporary"),
        ("/run/user/1000/fnm", "temporary"),
    ]
    assert {w["code"] for w in warnings} == {"path_entry_dropped"}
    assert all(w["message"] for w in warnings)


def test_filter_path_drops_an_entry_with_a_control_character(tmp_path: Path) -> None:
    evil = str(tmp_path / "a\nb")
    kept, warnings = cron.filter_path([evil], home=str(tmp_path), temp_roots=())
    assert kept == []
    assert warnings[0]["reason"] == "control" and warnings[0]["path"] == evil
    assert "\n" not in warnings[0]["message"]


@pytest.mark.parametrize("ch", ["\x00", "\r", "\t", "\x1b", "\x7f", "\x85", "\u2028"])
def test_filter_path_drops_every_kind_of_control_character(tmp_path: Path, ch: str) -> None:
    kept, warnings = cron.filter_path([f"/x{ch}y"], home=str(tmp_path), temp_roots=())
    assert kept == [] and warnings[0]["reason"] == "control"


def test_filter_path_temporary_is_a_path_prefix_not_a_string_prefix(tmp_path: Path) -> None:
    """``/tmpfoo`` is not under ``/tmp``; ``/tmp`` itself and ``/tmp/../tmp/x`` are."""
    kept, warnings = cron.filter_path(
        ["/tmp", "/tmp/../tmp/x"], home=str(tmp_path), temp_roots=("/tmp",)
    )
    assert kept == [] and [w["reason"] for w in warnings] == ["temporary", "temporary"]
    root = tmp_path / "tmpfoo"
    root.mkdir()
    kept, _ = cron.filter_path([str(root)], home=str(tmp_path), temp_roots=(str(tmp_path / "tmp"),))
    assert kept == [str(root)]


def test_filter_path_temporary_resolves_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "scratch"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    kept, warnings = cron.filter_path([str(link)], home=str(tmp_path), temp_roots=(str(real),))
    assert kept == [] and warnings[0]["reason"] == "temporary"


def test_filter_path_expands_a_leading_tilde(tmp_path: Path) -> None:
    home = tmp_path / "h"
    (home / "bin").mkdir(parents=True)
    kept, warnings = cron.filter_path(["~/bin", "~"], home=str(home), temp_roots=())
    assert kept == [str(home / "bin"), str(home)] and warnings == []


def test_filter_path_other_tilde_forms_are_relative(tmp_path: Path) -> None:
    kept, warnings = cron.filter_path(["~someone/bin"], home=str(tmp_path), temp_roots=())
    assert kept == [] and warnings[0]["reason"] == "relative"


def test_filter_path_drops_a_file_and_dedupes_silently(tmp_path: Path) -> None:
    (d,) = _dirs(tmp_path, "d")
    f = tmp_path / "file"
    f.write_text("x")
    kept, warnings = cron.filter_path([d, str(f), d], home=str(tmp_path), temp_roots=())
    assert kept == [d]
    assert [w["reason"] for w in warnings] == ["missing"]


# -- the entry --------------------------------------------------------------------------------


def test_entry_has_the_documented_shape() -> None:
    line = cron.build_entry(
        jobsctl="/opt/my tool/jobsctl",
        path=["/usr/bin", "/home/u/.local/bin"],
        xdg={"XDG_CONFIG_HOME": "/home/u/c", "XDG_STATE_HOME": "/home/u/s"},
    )
    assert line == (
        "* * * * * PATH='/usr/bin:/home/u/.local/bin' XDG_CONFIG_HOME='/home/u/c' "
        "XDG_STATE_HOME='/home/u/s' '/opt/my tool/jobsctl' run --quiet"
    )


def test_entry_without_xdg_values() -> None:
    line = cron.build_entry(jobsctl="/j", path=["/usr/bin"], xdg={})
    assert line == "* * * * * PATH='/usr/bin' '/j' run --quiet"


def test_entry_quotes_hostile_values_into_one_line() -> None:
    line = cron.build_entry(
        jobsctl="/j'; touch /pwned #/jobsctl",
        path=["/a b", "/c'd", "/50%"],
        xdg={"XDG_STATE_HOME": "/s$(id)`id`"},
    )
    assert "\n" not in line
    assert "'/j'\\''; touch /pwned #/jobsctl'" in line
    assert "PATH='/a b:/c'\\''d:/50\\%'" in line
    assert "XDG_STATE_HOME='/s$(id)`id`'" in line
    assert "%" not in line.replace("\\%", "")


def test_xdg_values_filtered() -> None:
    kept, warnings = cron.filter_xdg({"XDG_CONFIG_HOME": "/ok/c", "XDG_STATE_HOME": "/bad\nstate"})
    assert kept == {"XDG_CONFIG_HOME": "/ok/c"}
    assert warnings == [
        {
            "code": "path_entry_dropped",
            "message": warnings[0]["message"],
            "variable": "XDG_STATE_HOME",
            "reason": "control",
        }
    ]
    assert "bad" not in warnings[0]["message"]


def test_xdg_unset_empty_are_not_written_and_relative_is_left_out() -> None:
    kept, warnings = cron.filter_xdg({"XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "rel/s"})
    assert kept == {}
    assert [(w["variable"], w["reason"]) for w in warnings] == [("XDG_STATE_HOME", "relative")]
    assert "rel/s" not in warnings[0]["message"]


# -- splicing ---------------------------------------------------------------------------------


def test_with_block_appends_to_an_empty_or_absent_crontab() -> None:
    assert cron.with_block(None, ENTRY) == BLOCK
    assert cron.with_block(b"", ENTRY) == BLOCK


def test_with_block_preserves_other_lines_byte_for_byte() -> None:
    other = b"# my jobs\r\nMAILTO=me@example.com\n\n0 5 * * * /bin/backup %%\n\xff\xfe bytes\r\n"
    out = cron.with_block(other, ENTRY)
    assert out == other + BLOCK


def test_with_block_adds_a_missing_final_newline_only() -> None:
    assert cron.with_block(b"0 5 * * * x", ENTRY) == b"0 5 * * * x\n" + BLOCK


def test_with_block_replaces_in_place() -> None:
    old = b"# jobsctl managed\n* * * * * PATH='/old' '/old/jobsctl' run --quiet\n"
    data = b"before\n" + old + b"after\n"
    assert cron.with_block(data, ENTRY) == b"before\n" + BLOCK + b"after\n"


def test_with_block_same_block_is_unchanged_even_without_final_newline() -> None:
    data = b"x\n" + BLOCK.rstrip(b"\n")
    assert cron.with_block(data, ENTRY) == data


def test_without_block_removes_exactly_the_block() -> None:
    data = b"a\n\n" + BLOCK + b"b"
    assert cron.without_block(data) == b"a\n\nb\n"  # the final newline cron needs


def test_install_then_uninstall_restores_the_original() -> None:
    original = b"# c\nMAILTO=x\n\n0 1 * * * y\n"
    assert cron.without_block(cron.with_block(original, ENTRY)) == original


def test_without_block_with_no_block_is_the_same_bytes() -> None:
    data = b"0 1 * * * y"
    assert cron.without_block(data) == data
    assert cron.without_block(None) is None


def test_without_block_of_only_the_block_is_empty() -> None:
    assert cron.without_block(BLOCK) == b""


@pytest.mark.parametrize(
    "data",
    [
        BLOCK + BLOCK,  # two blocks
        b"# jobsctl managed\n",  # header without an entry
        b"# jobsctl managed\n\n" + ENTRY + b"\n",  # blank line between
        b"# jobsctl managed\n0 1 * * * /something/else\n",  # next line is not our entry
        b"a\0b\n",  # NUL byte
        b"x\n" * (1024 * 1024),  # over the size limit
    ],
)
def test_hostile_or_ambiguous_crontab_is_refused(data: bytes) -> None:
    with pytest.raises(JobsctlError) as err:
        cron.with_block(data, ENTRY)
    assert err.value.code == "internal"
    with pytest.raises(JobsctlError):
        cron.without_block(data)


def test_lookalike_header_lines_are_not_blocks() -> None:
    data = b"# jobsctl managed by hand\n #  jobsctl managed\n# jobsctl managed \n"
    assert cron.with_block(data, ENTRY) == data + BLOCK
    assert cron.without_block(data) == data


def test_shape_of_a_recognized_entry() -> None:
    assert cron.is_entry(ENTRY)
    assert not cron.is_entry(b"* * * * * /bin/x")
    assert not cron.is_entry(b"*/5 * * * * 'j' run --quiet")
