"""Canonical environment: inherit, JOBSCTL_ removal, PATH replacement, prepend and cleaning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jobsctl import env_canonical as ec
from jobsctl.config import Config


def cfg(**env: object) -> Config:
    return Config.model_validate({"env": env} if env else {})


def build(config: Config, environ: dict[str, str]) -> ec.CanonicalEnv:
    return ec.build_canonical_env(config, environ)


@pytest.fixture
def bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    # pytest's tmp dirs live under /tmp, which the cleaning step drops; move the "temporary"
    # root away for tests that use real directories (the /tmp rules have their own tests).
    monkeypatch.setattr(ec, "_TEMP_ROOTS", ("/jobsctl-test-tmp", "/run/user"))
    out = []
    for name in ("a", "b", "c"):
        d = tmp_path / "bin" / name
        d.mkdir(parents=True)
        out.append(str(d))
    return out


def test_default_inherit_names_only() -> None:
    environ = {"PATH": "/usr/bin", "HOME": "/h", "LC_ALL": "C", "SECRET_TOKEN": "x", "FOO": "1"}
    result = build(cfg(), environ)
    assert result.values == {"PATH": "/usr/bin", "HOME": "/h", "LC_ALL": "C"}


def test_inherit_prefix_and_exact(bins: list[str]) -> None:
    environ = {"AWS_REGION": "r", "AWS_PROFILE": "p", "AWSX": "no", "PATH": bins[0]}
    result = build(cfg(inherit=["AWS_*", "PATH"]), environ)
    assert result.values == {"AWS_REGION": "r", "AWS_PROFILE": "p", "PATH": bins[0]}


def test_xdg_and_tmpdir_always_inherited() -> None:
    environ = {
        "XDG_CONFIG_HOME": "/c",
        "XDG_STATE_HOME": "/s",
        "XDG_DATA_HOME": "/d",
        "XDG_CACHE_HOME": "/k",
        "XDG_RUNTIME_DIR": "/r",
        "TMPDIR": "/t",
        "HOME": "/h",
    }
    result = build(cfg(inherit=[]), environ)
    assert result.values == {k: v for k, v in environ.items() if k != "HOME"}


def test_jobsctl_variables_removed_even_when_inherited() -> None:
    environ = {"JOBSCTL_GLOBAL_CAP": "", "JOBSCTL_X": "1", "JOBSCTL_": "1", "HOME": "/h"}
    result = build(cfg(inherit=["JOBSCTL_*", "JOBSCTL_X", "HOME"]), environ)
    assert result.values == {"HOME": "/h"}


def test_env_set_path_replaces_inherited(bins: list[str]) -> None:
    result = build(cfg(set={"PATH": bins[1]}), {"PATH": bins[0]})
    assert result.values["PATH"] == bins[1]


def test_env_set_path_applies_even_if_path_not_inherited(bins: list[str]) -> None:
    result = build(cfg(inherit=[], set={"PATH": bins[1]}), {"PATH": bins[0]})
    assert result.values == {"PATH": bins[1]}


def test_other_env_set_values_are_not_in_the_canonical_environment(bins: list[str]) -> None:
    result = build(cfg(set={"FOO": "bar"}), {"PATH": bins[0]})
    assert "FOO" not in result.values


def test_path_prepend_in_order_in_front(bins: list[str]) -> None:
    result = build(cfg(path_prepend=[bins[1], bins[2]]), {"PATH": bins[0]})
    assert result.values["PATH"] == ":".join([bins[1], bins[2], bins[0]])


def test_path_prepend_after_env_set_path(bins: list[str]) -> None:
    result = build(cfg(set={"PATH": bins[0]}, path_prepend=[bins[2]]), {"PATH": bins[1]})
    assert result.values["PATH"] == ":".join([bins[2], bins[0]])


def test_path_prepend_without_any_path(bins: list[str]) -> None:
    assert build(cfg(path_prepend=[bins[0]]), {}).values == {"PATH": bins[0]}


def test_path_prepend_tilde_expanded_by_config(bins: list[str]) -> None:
    home = Path(os.environ["HOME"])
    (home / "bin").mkdir()
    result = build(cfg(path_prepend=["~/bin"]), {})
    assert result.values["PATH"] == str(home / "bin")


def test_no_path_means_no_path_key() -> None:
    assert "PATH" not in build(cfg(), {"HOME": "/h"}).values


def test_path_cleaning_drops_each_kind(bins: list[str], tmp_path: Path) -> None:
    missing = str(tmp_path / "nope")
    path = ":".join(
        ["", "rel/bin", ".", missing, "/jobsctl-test-tmp/x", "/run/user/1000/bin", bins[0]]
    )
    result = build(cfg(), {"PATH": path})
    assert result.values["PATH"] == bins[0]
    dropped = {(w.entry, w.reason) for w in result.warnings}
    assert dropped == {
        ("", "empty"),
        ("rel/bin", "relative"),
        (".", "relative"),
        (missing, "missing"),
        ("/jobsctl-test-tmp/x", "temporary"),
        ("/run/user/1000/bin", "temporary"),
    }
    assert {w.code for w in result.warnings} == {"path_entry_dropped"}


@pytest.mark.parametrize(
    "entry", ["/tmp", "/tmp/", "//tmp/bin", "/tmp/../tmp/bin", "/run/user", "/run/user/0"]
)
def test_temporary_entries_variants(entry: str) -> None:
    result = build(cfg(), {"PATH": entry})
    assert "PATH" not in result.values or result.values["PATH"] == ""
    assert [w.reason for w in result.warnings] in (["temporary"], ["missing"])


def test_tmp_prefix_is_not_a_substring_match(tmp_path: Path) -> None:
    # "/tmpfoo" is not under /tmp; it is dropped only because it does not exist
    result = build(cfg(), {"PATH": "/tmpfoo"})
    assert [w.reason for w in result.warnings] == ["missing"]


def test_symlink_into_tmp_dropped(tmp_path: Path) -> None:
    target = Path("/tmp") / f"jobsctl-test-{os.getpid()}"
    target.mkdir(exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(target)
    try:
        result = build(cfg(), {"PATH": str(link)})
        assert [w.reason for w in result.warnings] == ["temporary"]
    finally:
        target.rmdir()


def test_existing_file_entry_kept_and_duplicates_kept(bins: list[str]) -> None:
    result = build(cfg(), {"PATH": f"{bins[0]}:{bins[0]}"})
    assert result.values["PATH"] == f"{bins[0]}:{bins[0]}"
    assert result.warnings == ()


def test_all_entries_dropped_gives_empty_path() -> None:
    result = build(cfg(), {"PATH": "rel"})
    assert result.values.get("PATH", "") == ""


def test_warning_entry_is_sanitized() -> None:
    result = build(cfg(), {"PATH": "evil\x1b[31m\nbin"})
    (w,) = result.warnings
    assert w.entry is not None
    assert "\x1b" not in w.entry and "\n" not in w.entry


def test_never_contains_env_file_values(bins: list[str]) -> None:
    # the builder has no env_file input at all; an inherited variable of the same name is the
    # process's own and is not an env_file value
    import inspect

    assert "env_file" not in inspect.signature(ec.build_canonical_env).parameters


def test_state_key_stable_for_same_warnings() -> None:
    a = build(cfg(), {"PATH": "rel:/tmp/x"})
    b = build(cfg(), {"PATH": "rel:/tmp/x"})
    c = build(cfg(), {"PATH": "rel"})
    assert a.state_key == b.state_key != c.state_key


def test_matches_helper() -> None:
    assert ec.matches("LC_ALL", ["LC_*"])
    assert not ec.matches("LCX", ["LC_*"])
    assert ec.matches("PATH", ["PATH"])
    assert not ec.matches("PATHX", ["PATH"])
    assert not ec.matches("A", [])
