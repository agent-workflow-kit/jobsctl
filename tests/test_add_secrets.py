"""``jobsctl add``: secret values in --queue, --unique-key, --label and --cwd are refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.test_add import (  # noqa: F401  (the autouse fixtures of test_add)
    add,
    clean_path,
    log_records,
    ok,
    trusted_env_file,
    write_config,
    write_env_file,
)

pytestmark = pytest.mark.usefixtures("validated_records")

# Lowercase so it is also a valid queue name; every character is valid in a unique key, a label
# value and a label key.
TOKEN = "tok_abcdefgh_12345"


def cfg_for(tmp_path: Path, value: str = TOKEN) -> str:
    return write_config(tmp_path, env_file=write_env_file(tmp_path, f"API_TOKEN={value}\n"))


def state_text() -> str:
    """Everything jobsctl wrote, as text: the log, the audit and the database bytes."""
    home = resolve_state_dir()
    if not home.exists():
        return ""
    return "".join(p.read_bytes().decode("latin-1") for p in sorted(home.glob("*")) if p.is_file())


def assert_clean(res: Result, *values: str) -> None:
    for value in values:
        assert value not in res.out and value not in res.err and value not in state_text()


def secret_flags(token: str) -> dict[str, tuple[list[str], dict[str, str]]]:
    return {
        "queue": (["--queue", token], {"code": "secret_in_queue"}),
        "unique_key": (["--unique-key", token], {"code": "secret_in_unique_key"}),
        "label_value": (
            ["--label", f"ticket={token}"],
            {"code": "secret_in_label", "label": "ticket"},
        ),
        "label_key": (["--label", f"{token}=1"], {"code": "secret_in_label"}),
        "cwd": (["--cwd", f"/srv/{token}/work"], {"code": "secret_in_cwd"}),
    }


FIELDS = list(secret_flags(TOKEN))
WORDING = {
    "queue": "--queue contains a secret value",
    "unique_key": "--unique-key contains a secret value",
    "label_value": "the value of label ticket contains a secret value",
    "label_key": "a label key contains a secret value",
    "cwd": "the working directory contains a secret value",
}


def run_add(capsys: pytest.CaptureFixture[str], cfg: str, flags: list[str]) -> Result:
    # The cwd flag of the helper is overridden by a later --cwd in `flags` (argparse: last wins).
    return run_cli(capsys, "--json", "add", "--config", cfg, *flags, "--cwd", "/work", "--", "true")


def run_add_cwd(capsys: pytest.CaptureFixture[str], cfg: str, flags: list[str]) -> Result:
    if "--cwd" in flags:
        return run_cli(capsys, "--json", "add", "--config", cfg, *flags, "--", "true")
    return run_add(capsys, cfg, flags)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("dry_run", [False, True])
def test_a_secret_in_a_field_is_refused_with_exit_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str, dry_run: bool
) -> None:
    flags, details = secret_flags(TOKEN)[field]
    res = run_add_cwd(capsys, cfg_for(tmp_path), [*flags, *(["--dry-run"] if dry_run else [])])
    assert res.code == 2
    error = res.envelope()["error"]
    assert error["code"] == "usage"
    assert error["details"] == details
    validate_against("cli/error.json", res.envelope())
    assert_clean(res, TOKEN)
    assert not (resolve_state_dir() / "state.db").exists()
    assert not (resolve_state_dir() / "log.jsonl").exists()


@pytest.mark.parametrize("field", FIELDS)
def test_the_human_message_names_the_field_and_not_the_value(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    flags, _ = secret_flags(TOKEN)[field]
    if "--cwd" not in flags:
        flags = [*flags, "--cwd", "/work"]
    res = run_cli(capsys, "add", "--config", cfg_for(tmp_path), *flags, "--", "true")
    assert res.code == 2 and res.out == ""
    assert WORDING[field] in res.err
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("field", ["queue", "unique_key", "label_value", "cwd"])
@pytest.mark.parametrize("padding", [("pre-", "-post"), ("x", ""), ("", "x"), ("a.b/", "/c")])
def test_a_secret_with_padding_or_inside_a_longer_value_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str, padding: tuple[str, str]
) -> None:
    left, right = padding
    if field == "queue" and ("/" in left or "." in left):
        pytest.skip("not a valid queue name")
    padded = f"{left}{TOKEN}{right}"
    flags, details = secret_flags(padded)[field]
    res = run_add_cwd(capsys, cfg_for(tmp_path), flags)
    assert res.code == 2 and res.envelope()["error"]["details"] == details
    assert_clean(res, TOKEN, padded)


def test_a_cwd_whose_normalization_drops_the_secret_is_refused_as_given(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    res = run_add_cwd(capsys, cfg_for(tmp_path), ["--cwd", f"/work/{TOKEN}/.."])
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_cwd"}
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "shape", ["/srv//{t}/work", "/srv/{t}/", "/srv/./{t}/work", "/srv/{t}/../{t}//", "/srv//{t}"]
)
def test_a_cwd_that_normalization_would_keep_readable_is_refused_as_given(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, shape: str, dry_run: bool
) -> None:
    raw = shape.format(t=TOKEN)
    flags = ["--cwd", raw, *(["--dry-run"] if dry_run else [])]
    res = run_add_cwd(capsys, cfg_for(tmp_path), flags)
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_cwd"}
    assert_clean(res, TOKEN)
    assert not (resolve_state_dir() / "state.db").exists()
    assert not (resolve_state_dir() / "log.jsonl").exists()


def test_a_default_cwd_holding_a_secret_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / TOKEN
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setenv("PWD", str(here))
    res = run_cli(capsys, "--json", "add", "--config", cfg_for(tmp_path), "--", "true")
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_cwd"}
    assert_clean(res, TOKEN)


@pytest.mark.parametrize("field", ["queue", "unique_key", "label_value", "label_key", "cwd"])
def test_near_misses_are_accepted(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    cfg = cfg_for(tmp_path)
    for near in (TOKEN[:-1], TOKEN[1:], TOKEN[:-1] + "6", TOKEN.replace("_", "-"), TOKEN.upper()):
        if field in ("queue", "label_key"):
            if near == TOKEN.upper():
                continue  # these grammars are lowercase: the upper-case near miss is invalid
        flags, _ = secret_flags(near)[field]
        doc = ok(run_add_cwd(capsys, cfg, flags))
        assert doc["created"] is True


@pytest.mark.parametrize("field", ["queue", "unique_key", "label_value", "label_key", "cwd"])
def test_the_masking_length_threshold_is_eight_characters(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    eight, seven = "abcd1234", "abcd123"
    cfg = cfg_for(tmp_path, eight)
    flags, details = secret_flags(eight)[field]
    refused = run_add_cwd(capsys, cfg, flags)
    assert refused.code == 2 and refused.envelope()["error"]["details"] == details
    assert_clean(refused, eight)
    # A 7-character value is not masked, so not a secret.
    short_cfg = cfg_for(tmp_path, seven)
    flags, _ = secret_flags(seven)[field]
    assert ok(run_add_cwd(capsys, short_cfg, flags))["created"] is True


def test_a_value_from_mask_env_is_refused_in_a_field(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MASKED_VAR", TOKEN)
    cfg = write_config(tmp_path, mask_env=["MASKED_VAR"], env={"inherit": ["MASKED_VAR"]})
    res = run_add_cwd(capsys, cfg, ["--unique-key", f"k-{TOKEN}"])
    assert res.code == 2
    assert res.envelope()["error"]["details"] == {"code": "secret_in_unique_key"}
    assert_clean(res, TOKEN)


def test_a_secret_in_a_field_is_refused_before_a_unique_key_hit_and_strict_queues(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(
        tmp_path, strict_queues=True, env_file=write_env_file(tmp_path, f"API_TOKEN={TOKEN}\n")
    )
    res = run_add_cwd(capsys, cfg, ["--queue", TOKEN])
    assert res.code == 2  # not 10 unknown_queue, which would also be echoed nowhere
    assert_clean(res, TOKEN)


def test_a_secret_in_a_field_is_refused_on_a_unique_key_hit_and_nothing_is_logged(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = cfg_for(tmp_path)
    first = run_cli(
        capsys, "--json", "add", "--config", cfg, "--unique-key", "k1", "--cwd", "/work", "--",
        "true",
    )  # fmt: skip
    assert first.code == 0
    before = state_text()
    res = run_cli(
        capsys, "--json", "add", "--config", cfg, "--unique-key", "k1", "--label",
        f"ticket={TOKEN}", "--cwd", "/work", "--", "false",
    )  # fmt: skip
    assert res.code == 2  # not the unique-key hit (exit 0 with the holder)
    assert res.envelope()["error"]["details"] == {"code": "secret_in_label", "label": "ticket"}
    assert log_records("unique_key_reused") == []
    assert state_text() == before
    assert_clean(res, TOKEN)


def test_a_refused_add_changes_nothing_and_a_clean_add_next_to_it_logs_no_secret(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = cfg_for(tmp_path)
    first = ok(run_add_cwd(capsys, cfg, ["--queue", "ci", "--label", "team=core"]))
    before = state_text()
    for field in FIELDS:
        flags, _ = secret_flags(TOKEN)[field]
        assert run_add_cwd(capsys, cfg, flags).code == 2
    assert state_text() == before
    assert first["queue_created"] is True
    assert TOKEN not in before
    assert [r["queue"] for r in log_records("queue_created")] == ["ci"]


def test_non_secret_values_are_unaffected(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = cfg_for(tmp_path)
    doc = ok(
        run_add_cwd(
            capsys,
            cfg,
            ["--queue", "ci", "--unique-key", "build-1", "--label", "team=core", "--cwd", "/srv/x"],
        )
    )
    job = doc["job"]
    assert job["queue"] == "ci" and job["unique_key"] == "build-1" and job["cwd"] == "/srv/x"
    assert job["labels"] == ["team=core"]


def test_a_cwd_error_for_a_relative_path_does_not_echo_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    res = run_add_cwd(capsys, cfg_for(tmp_path), ["--cwd", TOKEN])
    assert res.code == 2
    assert_clean(res, TOKEN)


JOINED = "tok.en=value12345"


def test_a_secret_spanning_the_equals_sign_of_a_label_is_refused_without_naming_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MASKED_VAR", JOINED)
    cfg = write_config(tmp_path, mask_env=["MASKED_VAR"], env={"inherit": ["MASKED_VAR"]})
    res = run_add_cwd(capsys, cfg, ["--label", JOINED])
    assert res.code == 2
    error = res.envelope()["error"]
    assert error["details"] == {"code": "secret_in_label"}
    assert error["message"] == "a label contains a secret value"
    assert "tok.en" not in res.out + res.err + state_text()
    assert not (resolve_state_dir() / "state.db").exists()


def test_a_label_value_secret_overlapping_a_longer_secret_from_the_key_does_not_name_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MASKED_VAR", JOINED)
    monkeypatch.setenv("MASKED_VAR2", "value12345")
    cfg = write_config(
        tmp_path,
        mask_env=["MASKED_VAR", "MASKED_VAR2"],
        env={"inherit": ["MASKED_VAR", "MASKED_VAR2"]},
    )
    res = run_add_cwd(capsys, cfg, ["--label", JOINED])
    assert res.code == 2
    error = res.envelope()["error"]
    assert error["details"] == {"code": "secret_in_label"}
    assert error["message"] == "a label contains a secret value"
    assert "tok.en" not in res.out + res.err + state_text()
