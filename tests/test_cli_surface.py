"""The argparse surface: every command and flag of interface.md Commands and Global flags."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from jobsctl.cli import build_parser, parse_args
from jobsctl.errors import JobsctlError

GLOBAL = {"--json", "--config", "--verbose", "--quiet"}
MUTATING = {"--actor", "--decision-id", "--dry-run", "--yes"}
SELECTORS = {"--queue", "--label", "--unique-key"}

# command key -> the flags interface.md lists for it (global and -h excluded).
EXPECTED_FLAGS: dict[str, set[str]] = {
    "add": MUTATING
    | {
        "--queue",
        "--unique-key",
        "--priority",
        "--top",
        "--pin",
        "--cwd",
        "--env",
        "--pass-env",
        "--label",
        "--timeout",
        "--max-retries",
        "--kill-grace",
        "--no-start",
        "--wait",
        "--wait-timeout",
    },
    "list": SELECTORS | {"--status", "--all", "--limit"},
    "show": {"--unique-key", "--attempt", "--no-output"},
    "wait": {"--timeout"},
    "move": MUTATING | {"--before", "--after", "--top", "--bottom"},
    "pin": MUTATING,
    "unpin": MUTATING,
    "priority": MUTATING,
    "hold": MUTATING,
    "release": MUTATING | {"--override-escalation"},
    "resolve": MUTATING | {"--as", "--note-file"},
    "cancel": MUTATING | SELECTORS | {"--all", "--force", "--wait-timeout"},
    "retry": MUTATING | {"--top", "--attempt", "--timeout", "--max-retries", "--kill-grace"},
    "label add": MUTATING,
    "label remove": MUTATING,
    "capacity": {"--queue"},
    "queues": set(),
    "run": {"--loop", "--interval", "--max-starts"},
    "reconcile": {"--dry-run"},
    "prune": MUTATING | {"--older-than"},
    "export": {"--audit", "--log", "--since", "--after-seq"},
    "cron install": {"--dry-run"},
    "cron uninstall": set(),
    "browse": {"--focus", "--result-file"},
    "_shim": set(),
    "_deliver": set(),
    "_kill": set(),
}


def _leaf_parsers(
    parser: argparse.ArgumentParser, prefix: str = ""
) -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                key = f"{prefix} {name}".strip()
                nested = _leaf_parsers(sub, key)
                if nested:
                    found.update(nested)
                else:
                    found[key] = sub
    return found


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    out: set[str] = set()
    for action in parser._actions:
        out.update(o for o in action.option_strings if o.startswith("--"))
    return out - GLOBAL - {"--help"}


def test_every_command_exists_with_exactly_the_documented_flags() -> None:
    leaves = _leaf_parsers(build_parser())
    assert set(leaves) == set(EXPECTED_FLAGS)
    for key, parser in leaves.items():
        assert _flags(parser) == EXPECTED_FLAGS[key], key


def test_global_flags_are_accepted_by_every_command() -> None:
    for key, parser in _leaf_parsers(build_parser()).items():
        flags = {o for a in parser._actions for o in a.option_strings}
        assert GLOBAL <= flags, key


def test_internal_commands_are_hidden_from_help() -> None:
    text = build_parser().format_help()
    for hidden in ("_shim", "_deliver", "_kill"):
        assert hidden not in text
    assert "add" in text


JID = "jb-abcd2345"

# argv -> subset of attributes the parsed namespace must have.
VALID: list[tuple[list[str], dict[str, Any]]] = [
    (["queues", "--quiet"], {"quiet": True}),
    (
        [
            "add",
            "--queue",
            "q1",
            "--unique-key",
            "k:1",
            "--priority",
            "-5",
            "--top",
            "--pin",
            "--cwd",
            "/a/b/../c",
            "--env",
            "A=b=c",
            "--env",
            "B=",
            "--pass-env",
            "TOKEN",
            "--label",
            "k=v=w",
            "--label",
            "x=",
            "--timeout",
            "10m",
            "--max-retries",
            "100",
            "--kill-grace",
            "2h",
            "--no-start",
            "--wait",
            "--wait-timeout",
            "30s",
            "--actor",
            "human:me",
            "--decision-id",
            "d1",
            "--decision-id",
            "d2",
            "--yes",
            "--",
            "echo",
            "--json",
            "-x",
        ],
        {
            "command_key": "add",
            "queue": "q1",
            "unique_key": "k:1",
            "priority": -5,
            "top": True,
            "pin": True,
            "cwd": "/a/c",
            "env": [("A", "b=c"), ("B", "")],
            "pass_env": ["TOKEN"],
            "label": [("k", "v=w"), ("x", "")],
            "timeout": 600,
            "max_retries": 100,
            "kill_grace": 7200,
            "no_start": True,
            "wait": True,
            "wait_timeout": 30,
            "actor": "human:me",
            "decision_id": ["d1", "d2"],
            "yes": True,
            "argv": ["echo", "--json", "-x"],
            "json": False,
        },
    ),
    (["add", "--timeout", "none", "--", "true"], {"timeout": "none", "argv": ["true"]}),
    (["--json", "add", "--", "true"], {"json": True}),
    (["add", "--json", "--", "true"], {"json": True}),
    (["add", "--", "--dry-run"], {"dry_run": False, "argv": ["--dry-run"]}),
    (
        [
            "list",
            "--queue",
            "q",
            "--status",
            "queued",
            "--status",
            "held",
            "--label",
            "a=b",
            "--unique-key",
            "k",
            "--all",
            "--limit",
            "5",
        ],
        {"status": ["queued", "held"], "all": True, "limit": 5},
    ),
    (["show", JID, "--attempt", "2", "--no-output"], {"id": JID, "attempt": 2, "no_output": True}),
    (["show", "--unique-key", "K"], {"id": None, "unique_key": "K"}),
    (["show", "abcd"], {"id": "jb-abcd"}),
    (["show", "jb-abcd"], {"id": "jb-abcd"}),
    (["wait", JID, "--timeout", "1d"], {"timeout": 86400}),
    (["move", JID, "--before", "jb-wxyz"], {"before": "jb-wxyz"}),
    (["move", JID, "--after", "wxyz"], {"after": "jb-wxyz"}),
    (["move", JID, "--top"], {"top": True}),
    (["move", JID, "--bottom", "--dry-run"], {"bottom": True, "dry_run": True}),
    (["pin", JID], {"id": JID}),
    (["unpin", JID, "--actor", "auto:bot"], {"actor": "auto:bot"}),
    (["priority", JID, "-1000"], {"value": -1000}),
    (["priority", JID, "1000"], {"value": 1000}),
    (["hold", JID], {"id": JID}),
    (["release", JID, "--override-escalation"], {"override_escalation": True}),
    (["resolve", JID], {"as_": "done", "note_file": None}),
    (["resolve", JID, "--as", "failed", "--note-file", "-"], {"as_": "failed", "note_file": "-"}),
    (["cancel", JID, "--force", "--wait-timeout", "0"], {"force": True, "wait_timeout": 0}),
    (["cancel", "--queue", "q", "--label", "a=b", "--all"], {"all": True, "id": None}),
    (["cancel", "--unique-key", "k", "--force"], {"force": True}),
    (
        [
            "retry",
            JID,
            "--top",
            "--attempt",
            "3",
            "--timeout",
            "none",
            "--max-retries",
            "0",
            "--kill-grace",
            "5s",
        ],
        {"attempt": 3, "timeout": "none", "max_retries": 0},
    ),
    (
        ["label", "add", JID, "a=b", "c=d"],
        {"command_key": "label add", "labels": [("a", "b"), ("c", "d")]},
    ),
    (["label", "remove", JID, "a=b"], {"command_key": "label remove"}),
    (["capacity", "--queue", "q"], {"queue": "q"}),
    (["queues"], {"command_key": "queues"}),
    (["run", "--loop", "--interval", "5s", "--max-starts", "2"], {"loop": True, "interval": 5}),
    (["reconcile", "--dry-run"], {"dry_run": True}),
    (["prune", "--older-than", "30d", "--dry-run"], {"older_than": 2592000}),
    (
        ["export", "--audit", "--since", "2026-10-31T12:00:00.000Z", "--after-seq", "7"],
        {"audit": True, "after_seq": 7},
    ),
    (["export", "--log", "--since", "1h"], {"log": True}),
    (["cron", "install", "--dry-run"], {"command_key": "cron install", "dry_run": True}),
    (["cron", "uninstall"], {"command_key": "cron uninstall"}),
    (["browse", "--focus", JID, "--result-file", "/x/y"], {"focus": JID, "result_file": "/x/y"}),
    (["_shim", JID, "claim123"], {"command_key": "_shim", "claim_id": "claim123"}),
    (["_deliver"], {"command_key": "_deliver"}),
    (["_kill", JID], {"command_key": "_kill"}),
    (["--config", "rel/c.yaml", "queues"], {"config": "rel/c.yaml"}),
    (["queues", "--verbose"], {"verbose": True, "quiet": False}),
]


@pytest.mark.parametrize(("argv", "expected"), VALID, ids=[" ".join(a[:3]) for a, _ in VALID])
def test_valid_invocations_parse(argv: list[str], expected: dict[str, Any]) -> None:
    ns = parse_args(argv)
    for key, value in expected.items():
        assert getattr(ns, key) == value, key


INVALID: list[list[str]] = [
    ["add", "--queue", "Bad Queue", "--", "x"],
    ["add", "--queue", "-x", "--", "x"],
    ["add", "--queue", "a" * 65, "--", "x"],
    ["add", "--priority", "1001", "--", "x"],
    ["add", "--priority", "x", "--", "x"],
    ["add", "--cwd", "relative", "--", "x"],
    ["add", "--cwd", "", "--", "x"],
    ["add", "--env", "NOEQUALS", "--", "x"],
    ["add", "--env", "JOBSCTL_X=1", "--", "x"],
    ["add", "--env", "1A=1", "--", "x"],
    ["add", "--pass-env", "JOBSCTL_X", "--", "x"],
    ["add", "--pass-env", "A-B", "--", "x"],
    ["add", "--label", "nokey", "--", "x"],
    ["add", "--label", "UP=v", "--", "x"],
    ["add", "--label", "k=sp ace", "--", "x"],
    ["add", "--timeout", "0s", "--", "x"],
    ["add", "--timeout", "1w", "--", "x"],
    ["add", "--timeout", "-1m", "--", "x"],
    ["add", "--timeout", "", "--", "x"],
    ["add", "--kill-grace", "none", "--", "x"],
    ["add", "--max-retries", "101", "--", "x"],
    ["add", "--max-retries", "-1", "--", "x"],
    ["add", "--unique-key", "", "--", "x"],
    ["add", "--unique-key", "a b", "--", "x"],
    ["add", "--wait-timeout", "0s", "--wait", "--", "x"],
    ["add", "--actor", "bad", "--", "x"],
    ["add", "--actor", "human:", "--", "x"],
    ["add", "--actor", "robot:x", "--", "x"],
    ["add", "--decision-id", "bad id", "--", "x"],
    ["add", "--bogus", "--", "x"],
    ["add", "stray", "--", "x"],
    ["add", "--prio", "1", "--", "x"],  # abbreviations are not accepted
    ["list", "--status", "bogus"],
    ["list", "--limit", "0"],
    ["list", "--limit", "x"],
    ["show", "jb-abc"],
    ["show", "abc"],
    ["show", "jb-ABCD"],
    ["show", "jb-abcd1"],
    ["show", "jb-abcdefghi"],
    ["show", JID, "--attempt", "0"],
    ["wait", JID, "--timeout", "0s"],
    ["move", JID],
    ["move", JID, "--top", "--bottom"],
    ["move", JID, "--before", "jb-ab"],
    ["priority", JID, "1.5"],
    ["priority", JID, "5000"],
    ["priority", JID],
    ["resolve", JID, "--as", "queued"],
    ["retry", JID, "--attempt", "0"],
    ["retry", JID, "--timeout", "0m"],
    ["label", "add", JID],
    ["label", "add", JID, "bad"],
    ["label", "remove", JID, "UP=v"],
    ["label"],
    ["cron"],
    ["cron", "install", "--bogus"],
    ["run", "--interval", "0s"],
    ["run", "--max-starts", "0"],
    ["prune"],
    ["prune", "--older-than", "0d"],
    ["export"],
    ["export", "--audit", "--log"],
    ["export", "--audit", "--since", "yesterday"],
    ["export", "--audit", "--after-seq", "-1"],
    ["nosuchcommand"],
    ["_kill"],
    ["--config"],
]


@pytest.mark.parametrize("argv", INVALID, ids=[" ".join(a[:4]) for a in INVALID])
def test_invalid_invocations_raise_usage(argv: list[str]) -> None:
    with pytest.raises(JobsctlError) as exc:
        parse_args(argv)
    assert exc.value.code == "usage"
    assert exc.value.exit_code == 2


def test_double_dash_for_add_keeps_everything_after_verbatim() -> None:
    ns = parse_args(["add", "--", "sh", "-c", "echo hi", "--", "--queue", "x"])
    assert ns.argv == ["sh", "-c", "echo hi", "--", "--queue", "x"]
    assert ns.queue is None


def test_add_without_double_dash_has_empty_argv() -> None:
    assert parse_args(["add", "--queue", "q"]).argv == []
