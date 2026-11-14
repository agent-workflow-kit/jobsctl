"""Parse-time errors never print the offending input (interface.md, Commands).

A secret can reach any argument (a mistyped flag, a pasted token) before config and the masking
set exist, so no argparse or grammar error quotes the text it rejects. Each error names the
option or position and the rule instead.
"""

from __future__ import annotations

import pytest

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
