"""Error envelope, exit codes, flag-combination usage errors and the check order."""

from __future__ import annotations

import json

import pytest

from jobsctl.checks import run_checks
from jobsctl.cli_output import print_error
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
    "candidates",
    [[], ["jb-aaaaaaaa"], ["jb-aaaaaaaa", "jb-b"], ["jb-aaaaaaaa", "jb-BBBBBBBB"], ["x", "y"]],
)
def test_ambiguous_needs_two_well_formed_candidates(candidates: list[str]) -> None:
    with pytest.raises(ValueError):
        JobsctlError("ambiguous", "x", candidates=candidates)


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
    # An unrecognized argument is echoed raw by us (not repr-escaped by argparse).
    res = run_cli(capsys, "list", "\x1b]0;pwned\x07\x1b[31mred\x1b[0m\u202e\x01")
    assert res.code == 2
    assert res.err == "jobsctl: error: unrecognized argument: red\\u202e\\u0001\n"


def test_print_error_sanitizes_its_message_and_candidates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = JobsctlError("usage", "a\x1b]0;pwned\x07b\u202e\u2028c")
    print_error(err, as_json=False)
    assert capsys.readouterr().err == "jobsctl: error: ab\\u202e\\u2028c\n"


def test_print_error_cannot_forge_or_erase_an_error_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_error(JobsctlError("usage", "x\njobsctl: error: forged\rerased"), as_json=False)
    err = capsys.readouterr().err
    assert err == "jobsctl: error: x\\njobsctl: error: forged\\rerased\n"
    assert len(err.splitlines()) == 1


def test_echoed_input_cannot_forge_a_second_error_line(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "x\njobsctl: error: forged\roverwrite")
    assert res.code == 2
    assert len(res.err.splitlines()) == 1


@pytest.mark.parametrize("argv", [["list", "--status", "x\n\r"], ["show", "jb-\nabcd"]])
def test_value_errors_escape_line_breaks(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    res = run_cli(capsys, *argv)
    assert res.code == 2
    assert len(res.err.splitlines()) == 1


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
    for bad in ("0s", "0m", "0d", "00"):
        assert run_cli(capsys, "cancel", JID, "--force", "--wait-timeout", bad).code == 2


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


# --- value leaks, huge input, unknown commands, parse-stage failures ----------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "--evn", "SECRET=s3cretvalue", "--", "x"],
        ["add", "--envv=TOKEN=s3cretvalue", "--", "x"],
        ["--evn", "SECRET=s3cretvalue", "add", "--", "x"],
        ["add", "--e=SECRET=s3cretvalue", "--", "x"],
        ["add", "--pass-env", "NAME=s3cretvalue", "--", "x"],
        ["add", "--env", "bad-name=s3cretvalue", "--", "x"],
        ["add", "--env", "A=s3cretvalue", "--evn", "B=s3cretvalue2", "--", "x"],
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_env_values_are_never_echoed_in_any_error(
    capsys: pytest.CaptureFixture[str], argv: list[str], as_json: bool
) -> None:
    res = run_cli(capsys, *(["--json"] if as_json else []), *argv)
    assert res.code == 2
    assert "s3cretvalue" not in res.out + res.err
    if as_json:
        validate_against("cli/error.json", res.envelope())


def test_unrecognized_argument_names_only_the_first_token_up_to_equals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "--json", "add", "--evn", "SECRET=v", "--other", "--", "x")
    message = res.envelope()["error"]["message"]
    assert message == "unrecognized argument: --evn"
    res = run_cli(capsys, "--json", "add", "--envv=TOKEN=v", "--", "x")
    assert res.envelope()["error"]["message"] == "unrecognized argument: --envv"


def test_unrecognized_argument_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "x" * 5000)
    assert res.code == 2
    assert len(res.err) < 200


def test_unknown_command_lists_only_visible_commands(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "bogus")
    assert res.code == 2
    message = res.envelope()["error"]["message"]
    assert message.startswith("unknown command 'bogus': choose from add, list, show, ")
    assert "_shim" not in message
    assert "_deliver" not in message
    assert "_kill" not in message
    assert "queues" in message


def test_unknown_command_echo_is_sanitized(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "\x1b[31mbad\n\u202e")
    assert res.code == 2
    assert res.err.startswith("jobsctl: error: unknown command 'bad\\n\\u202e': choose from ")
    assert len(res.err.splitlines()) == 1


def test_json_message_has_no_lone_surrogates(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["--json", "list", "a\udcffb"],
        ["--json", "list", "--status", "a\udcffb"],
        ["--json", "show", "a\udcffb"],
    ):
        res = run_cli(capsys, *argv)
        message = json.loads(res.out)["error"]["message"]
        message.encode("utf-8")


def test_print_json_replaces_lone_surrogates_with_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_error(
        JobsctlError("usage", "a\udcffb\ud800", details={"k\udcff": ["\udc80"]}), as_json=True
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["message"] == "a\\xffb\\ud800"
    assert doc["error"]["details"] == {"k\\xff": ["\\x80"]}


@pytest.mark.parametrize("value", ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+05:00"])
def test_since_timestamp_overflow_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    res = run_cli(capsys, "--json", "export", "--since", value)
    assert res.code == 2
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["code"] == "usage"


def test_unexpected_exception_while_parsing_is_an_internal_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise OverflowError("secret detail")

    monkeypatch.setattr("jobsctl.cli.build_parser", boom)
    res = run_cli(capsys, "--json", "list")
    assert res.code == 1
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"] == {"code": "internal", "message": "internal error: OverflowError"}
    res = run_cli(capsys, "list")
    assert res.code == 1
    assert "secret detail" not in res.err


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "--limit", "9" * 5000],
        ["show", JID, "--attempt", "9" * 5000],
        ["cancel", JID, "--force", "--wait-timeout", "9" * 5000],
        ["cancel", JID, "--force", "--wait-timeout", "9" * 5000 + "s"],
        ["add", "--timeout", "9" * 5000 + "m", "--", "x"],
        ["add", "--priority", "9" * 5000, "--", "x"],
        ["export", "--since", "9" * 5000 + "h"],
        ["export", "--since", "9" * 5000],
    ],
)
def test_huge_digit_strings_give_short_usage_errors(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    res = run_cli(capsys, *argv)
    assert res.code == 2
    assert len(res.err) < 400
    for internal in ("ValueError", "parse_duration", "int()", "Traceback"):
        assert internal not in res.err


@pytest.mark.parametrize("flag", [["--attempt"], ["--limit"], ["--max-starts"]])
def test_counts_need_at_least_one(capsys: pytest.CaptureFixture[str], flag: list[str]) -> None:
    command = {"--attempt": ["show", JID], "--limit": ["list"], "--max-starts": ["run"]}[flag[0]]
    for bad in ("0", "-1"):
        assert run_cli(capsys, *command, *flag, bad).code == 2


def test_cwd_with_a_newline_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "add", "--cwd", "/a\nb", "--", "x").code == 2


def test_leading_zero_duration_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "add", "--timeout", "05m", "--", "x").code == 2
    assert run_cli(capsys, "add", "--timeout", "5m", "--", "x").code == 1
