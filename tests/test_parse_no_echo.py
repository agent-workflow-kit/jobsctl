"""Parse-time errors never print the offending input (interface.md, Commands).

A secret can reach any argument (a mistyped flag, a pasted token) before config and the masking
set exist, so no argparse or grammar error quotes the text it rejects. Each error names the
option or position and the rule instead.
"""

from __future__ import annotations

import argparse

import pytest

from jobsctl.cli_surface import Parser, build_parser
from tests.conftest import validate_against
from tests.helpers import run_cli

# Upper-case and punctuated on purpose: it breaks the lowercase grammars, and contains no `=`
# (the old cut-at-`=` echo rule would not have hidden it).
SECRET = "Zq9TopSecretToken"
JID = "jb-abcd2345"

# argv (without --json) -> a fragment the message must still carry so it stays usable.
CASES: dict[str, tuple[list[str], str]] = {
    "queue": (["add", "--queue", SECRET, "--", "x"], "--queue"),
    "unique_key": (["add", "--unique-key", SECRET + " x", "--", "x"], "--unique-key"),
    "unique_key_selector": (["list", "--unique-key", SECRET + " x"], "--unique-key"),
    "label_key": (["add", "--label", f"{SECRET}=1", "--", "x"], "--label"),
    "label_value": (["add", "--label", f"ticket={SECRET} x", "--", "x"], "--label"),
    "label_no_equals": (["add", "--label", SECRET, "--", "x"], "--label"),
    "label_positional": (["label", "add", JID, SECRET], "K=V"),
    "env_name": (["add", "--env", f"{SECRET}-bad=v", "--", "x"], "--env"),
    "env_no_equals": (["add", "--env", SECRET, "--", "x"], "--env"),
    "pass_env": (["add", "--pass-env", SECRET + "-x", "--", "x"], "--pass-env"),
    "cwd_relative": (["add", "--cwd", SECRET, "--", "x"], "--cwd"),
    "cwd_newline": (["add", "--cwd", f"/{SECRET}\nx", "--", "x"], "--cwd"),
    "actor": (["add", "--actor", SECRET, "--", "x"], "--actor"),
    "actor_reserved_shape": (["add", "--actor", f"robot:{SECRET}", "--", "x"], "--actor"),
    "decision_id": (["add", "--decision-id", SECRET + " x", "--", "x"], "--decision-id"),
    "priority": (["add", "--priority", SECRET, "--", "x"], "--priority"),
    "max_retries": (["add", "--max-retries", SECRET, "--", "x"], "--max-retries"),
    "timeout": (["add", "--timeout", SECRET, "--", "x"], "--timeout"),
    "kill_grace": (["add", "--kill-grace", SECRET, "--", "x"], "--kill-grace"),
    "wait_timeout": (["cancel", JID, "--wait-timeout", SECRET], "--wait-timeout"),
    "limit": (["list", "--limit", SECRET], "--limit"),
    "since": (["export", "--audit", "--since", SECRET], "--since"),
    "after_seq": (["export", "--log", "--after-seq", SECRET], "--after-seq"),
    "job_id": (["show", SECRET], "job id"),
    "job_id_before": (["move", JID, "--before", SECRET], "job id"),
    "status_choice": (["list", "--status", SECRET], "--status"),
    "as_choice": (["resolve", JID, "--as", SECRET], "--as"),
    "unknown_command": ([SECRET], "unknown command"),
    "unknown_subcommand": (["label", SECRET], "invalid choice"),
    "unrecognized_flag": (["add", f"--{SECRET}", "--", "x"], "unrecognized argument"),
    "unrecognized_flag_equals": (["add", f"--{SECRET}=v", "--", "x"], "unrecognized argument"),
    "unrecognized_positional": (["list", SECRET], "unrecognized argument"),
    "unrecognized_short": (["add", f"-{SECRET}", "--", "x"], "unrecognized argument"),
    "explicit_argument_on_a_switch": (["add", f"--top={SECRET}", "--", "x"], "--top"),
    "explicit_argument_on_json": ([f"--json={SECRET}", "list"], "--json"),
    "explicit_argument_on_help": ([f"--help={SECRET}"], "--help"),
    "attempt": (["show", JID, "--attempt", SECRET], "--attempt"),
    "retry_attempt": (["retry", JID, "--attempt", SECRET], "--attempt"),
    "max_starts": (["run", "--max-starts", SECRET], "--max-starts"),
    "interval": (["run", "--loop", "--interval", SECRET], "--interval"),
    "older_than": (["prune", "--older-than", SECRET], "--older-than"),
    "move_after": (["move", JID, "--after", SECRET], "job id"),
    "focus": (["browse", "--focus", SECRET], "job id"),
    "capacity_queue": (["capacity", "--queue", SECRET], "--queue"),
    "wait_job_timeout": (["wait", JID, "--timeout", SECRET], "--timeout"),
    # Plain-string options accept any text, so the error comes from a later bad token.
    "note_file": (["resolve", JID, "--note-file", SECRET, "--bogus"], "unrecognized argument"),
    "result_file": (["browse", "--result-file", SECRET, "--bogus"], "unrecognized argument"),
    "config": (["--config", SECRET, "list", "--bogus"], "unrecognized argument"),
    "env_value": (["add", "--env", f"A={SECRET}\x00", "--", "x"], "--env"),
    "abbreviated_option": ([f"--ver={SECRET}", "list"], "unrecognized argument"),
}


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("as_json", [False, True])
def test_a_parse_error_never_echoes_the_input(
    capsys: pytest.CaptureFixture[str], name: str, as_json: bool
) -> None:
    argv, fragment = CASES[name]
    res = run_cli(capsys, *(["--json"] if as_json else []), *argv)
    assert res.code == 2
    assert SECRET not in res.out + res.err
    if as_json:
        validate_against("cli/error.json", res.envelope())
        message = res.envelope()["error"]["message"]
    else:
        message = res.err
    assert fragment in message


def test_the_secret_check_is_not_vacuous(capsys: pytest.CaptureFixture[str]) -> None:
    # The same input in a valid position is accepted, so the cases above fail only on grammar.
    res = run_cli(capsys, "add", "--dry-run", "--", SECRET)
    assert res.code == 0


def test_an_unrecognized_known_option_name_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--top")
    assert res.envelope()["error"]["message"] == "unrecognized argument: --top"


def test_an_unrecognized_unknown_token_is_named_by_position(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "--json", "list", "--queue", "ok", "--evn", "S=v")
    assert res.envelope()["error"]["message"] == "unrecognized argument at position 5"


def test_a_grammar_error_states_the_rule(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "add", "--queue", SECRET, "--", "x")
    message = res.envelope()["error"]["message"]
    assert message == "argument --queue: invalid queue name: use [a-z0-9][a-z0-9_-]{0,63}"


def test_every_value_taking_option_has_a_case() -> None:
    declared: set[str] = set()
    pending = [build_parser()]
    while pending:
        parser = pending.pop()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())
            elif action.option_strings and action.nargs != 0:
                declared.update(o for o in action.option_strings if o.startswith("--"))
    used = {token for argv, _ in CASES.values() for token in argv}
    assert declared - used == set()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["list", "--queue", "ok", "ok"], "unrecognized argument at position 4 or 5"),
        (["list", "--limit", "5", "5"], "unrecognized argument at position 4 or 5"),
        (["add", "--queue", "a", "a", "--", "x"], "unrecognized argument at position 4 or 5"),
        (["list", "z", "q", "z", "z"], "unrecognized argument at position 3, 5 or 6"),
        # Every token counts, `--` included; the stray token after the `--` is the one reported.
        (["list", "--", "z"], "unrecognized argument at position 4"),
    ],
)
def test_a_repeated_unrecognized_token_names_every_position(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    res = run_cli(capsys, "--json", *argv)
    assert res.envelope()["error"]["message"] == message


def test_a_token_after_a_double_dash_counts_from_the_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "list", "--", SECRET)
    assert res.err == "jobsctl: error: unrecognized argument at position 3\n"


def test_the_double_dash_itself_is_skipped_when_a_stray_token_follows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "list", "--limit", "1", "--", SECRET, SECRET)
    assert res.err == "jobsctl: error: unrecognized argument at position 5 or 6\n"


def test_a_lone_double_dash_is_reported_by_its_own_position(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "list", "--")
    assert res.err == "jobsctl: error: unrecognized argument at position 2\n"


def test_a_long_position_list_is_capped_at_eight(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", *[SECRET] * 4000)
    assert res.err == (
        "jobsctl: error: unrecognized argument at position 2, 3, 4, 5, 6, 7, 8, 9,"
        " or later (4000 occurrences in total)\n"
    )


def test_exactly_eight_positions_are_all_named(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", *[SECRET] * 8)
    assert res.err == "jobsctl: error: unrecognized argument at position 2, 3, 4, 5, 6, 7, 8 or 9\n"


def test_env_assign_messages_never_carry_the_value(capsys: pytest.CaptureFixture[str]) -> None:
    nul = run_cli(capsys, "--json", "add", "--env", f"A={SECRET}\x00", "--", "x")
    big = run_cli(capsys, "--json", "add", "--env", "A=" + SECRET + "x" * 140_000, "--", "x")
    for res, text in ((nul, "contains a NUL byte"), (big, "is over 128 KiB")):
        assert res.code == 2
        assert res.envelope()["error"]["message"] == f"argument --env: an --env value {text}"
        assert SECRET not in res.out + res.err
    human = run_cli(capsys, "add", "--env", f"A={SECRET}\x00", "--", "x")
    assert human.err == "jobsctl: error: argument --env: an --env value contains a NUL byte\n"


@pytest.mark.parametrize(
    "message",
    [
        "argument --queue: maybe you meant 'CANARY'",
        "something unknown about 'CANARY'",
        "ambiguous option: --CANARY could match --a, --b",
        "argument --x: invalid <lambda> value: 'CANARY'",
        "argument --x: invalid functools.partial(<f>, 1) value: 'CANARY\nx'",
        "argument --x: invalid choice: 'CANARY' (choose from 'a', 'b')",
        "argument --x: ignored explicit argument 'CANARY'",
    ],
)
def test_message_never_passes_unknown_wording_through(message: str) -> None:
    out = build_parser()._message(message)  # type: ignore[attr-defined]
    assert "CANARY" not in out
    assert "\n" not in out


@pytest.mark.parametrize(
    "message",
    [
        "the following arguments are required: --older-than",
        "the following arguments are required: <add|remove>",
        "one of the arguments --before --after --top --bottom is required",
        "argument --after: not allowed with argument --before",
        "argument --queue: expected one argument",
        "argument K=V: expected at least one argument",
        "argument --x: expected 2 arguments",
    ],
)
def test_message_keeps_known_safe_argparse_wording(message: str) -> None:
    assert build_parser()._message(message) == message  # type: ignore[attr-defined]


def test_an_unknown_fallback_is_generic() -> None:
    parser = build_parser()
    assert isinstance(parser, Parser)
    assert parser._message("argument --queue: maybe you meant 'CANARY'") == "invalid command line"


def test_real_argparse_wording_stays_usable(capsys: pytest.CaptureFixture[str]) -> None:
    cases = [
        (["prune"], "the following arguments are required: --older-than"),
        (["move", JID], "one of the arguments --before --after --top --bottom is required"),
        (["move", JID, "--top", "--bottom"], "not allowed with argument"),
        (["list", "--queue"], "argument --queue: expected one argument"),
        (["label", "add", JID], "required: K=V"),
    ]
    for argv, fragment in cases:
        res = run_cli(capsys, *argv)
        assert res.code == 2
        assert fragment in res.err, (argv, res.err)


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--env", f"{SECRET}=1", "--env", f"{SECRET}=2"], "an --env name is given more than once"),
        (
            ["--env", f"{SECRET}=1", "--pass-env", SECRET],
            "a name is given with both --env and --pass-env",
        ),
    ],
)
def test_duplicate_env_names_are_not_echoed(
    capsys: pytest.CaptureFixture[str], flags: list[str], message: str
) -> None:
    res = run_cli(capsys, "--json", "add", *flags, "--cwd", "/work", "--", "x")
    assert res.code == 2
    assert res.envelope()["error"]["message"] == message
    assert SECRET not in res.out + res.err
