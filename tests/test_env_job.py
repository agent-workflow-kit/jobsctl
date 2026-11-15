"""Job environment: the six steps in order, missing_env, reserved names."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsctl import env_canonical as ec
from jobsctl import env_file as ef
from jobsctl import env_job as ej
from jobsctl.config import Config


def cfg(**kw: object) -> Config:
    return Config.model_validate(kw)


def secrets(tmp_path: Path, body: str) -> ef.EnvFile:
    d = tmp_path / "s"
    d.mkdir(mode=0o700, exist_ok=True)
    p = d / "env"
    p.write_text(body)
    p.chmod(0o600)
    return ef.load_env_file(str(p), check_ancestors_from=tmp_path)


NOFILE = ef.EnvFile("not_configured")


def build(
    config: Config,
    canonical: dict[str, str] | None = None,
    env_file: ef.EnvFile = NOFILE,
    env: dict[str, str] | None = None,
    pass_env: list[str] | None = None,
) -> dict[str, str]:
    result = ej.build_job_env(
        config,
        ec.CanonicalEnv(canonical or {}),
        env_file,
        job_id="jb-abcdefgh",
        attempt=2,
        env=env or {},
        pass_env=pass_env or [],
    )
    return dict(result.values)


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(ec, "_TEMP_ROOTS", ("/jobsctl-test-tmp", "/run/user"))
    out = []
    for n in "abc":
        d = tmp_path / n
        d.mkdir()
        out.append(str(d))
    return out


def test_base_is_canonical_restricted_to_inherit() -> None:
    canonical = {"HOME": "/h", "XDG_CONFIG_HOME": "/c", "TMPDIR": "/t", "LANG": "C", "ODD": "1"}
    result = build(cfg(env={"inherit": ["HOME", "LANG"]}), canonical)
    assert result == {
        "HOME": "/h",
        "LANG": "C",
        "JOBSCTL_JOB_ID": "jb-abcdefgh",
        "JOBSCTL_ATTEMPT": "2",
    }


def test_default_inherit_excludes_tmux_and_ssh() -> None:
    canonical = {"TMUX": "x", "TMUX_PANE": "%1", "SSH_AUTH_SOCK": "/s", "TERM": "xterm"}
    assert build(cfg(), canonical).keys() >= {"TERM"}
    result = build(cfg(), canonical)
    assert not {"TMUX", "TMUX_PANE", "SSH_AUTH_SOCK"} & result.keys()


def test_inherit_listing_tmux_keeps_it() -> None:
    result = build(cfg(env={"inherit": ["TMUX"]}), {"TMUX": "x"})
    assert result["TMUX"] == "x"


def test_env_set_overrides_base_then_env_overrides_set() -> None:
    config = cfg(env={"set": {"A": "set", "B": "set"}})
    result = build(config, {"A": "base"}, env={"B": "env"})
    assert result["A"] == "set"
    assert result["B"] == "env"
    result = build(cfg(env={"inherit": ["A"], "set": {"A": "set"}}), {"A": "base"})
    assert result["A"] == "set"


def test_env_set_path_then_prepend(dirs: list[str]) -> None:
    config = cfg(env={"set": {"PATH": dirs[0]}, "path_prepend": [dirs[1]]})
    result = build(config, {"PATH": dirs[2]})
    assert result["PATH"] == f"{dirs[1]}:{dirs[0]}"


def test_canonical_path_with_prepend_is_not_duplicated(dirs: list[str]) -> None:
    config = cfg(env={"path_prepend": [dirs[1]]})
    canonical = ec.build_canonical_env(config, {"PATH": dirs[0]})
    result = ej.build_job_env(
        config, canonical, NOFILE, job_id="jb-abcdefgh", attempt=1, env={}, pass_env=[]
    )
    assert result.values["PATH"] == f"{dirs[1]}:{dirs[0]}"


def test_prepend_entries_that_canonical_cleaning_drops_stay_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the directory exists, so only the (patched) temporary rule can drop it
    temp = tmp_path / "tmproot"
    (temp / "bin").mkdir(parents=True)
    monkeypatch.setattr(ec, "_TEMP_ROOTS", (str(temp),))
    config = cfg(env={"path_prepend": [str(temp / "bin")]})
    canonical = ec.build_canonical_env(config, {"PATH": "/usr/bin"})
    assert [(w.path, w.reason) for w in canonical.warnings] == [(str(temp / "bin"), "temporary")]
    result = build(config, {"PATH": "/usr/bin"})
    assert result["PATH"] == "/usr/bin"


def test_prepend_entry_with_colon_split_like_canonical(dirs: list[str]) -> None:
    config = cfg(env={"path_prepend": [f"{dirs[0]}:/jobsctl-test-tmp/evil:{dirs[1]}"]})
    assert build(config, {"PATH": dirs[2]})["PATH"] == f"{dirs[0]}:{dirs[1]}:{dirs[2]}"
    canonical = ec.build_canonical_env(config, {"PATH": dirs[2]})
    result = ej.build_job_env(
        config, canonical, NOFILE, job_id="jb-abcdefgh", attempt=1, env={}, pass_env=[]
    )
    assert result.values["PATH"] == canonical.values["PATH"] == f"{dirs[0]}:{dirs[1]}:{dirs[2]}"


def test_prepend_all_dropped_never_leaves_an_empty_path(dirs: list[str]) -> None:
    config = cfg(env={"inherit": [], "path_prepend": ["/jobsctl-test-tmp/x"]})
    assert "PATH" not in build(config, {})


def test_env_entry_overrides_path(dirs: list[str]) -> None:
    result = build(cfg(env={"path_prepend": [dirs[0]]}), {"PATH": "/usr/bin"}, env={"PATH": "/x"})
    assert result["PATH"] == "/x"


def test_pass_env_comes_from_env_file_last_before_jobsctl_vars(tmp_path: Path) -> None:
    file = secrets(tmp_path, "TOKEN=t0k3n-value\nOTHER=nope\nEMPTY=\n")
    result = build(
        cfg(env={"set": {"TOKEN": "set"}}),
        {},
        file,
        env={"TOKEN": "env"},
        pass_env=["TOKEN", "EMPTY"],
    )
    assert result["TOKEN"] == "t0k3n-value"
    assert result["EMPTY"] == ""
    assert "OTHER" not in result


def test_env_file_values_never_reach_job_without_pass_env(tmp_path: Path) -> None:
    file = secrets(tmp_path, "TOKEN=t0k3n-value\n")
    result = build(cfg(env={"inherit": ["TOKEN"]}), {"TOKEN": "inherited"}, file)
    assert result["TOKEN"] == "inherited"
    assert "t0k3n-value" not in result.values()


def test_pass_env_can_pass_a_hook_ignored_name(tmp_path: Path) -> None:
    file = secrets(tmp_path, "LD_PRELOAD=/x.so\n")
    assert build(cfg(), {}, file, pass_env=["LD_PRELOAD"])["LD_PRELOAD"] == "/x.so"


def test_jobsctl_vars_are_last_and_cannot_be_overridden(tmp_path: Path) -> None:
    config = cfg(env={"set": {"JOBSCTL_JOB_ID": "evil", "JOBSCTL_ATTEMPT": "9"}})
    result = build(config)
    assert result["JOBSCTL_JOB_ID"] == "jb-abcdefgh"
    assert result["JOBSCTL_ATTEMPT"] == "2"


def test_pass_env_returns_values_read_for_masking(tmp_path: Path) -> None:
    file = secrets(tmp_path, "A=aaaaaaaaaa\nB=bbbbbbbbbb\n")
    result = ej.build_job_env(
        cfg(), ec.CanonicalEnv({}), file, job_id="jb-abcdefgh", attempt=1, env={}, pass_env=["A"]
    )
    assert dict(result.pass_env_values) == {"A": "aaaaaaaaaa"}


@pytest.mark.parametrize("state", ["undefined", "unavailable", "not_configured"])
def test_missing_env_cases(tmp_path: Path, state: str) -> None:
    if state == "undefined":
        file = secrets(tmp_path, "OTHER=1\n")
    elif state == "unavailable":
        file = secrets(tmp_path, "WANT=1\n")
        (tmp_path / "s" / "env").chmod(0o644)
        file = ef.load_env_file(str(tmp_path / "s" / "env"), check_ancestors_from=tmp_path)
        assert file.status == "unavailable"
    else:
        file = NOFILE
    with pytest.raises(ej.MissingEnvError) as caught:
        build(cfg(), {}, file, pass_env=["WANT"])
    assert caught.value.variable == "WANT"
    assert caught.value.reason == state
    assert "WANT" in str(caught.value)


def test_first_missing_variable_is_named(tmp_path: Path) -> None:
    file = secrets(tmp_path, "B=bbbbbbbbbb\n")
    with pytest.raises(ej.MissingEnvError) as caught:
        build(cfg(), {}, file, pass_env=["B", "Z1", "Z2"])
    assert caught.value.variable == "Z1"


def test_no_pass_env_needs_no_env_file() -> None:
    unavailable = ef.EnvFile("unavailable")
    assert build(cfg(), {}, unavailable)["JOBSCTL_ATTEMPT"] == "2"


def test_error_never_contains_values(tmp_path: Path) -> None:
    file = secrets(tmp_path, "HAVE=sup3rs3cretvalue\n")
    with pytest.raises(ej.MissingEnvError) as caught:
        build(cfg(), {}, file, pass_env=["HAVE", "NOPE"])
    assert "sup3rs3cretvalue" not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("name", ["JOBSCTL_X", "JOBSCTL_JOB_ID", "JOBSCTL_"])
def test_reserved_names_refused_in_env_and_pass_env(tmp_path: Path, name: str) -> None:
    file = secrets(tmp_path, f"{name}=forged-value-1\n")
    with pytest.raises(ej.MissingEnvError) as caught:
        build(cfg(), {}, file, pass_env=[name])
    assert caught.value.reason == "reserved"
    with pytest.raises(ej.MissingEnvError) as caught:
        build(cfg(), {}, file, env={name: "x"})
    assert caught.value.reason == "reserved"


@pytest.mark.parametrize("name", ["", "1A", "A B", "A=B", "A\x00", "N" * 129])
def test_invalid_names_refused(name: str) -> None:
    with pytest.raises(ej.MissingEnvError):
        build(cfg(), {}, env={name: "x"})
    with pytest.raises(ej.MissingEnvError):
        build(cfg(), {}, pass_env=[name])


def test_duplicate_pass_env_names_are_fine(tmp_path: Path) -> None:
    file = secrets(tmp_path, "A=aaaaaaaaaa\n")
    assert build(cfg(), {}, file, pass_env=["A", "A"])["A"] == "aaaaaaaaaa"
