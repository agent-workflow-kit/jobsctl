"""``jobsctl add``: secret values in --actor, --decision-id, --env names and --pass-env names."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.test_add import (  # noqa: F401  (the autouse fixtures of test_add)
    clean_path,
    log_records,
    ok,
    trusted_env_file,
    write_config,
    write_env_file,
)
from tests.test_add_secrets import assert_clean, state_text

pytestmark = pytest.mark.usefixtures("validated_records")

# Valid in every grammar involved: an actor name (no underscore), a decision id and an env name.
TOKEN = "tokabcdefgh12345"


def cfg_for(tmp_path: Path, value: str = TOKEN) -> str:
    return write_config(tmp_path, env_file=write_env_file(tmp_path, f"API_TOKEN={value}\n"))


def name_flags(token: str) -> dict[str, tuple[list[str], str]]:
    return {
        "actor": (["--actor", f"human:{token}"], "secret_in_actor"),
        "decision_id": (["--decision-id", f"dec-{token}"], "secret_in_decision_id"),
        "env_name": (["--env", f"{token}=1"], "secret_in_env_name"),
        "pass_env_name": (["--pass-env", token], "secret_in_pass_env_name"),
    }


FIELDS = list(name_flags(TOKEN))
WORDING = {
    "actor": "--actor contains a secret value",
    "decision_id": "a --decision-id contains a secret value",
    "env_name": "an --env name contains a secret value",
    "pass_env_name": "a --pass-env name contains a secret value",
}


def run_add(capsys: pytest.CaptureFixture[str], cfg: str, flags: list[str]) -> Result:
    return run_cli(capsys, "--json", "add", "--config", cfg, *flags, "--cwd", "/work", "--", "true")


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("dry_run", [False, True])
def test_a_secret_in_a_name_field_is_refused_with_exit_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str, dry_run: bool
) -> None:
    flags, code = name_flags(TOKEN)[field]
    res = run_add(capsys, cfg_for(tmp_path), [*flags, *(["--dry-run"] if dry_run else [])])
    assert res.code == 2
    error = res.envelope()["error"]
    assert error["code"] == "usage"
    assert error["details"] == {"code": code}
    validate_against("cli/error.json", res.envelope())
    assert_clean(res, TOKEN)
    assert not (resolve_state_dir() / "state.db").exists()
    assert not (resolve_state_dir() / "log.jsonl").exists()


@pytest.mark.parametrize("field", FIELDS)
def test_the_human_message_names_the_field_and_not_the_value(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    flags, _ = name_flags(TOKEN)[field]
    res = run_cli(capsys, "add", "--config", cfg_for(tmp_path), *flags, "--cwd", "/work", "--", "x")
    assert res.code == 2 and res.out == ""
    assert WORDING[field] in res.err
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("padding", [("pre-", "-post"), ("x", ""), ("", "x"), ("a.b", "c")])
def test_a_secret_with_padding_or_inside_a_longer_name_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str, padding: tuple[str, str]
) -> None:
    left, right = padding
    if field in ("env_name", "pass_env_name"):
        left, right = left.replace("-", "_").replace(".", "_"), right.replace("-", "_")
        left = left or "a"  # an env name starts with a letter or underscore
    padded = f"{left}{TOKEN}{right}"
    flags, code = name_flags(padded)[field]
    res = run_add(capsys, cfg_for(tmp_path), flags)
    assert res.code == 2 and res.envelope()["error"]["details"] == {"code": code}
    assert_clean(res, TOKEN, padded)


def test_a_secret_in_the_second_of_several_names_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    flags = ["--decision-id", "d1", "--decision-id", f"x{TOKEN}", "--env", "A=1", "--env", "B=2"]
    res = run_add(capsys, cfg_for(tmp_path), flags)
    assert res.envelope()["error"]["details"] == {"code": "secret_in_decision_id"}
    res = run_add(capsys, cfg_for(tmp_path), ["--pass-env", "ok_name", "--pass-env", TOKEN])
    assert res.envelope()["error"]["details"] == {"code": "secret_in_pass_env_name"}
    assert_clean(res, TOKEN)


def test_an_env_name_that_is_a_secret_with_a_secret_value_names_neither(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    res = run_add(capsys, cfg_for(tmp_path), ["--env", f"{TOKEN}={TOKEN}"])
    assert res.envelope()["error"]["details"] == {"code": "secret_in_env_name"}
    assert_clean(res, TOKEN)


def test_an_env_entry_after_a_secret_value_entry_never_names_the_secret_name(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Entry order must not matter: the name check covers every entry before any value check.
    res = run_add(capsys, cfg_for(tmp_path), ["--env", f"A={TOKEN}", "--env", f"{TOKEN}=1"])
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_env_name"}
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("field", FIELDS)
def test_near_misses_are_accepted(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    cfg = cfg_for(tmp_path)
    for near in (TOKEN[:-1], TOKEN[1:], TOKEN[:-1] + "6", TOKEN.upper()):
        flags, _ = name_flags(near)[field]
        assert ok(run_add(capsys, cfg, flags))["created"] is True


@pytest.mark.parametrize("field", FIELDS)
def test_the_masking_length_threshold_is_eight_characters(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    eight, seven = "abcd1234", "abcd123"
    flags, code = name_flags(eight)[field]
    refused = run_add(capsys, cfg_for(tmp_path, eight), flags)
    assert refused.code == 2 and refused.envelope()["error"]["details"] == {"code": code}
    assert_clean(refused, eight)
    flags, _ = name_flags(seven)[field]
    assert ok(run_add(capsys, cfg_for(tmp_path, seven), flags))["created"] is True


@pytest.mark.parametrize("field", FIELDS)
def test_a_secret_name_is_refused_before_the_unique_key_hit_and_pass_env_warning(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    cfg = cfg_for(tmp_path)
    assert run_add(capsys, cfg, ["--unique-key", "k1"]).code == 0
    before = state_text()
    flags, code = name_flags(TOKEN)[field]
    res = run_add(capsys, cfg, ["--unique-key", "k1", *flags])
    assert res.code == 2  # not the unique-key hit, not the pass_env_undefined warning
    assert res.envelope()["error"]["details"] == {"code": code}
    assert state_text() == before
    assert log_records("unique_key_reused") == []
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("field", FIELDS)
def test_a_secret_name_is_refused_before_strict_queues(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    cfg = write_config(
        tmp_path, strict_queues=True, env_file=write_env_file(tmp_path, f"API_TOKEN={TOKEN}\n")
    )
    flags, code = name_flags(TOKEN)[field]
    res = run_add(capsys, cfg, ["--queue", "nowhere", *flags])
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": code}  # not unknown_queue
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("field", FIELDS)
def test_a_mask_env_value_is_refused_in_a_name(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    monkeypatch.setenv("MASKED_VAR", TOKEN)
    cfg = write_config(tmp_path, mask_env=["MASKED_VAR"], env={"inherit": ["MASKED_VAR"]})
    flags, code = name_flags(TOKEN)[field]
    res = run_add(capsys, cfg, flags)
    assert res.envelope()["error"]["details"] == {"code": code}
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize(
    ("flags", "code"),
    [
        (["--env", f"{TOKEN}=1", "--env", f"{TOKEN}=2"], "secret_in_env_name"),
        (["--env", f"{TOKEN}=1", "--pass-env", TOKEN], "secret_in_env_name"),
    ],
)
def test_duplicate_name_errors_never_echo_a_secret_name(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    flags: list[str],
    code: str,
    dry_run: bool,
    json_mode: bool,
) -> None:
    cfg = cfg_for(tmp_path)
    extra = ["--dry-run"] if dry_run else []
    argv = ["add", "--config", cfg, *flags, *extra, "--cwd", "/work", "--", "true"]
    res = run_cli(capsys, *(["--json"] if json_mode else []), *argv)
    assert res.code == 2
    if json_mode:
        assert res.envelope()["error"]["details"] == {"code": code}
    assert_clean(res, TOKEN)


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--env", "A=1", "--env", "A=2"], "--env A is given more than once"),
        (["--env", "A=1", "--pass-env", "A"], "A is given with both --env and --pass-env"),
    ],
)
def test_non_secret_duplicate_names_keep_their_usage_errors(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, flags: list[str], message: str
) -> None:
    res = run_cli(
        capsys, "add", "--config", cfg_for(tmp_path), *flags, "--cwd", "/work", "--", "true"
    )
    assert res.code == 2
    assert message in res.err


def default_actor_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: str, value: str
) -> str:
    monkeypatch.setattr("jobsctl.cli._stdin_isatty", lambda: True)
    monkeypatch.setattr("jobsctl.cli.os_user_name", lambda: user)
    return cfg_for(tmp_path, value)


def test_a_secret_as_the_default_actor_user_name_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_actor_cfg(tmp_path, monkeypatch, TOKEN, TOKEN)
    res = run_add(capsys, cfg, [])
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_actor"}
    assert_clean(res, TOKEN)


def test_a_secret_default_actor_is_reported_as_the_default_not_as_the_flag(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_actor_cfg(tmp_path, monkeypatch, TOKEN, TOKEN)
    res = run_add(capsys, cfg, [])
    assert res.code == 2
    assert res.envelope()["error"]["message"] == (
        "the default actor contains a secret value; pass a different --actor"
    )
    assert_clean(res, TOKEN)


def test_an_explicit_secret_actor_keeps_the_flag_message(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_actor_cfg(tmp_path, monkeypatch, "ann", TOKEN)
    res = run_add(capsys, cfg, ["--actor", f"human:{TOKEN}"])
    assert res.code == 2
    assert res.envelope()["error"]["message"] == "--actor contains a secret value"
    assert res.envelope()["error"]["details"] == {"code": "secret_in_actor"}
    assert_clean(res, TOKEN)


def test_a_raw_user_name_that_sanitizing_would_rewrite_is_still_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "abcd.efgh!ijk"  # sanitized to abcd.efgh_ijk, which is not the masked value
    cfg = default_actor_cfg(tmp_path, monkeypatch, raw, raw)
    res = run_add(capsys, cfg, [])
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_actor"}
    assert_clean(res, raw)


def test_an_explicit_actor_is_not_refused_for_the_raw_user_name(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "abcd.efgh!ijk"
    cfg = default_actor_cfg(tmp_path, monkeypatch, raw, raw)
    assert ok(run_add(capsys, cfg, ["--actor", "human:ann"]))["created"] is True


def test_a_refused_add_changes_nothing_and_non_secret_names_are_unaffected(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = cfg_for(tmp_path)
    doc = ok(
        run_add(
            capsys,
            cfg,
            ["--actor", "human:ann", "--decision-id", "d-1", "--env", "A=1", "--pass-env", "B"],
        )
    )
    assert doc["job"]["env"] == {"A": "1"}
    assert doc["job"]["pass_env"] == ["B"]
    assert doc["job"]["actor"] == "human:ann"
    before = state_text()
    for field in FIELDS:
        flags, _ = name_flags(TOKEN)[field]
        assert run_add(capsys, cfg, flags).code == 2
    assert state_text() == before
