"""Hook environment: canonical + env_file - hooks_strip_env + JOBSCTL_* hook variables."""

from __future__ import annotations

import pytest

from jobsctl.config import Config
from jobsctl.hook_env import (
    build_hook_env,
    env_file_name_ignored,
    provisional_canonical_environment,
)

BASE = {"PATH": "/usr/bin", "HOME": "/h", "LANG": "C"}


def build(**kw: object) -> dict[str, str]:
    args: dict[str, object] = {"hook": "pre-start", "job_id": "jb-abcdefgh"}
    args.update(kw)
    return build_hook_env(BASE, **args)  # type: ignore[arg-type]


def test_hook_variables_are_set() -> None:
    env = build()
    assert env["JOBSCTL_HOOK"] == "pre-start"
    assert env["JOBSCTL_ID"] == "jb-abcdefgh"
    assert env["JOBSCTL_OBJECT_TYPE"] == "job"
    assert env["PATH"] == "/usr/bin"


def test_env_file_values_are_added_and_override_canonical() -> None:
    env = build(env_file={"TOKEN": "t", "LANG": "de"})
    assert env["TOKEN"] == "t" and env["LANG"] == "de"


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "JOBSCTL_FOO",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "IFS",
    ],
)
def test_env_file_ignored_names_never_reach_a_hook(name: str) -> None:
    assert env_file_name_ignored(name)
    env = build(env_file={name: "evil"})
    assert env.get(name) != "evil"


def test_ignored_does_not_overmatch() -> None:
    assert not env_file_name_ignored("PATHS")
    assert not env_file_name_ignored("MY_LD_X")
    assert not env_file_name_ignored("XDG_DATA_HOME")


def test_strip_removes_canonical_and_env_file_values() -> None:
    env = build(env_file={"TOKEN": "t"}, strip=["LANG", "TOKEN", "NOPE"])
    assert "LANG" not in env and "TOKEN" not in env and env["HOME"] == "/h"


def test_strip_cannot_remove_hook_variables_and_jobsctl_internals_never_leak() -> None:
    canonical = {**BASE, "JOBSCTL_STATE": "/s", "JOBSCTL_HOOK": "forged", "JOBSCTL_ID": "x"}
    env = build_hook_env(
        canonical,
        hook="post-fail",
        job_id="jb-abcdefgh",
        env_file={"JOBSCTL_X": "1"},
        strip=["JOBSCTL_HOOK", "JOBSCTL_ID"],
    )
    assert env["JOBSCTL_HOOK"] == "post-fail" and env["JOBSCTL_ID"] == "jb-abcdefgh"
    assert {k for k in env if k.startswith("JOBSCTL_")} == {
        "JOBSCTL_HOOK",
        "JOBSCTL_ID",
        "JOBSCTL_OBJECT_TYPE",
    }


def test_invalid_names_and_nul_values_are_dropped() -> None:
    env = build_hook_env(
        {"A=B": "1", "": "2", "OK": "v\x00w", "1X": "3", "GOOD": "fine", "ünï": "4"},
        hook="pre-start",
        job_id="jb-abcdefgh",
    )
    assert env["GOOD"] == "fine"
    assert set(env) == {"GOOD", "JOBSCTL_HOOK", "JOBSCTL_ID", "JOBSCTL_OBJECT_TYPE"}


def test_provisional_canonical_environment_inherits_by_name_and_pattern() -> None:
    environ = {
        "PATH": "/bin",
        "LC_ALL": "C",
        "LCX": "no",
        "SECRET_TOKEN": "s",
        "JOBSCTL_X": "1",
        "TMPDIR": "/t",
        "XDG_RUNTIME_DIR": "/r",
        "XDG_SESSION_ID": "9",
    }
    got = provisional_canonical_environment(Config(), environ)
    assert got == {"PATH": "/bin", "LC_ALL": "C", "TMPDIR": "/t", "XDG_RUNTIME_DIR": "/r"}


def test_provisional_canonical_environment_honors_configured_inherit() -> None:
    config = Config.model_validate({"env": {"inherit": ["SECRET_*"]}})
    got = provisional_canonical_environment(config, {"SECRET_A": "1", "PATH": "/bin"})
    assert got == {"SECRET_A": "1"}
