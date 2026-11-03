"""Error envelope, exit codes, flag-combination usage errors and the check order."""

from __future__ import annotations

import json

import pytest

from jobsctl.checks import run_checks
from jobsctl.errors import (
    ERROR_EXIT,
    ExitCode,
    JobsctlError,
    refused,
    usage_error,
    wait_exit_code,
)
from tests.conftest import validate_against
from tests.helpers import run_cli

JID = "jb-abcd2345"


def test_version_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli(capsys, "--version")
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("jobsctl ")


def test_exit_code_table_matches_interface() -> None:
    assert ERROR_EXIT == {
        "usage": 2,
        "config_invalid": 2,
        "tui_missing": 2,
        "not_found": 10,
        "unknown_queue": 10,
        "cancel_pending": 12,
        "version_skew": 12,
        "interrupted": 130,
        "needs_human": 13,
        "refused": 20,
        "ambiguous": 21,
        "busy": 1,
        "internal": 1,
    }
    assert 11 not in set(ERROR_EXIT.values()) | {int(c) for c in ExitCode}
    assert ExitCode.WAIT_TIMEOUT == 22


def test_wait_exit_codes() -> None:
    assert [wait_exit_code(s) for s in ("done", "failed", "held", "canceled")] == [0, 12, 13, 20]
    with pytest.raises(ValueError):
        wait_exit_code("running")


def test_error_constructor_rejects_inconsistent_fields() -> None:
    with pytest.raises(ValueError):
        JobsctlError("nosuch", "x")
    with pytest.raises(ValueError):
        JobsctlError("usage", "x", reason="live")
    with pytest.raises(ValueError):
        JobsctlError("refused", "x")
    with pytest.raises(ValueError):
        JobsctlError("refused", "x", reason="nosuch")
    with pytest.raises(ValueError):
        JobsctlError("ambiguous", "x")
    with pytest.raises(ValueError):
        JobsctlError("usage", "x", candidates=["jb-aaaaaaaa", "jb-bbbbbbbb"])


@pytest.mark.parametrize(
    "err",
    [
        usage_error("bad"),
        usage_error("too many labels", limit=32),
        JobsctlError("not_found", "no such job"),
        JobsctlError("unknown_queue", "unknown queue"),
        refused("live", "job is running"),
        refused("duplicate_key", "key held", details={"job_id": JID}),
        JobsctlError("ambiguous", "two", candidates=["jb-aaaaaaaa", "jb-bbbbbbbb"]),
        JobsctlError("needs_human", "needs a human"),
        JobsctlError("cancel_pending", "pending", details={"job_id": JID}),
        JobsctlError("version_skew", "upgraded"),
        JobsctlError("internal", "boom"),
        JobsctlError("busy", "busy"),
        JobsctlError("config_invalid", "bad key"),
        JobsctlError("tui_missing", "install textual"),
        JobsctlError("interrupted", "interrupted"),
    ],
)
def test_every_error_envelope_validates_against_schema(err: JobsctlError) -> None:
    validate_against("cli/error.json", err.envelope())
    assert err.exit_code == ERROR_EXIT[err.code]


def test_json_usage_error_prints_envelope_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "add")
    assert res.code == 2
    assert res.err == ""
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["code"] == "usage"


def test_json_flag_after_command_and_on_parse_failure(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "--status", "bogus", "--json")
    assert res.code == 2
    validate_against("cli/error.json", res.envelope())


def test_human_usage_error_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "add")
    assert res.code == 2
    assert res.out == ""
    assert "empty" in res.err or "argv" in res.err


def test_no_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys)
    assert res.code == 2
    res = run_cli(capsys, "--json")
    validate_against("cli/error.json", res.envelope())


def test_human_error_message_sanitizes_echoed_input(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "--status", "\x1b[31mred\x1b[0m‮\x07")
    assert res.code == 2
    assert "\x1b" not in res.err
    assert "\x07" not in res.err
    assert "‮" not in res.err
    assert "\\u202e" in res.err


def test_surrogate_escaped_argv_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "show", "bad\udcff")
    assert res.code == 2
    assert "\\xff" in res.err
    res = run_cli(capsys, "list", "--status", "bad\udcff")
    assert res.code == 2


def test_surrogate_in_json_error_is_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--status", "bad\udcff")
    assert res.code == 2
    json.loads(res.out)


def test_env_value_never_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "add", "--env", "bad-name=s3cretvalue", "--", "x")
    assert res.code == 2
    assert "s3cretvalue" not in res.err
    res = run_cli(capsys, "--json", "add", "--env", "bad-name=s3cretvalue", "--", "x")
    assert "s3cretvalue" not in res.out


def test_unimplemented_command_is_internal_error(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "queues")
    assert res.code == 1
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["code"] == "internal"


USAGE_COMBINATIONS: list[list[str]] = [
    ["add", "--wait-timeout", "5s", "--", "x"],  # --wait-timeout without --wait
    ["add", "--dry-run", "--wait", "--", "x"],
    ["add", "--queue", "q"],  # empty argv
    ["add", "--", "a\x00b"],  # NUL in argv
    ["add", "--env", "A=\x00", "--", "x"],
    ["cancel", JID, "--wait-timeout", "5s"],  # --wait-timeout without --force
    ["cancel", "--unique-key", "k", "--wait-timeout", "5s"],
    ["cancel", "--all"],  # --all without a selector
    ["cancel", JID, "--all"],
    ["cancel"],  # neither id nor selector
    ["cancel", JID, "--queue", "q"],  # id together with selectors
    ["cancel", JID, "--label", "a=b"],
    ["cancel", JID, "--unique-key", "k"],
    ["cancel", "--queue", "q", "--force"],  # selectors with --force
    ["cancel", "--label", "a=b", "--force"],
    ["cancel", "--unique-key", "k", "--queue", "q", "--force"],
    ["cancel", "--unique-key", "k", "--label", "a=b", "--force"],
    ["show"],  # neither id nor --unique-key
    ["show", JID, "--unique-key", "k"],
    ["add", "--actor", "auto:jobsctl", "--", "x"],
    ["cancel", JID, "--actor", "auto:jobsctl"],
    ["prune", "--older-than", "1d", "--actor", "auto:jobsctl"],
    ["queues", "--verbose", "--quiet"],
    ["add", "--priority", "5000", "--", "x"],
]


@pytest.mark.parametrize(
    "argv", USAGE_COMBINATIONS, ids=[" ".join(a)[:50] for a in USAGE_COMBINATIONS]
)
def test_flag_combination_usage_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", *argv)
    assert res.code == 2
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["code"] == "usage"


def test_label_limit_is_usage_with_limit_detail(capsys: pytest.CaptureFixture[str]) -> None:
    labels = [x for i in range(33) for x in ("--label", f"k{i}=v")]
    res = run_cli(capsys, "--json", "add", *labels, "--", "x")
    assert res.code == 2
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["details"]["limit"] == 32


def test_duplicate_labels_count_once(capsys: pytest.CaptureFixture[str]) -> None:
    labels = [x for _ in range(40) for x in ("--label", "a=b")]
    res = run_cli(capsys, "--json", "add", *labels, "--", "x")
    assert res.code == 1  # parses and passes the usage checks; the handler is not implemented


def test_argv_limits(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "add", "--", *(["a"] * 4097))
    assert res.code == 2
    res = run_cli(capsys, "--json", "add", "--", *(["a"] * 4096))
    assert res.code == 1
    res = run_cli(capsys, "--json", "add", "--", "x" * (1024 * 1024 + 1))
    assert res.code == 2
    res = run_cli(capsys, "--json", "add", "--", "x" * (1024 * 1024))
    assert res.code == 1


def test_env_entry_limits(capsys: pytest.CaptureFixture[str]) -> None:
    many = [x for i in range(257) for x in ("--env", f"V{i}=1")]
    assert run_cli(capsys, "add", *many, "--", "x").code == 2
    many = [x for i in range(257) for x in ("--pass-env", f"V{i}")]
    assert run_cli(capsys, "add", *many, "--", "x").code == 2
    big = "A=" + "x" * (128 * 1024 + 1)
    assert run_cli(capsys, "add", "--env", big, "--", "x").code == 2
    ok = "A=" + "x" * (128 * 1024)
    assert run_cli(capsys, "add", "--env", ok, "--", "x").code == 1


def test_cancel_wait_timeout_zero_allowed_with_force(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "cancel", JID, "--force", "--wait-timeout", "0").code == 1
    assert run_cli(capsys, "cancel", JID, "--force", "--wait-timeout", "0s").code == 1


def test_yes_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    a = run_cli(capsys, "--json", "add", "--wait-timeout", "5s", "--", "x")
    b = run_cli(capsys, "--json", "add", "--yes", "--wait-timeout", "5s", "--", "x")
    assert (a.code, a.out) == (b.code, b.out) == (2, a.out)


# --- check order -------------------------------------------------------------------------


def _raiser(err: JobsctlError):
    def fn() -> None:
        raise err

    return fn


USAGE = usage_error("usage")
LOOKUP = JobsctlError("not_found", "unknown id")
STATE = refused("live", "running")
HUMAN = JobsctlError("needs_human", "needs a human")


def test_check_order_usage_beats_everything() -> None:
    with pytest.raises(JobsctlError) as exc:
        run_checks(
            usage=_raiser(USAGE), lookup=_raiser(LOOKUP), state=_raiser(STATE), human=_raiser(HUMAN)
        )
    assert exc.value is USAGE
    assert exc.value.exit_code == 2


def test_check_order_not_found_beats_refusal() -> None:
    with pytest.raises(JobsctlError) as exc:
        run_checks(lookup=_raiser(LOOKUP), state=_raiser(STATE), human=_raiser(HUMAN))
    assert exc.value.exit_code == 10


def test_check_order_ambiguous_is_in_the_lookup_stage() -> None:
    amb = JobsctlError("ambiguous", "two", candidates=["jb-aaaaaaaa", "jb-bbbbbbbb"])
    with pytest.raises(JobsctlError) as exc:
        run_checks(lookup=_raiser(amb), state=_raiser(STATE), human=_raiser(HUMAN))
    assert exc.value.exit_code == 21


def test_check_order_refusal_beats_missing_human_gate() -> None:
    with pytest.raises(JobsctlError) as exc:
        run_checks(state=_raiser(STATE), human=_raiser(HUMAN))
    assert exc.value.exit_code == 20


def test_check_order_human_gate_last_and_stages_run_in_order() -> None:
    seen: list[str] = []

    def mark(name: str):
        return lambda: seen.append(name)

    with pytest.raises(JobsctlError) as exc:
        run_checks(
            usage=mark("usage"), lookup=mark("lookup"), state=mark("state"), human=_raiser(HUMAN)
        )
    assert exc.value.exit_code == 13
    assert seen == ["usage", "lookup", "state"]


def test_check_order_all_pass() -> None:
    run_checks()
    run_checks(usage=lambda: None, human=lambda: None)


def test_usage_error_plus_unknown_id_exits_2_through_the_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An invalid flag combination is reported before any lookup of the id could run.
    res = run_cli(capsys, "--json", "cancel", "jb-zzzzzzzz", "--wait-timeout", "5s")
    assert res.code == 2
