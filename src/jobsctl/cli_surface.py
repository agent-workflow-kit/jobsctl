"""The frozen argparse surface of interface.md (Commands, Global flags) and its usage checks.

One `_add_<command>` function per command declares its flags; `USAGE_CHECKS` holds the
flag-combination rules (exit 2) that argparse cannot express. Behavior lives in
`jobsctl/commands/`.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Callable
from typing import Any

from jobsctl import __version__
from jobsctl import cli_values as v
from jobsctl.errors import usage_error
from jobsctl.textsafe import echo

STATUSES = ("queued", "starting", "running", "held", "done", "failed", "canceled")
MAX_ARGV_ITEMS = 4096
MAX_ARGV_BYTES = 1024 * 1024
MAX_ENTRIES = 256
MAX_LABELS = 32
_INVALID_CHOICE = re.compile(
    r"argument (.+?): invalid choice: (.*) \(choose from (.*)\)", re.DOTALL
)


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose errors are usage errors, not a print-and-exit.

    Messages are built here, not by argparse, wherever argparse would echo input beyond what is
    needed: an unrecognized argument names only the first token up to `=` (a mistyped
    `--env` must not leak its value), an unknown command lists only the visible commands, and
    any other invalid choice is cut at `=`, sanitized and capped.
    """

    def parse_args(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, args: Any = None, namespace: Any = None
    ) -> Any:
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            self.error("unrecognized argument: " + echo(extras[0].partition("=")[0]))
        return parsed

    def error(self, message: str) -> Any:
        raise usage_error(self._message(message))

    def _message(self, message: str) -> str:
        if message.startswith("ambiguous option: "):
            option = message[len("ambiguous option: ") :].rsplit(" could match ", 1)[0]
            return "ambiguous option: " + echo(option.partition("=")[0])
        match = _INVALID_CHOICE.match(message)
        if match is None:
            return message
        argument, token, choices = match.groups()
        try:  # argparse quotes the input with repr()
            literal = ast.literal_eval(token)
        except (ValueError, SyntaxError):
            literal = token
        token = literal if isinstance(literal, str) else token
        name, eq, _ = token.partition("=")  # a mistyped `--env` must not leak its value
        shown = echo(name) + ("=..." if eq else "")
        if argument == "<command>":
            return f"unknown command '{shown}': choose from " + ", ".join(self._visible())
        return f"argument {argument}: invalid choice: '{shown}' (choose from {choices})"

    def _visible(self) -> list[str]:
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                return [name for name in action.choices if not name.startswith("_")]
        return []


def _global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    # Accepted before and after the command; the copies on a subcommand never override a value
    # given before it.
    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument(
        "--json", action="store_true", default=default(False), help="emit machine-readable JSON"
    )
    parser.add_argument(
        "--config", metavar="PATH", default=default(None), help="config file instead of the default"
    )
    parser.add_argument(
        "--verbose", action="store_true", default=default(False), help="more output on stderr"
    )
    parser.add_argument(
        "--quiet", action="store_true", default=default(False), help="less output on stderr"
    )


def _mutating_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--actor", type=v.actor, metavar="ACTOR", help="human:<name> or auto:<name>"
    )
    parser.add_argument(
        "--decision-id", type=v.decision_id, action="append", default=[], metavar="ID"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would change; change nothing"
    )
    parser.add_argument("--yes", action="store_true", help="accepted and ignored")


def _selector_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--queue", type=v.queue_name, metavar="Q")
    parser.add_argument("--label", type=v.label_pair, action="append", metavar="K=V")
    parser.add_argument("--unique-key", type=v.unique_key, metavar="K")


def _command(sub: Any, key: str, help_text: str | None, *, mutating: bool = False) -> Parser:
    """A leaf command parser named by the last word of `key`, with global flags on it."""
    name = key.split()[-1]
    # A subcommand without `help=` is left out of the command list: that hides internal ones.
    shown: dict[str, Any] = {} if help_text is None else {"help": help_text}
    parser = sub.add_parser(name, description=help_text, allow_abbrev=False, **shown)
    _global_flags(parser, suppress=True)
    if mutating:
        _mutating_flags(parser)
    parser.set_defaults(command_key=key)
    return parser


def _add_add(sub: Any) -> None:
    p = _command(sub, "add", "add a job: jobsctl add [options] -- <argv...>", mutating=True)
    p.add_argument("--queue", type=v.queue_name, metavar="Q")
    p.add_argument("--unique-key", type=v.unique_key, metavar="K")
    p.add_argument("--priority", type=v.priority, default=0, metavar="N")
    p.add_argument("--top", action="store_true")
    p.add_argument("--pin", action="store_true")
    p.add_argument("--cwd", type=v.cwd, metavar="PATH")
    p.add_argument("--env", type=v.env_assign, action="append", default=[], metavar="VAR=value")
    p.add_argument("--pass-env", type=v.env_name, action="append", default=[], metavar="VAR")
    p.add_argument("--label", type=v.label_pair, action="append", default=[], metavar="K=V")
    p.add_argument("--timeout", type=v.duration_or_none, metavar="DURATION|none")
    p.add_argument("--max-retries", type=v.max_retries, metavar="N")
    p.add_argument("--kill-grace", type=v.duration, metavar="DURATION")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--wait-timeout", type=v.duration, metavar="DURATION")


def _add_list(sub: Any) -> None:
    p = _command(sub, "list", "list jobs")
    _selector_flags(p)
    p.add_argument("--status", choices=STATUSES, action="append", metavar="S")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=v.positive_int, metavar="N")


def _add_show(sub: Any) -> None:
    p = _command(sub, "show", "show one job")
    p.add_argument("id", nargs="?", type=v.job_id)
    p.add_argument("--unique-key", type=v.unique_key, metavar="K")
    p.add_argument("--attempt", type=v.positive_int, metavar="N")
    p.add_argument("--no-output", action="store_true")


def _add_wait(sub: Any) -> None:
    p = _command(sub, "wait", "wait until a job is terminal or held")
    p.add_argument("id", type=v.job_id)
    p.add_argument("--timeout", type=v.duration, metavar="DURATION")


def _add_move(sub: Any) -> None:
    p = _command(sub, "move", "move a queued or held job", mutating=True)
    p.add_argument("id", type=v.job_id)
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--before", type=v.job_id, metavar="ID2")
    where.add_argument("--after", type=v.job_id, metavar="ID2")
    where.add_argument("--top", action="store_true")
    where.add_argument("--bottom", action="store_true")


def _add_pin(sub: Any) -> None:
    _command(sub, "pin", "pin a job", mutating=True).add_argument("id", type=v.job_id)


def _add_unpin(sub: Any) -> None:
    _command(sub, "unpin", "unpin a job", mutating=True).add_argument("id", type=v.job_id)


def _add_priority(sub: Any) -> None:
    p = _command(sub, "priority", "set a job's priority", mutating=True)
    p.add_argument("id", type=v.job_id)
    p.add_argument("value", type=v.priority, metavar="N")


def _add_hold(sub: Any) -> None:
    _command(sub, "hold", "hold a queued job", mutating=True).add_argument("id", type=v.job_id)


def _add_release(sub: Any) -> None:
    p = _command(sub, "release", "release a held job", mutating=True)
    p.add_argument("id", type=v.job_id)
    p.add_argument("--override-escalation", action="store_true")


def _add_resolve(sub: Any) -> None:
    p = _command(sub, "resolve", "close a held job for good", mutating=True)
    p.add_argument("id", type=v.job_id)
    p.add_argument("--as", dest="as_", choices=("done", "failed", "canceled"), default="done")
    p.add_argument("--note-file", metavar="PATH|-")


def _add_cancel(sub: Any) -> None:
    p = _command(sub, "cancel", "cancel jobs", mutating=True)
    p.add_argument("id", nargs="?", type=v.job_id)
    _selector_flags(p)
    p.add_argument("--all", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--wait-timeout", type=v.duration_or_zero, metavar="DURATION")


def _add_retry(sub: Any) -> None:
    p = _command(sub, "retry", "retry a finished job", mutating=True)
    p.add_argument("id", type=v.job_id)
    p.add_argument("--top", action="store_true")
    p.add_argument("--attempt", type=v.positive_int, metavar="N")
    p.add_argument("--timeout", type=v.duration_or_none, metavar="DURATION|none")
    p.add_argument("--max-retries", type=v.max_retries, metavar="N")
    p.add_argument("--kill-grace", type=v.duration, metavar="DURATION")


def _add_label(sub: Any) -> None:
    group = sub.add_parser("label", help="add or remove labels", allow_abbrev=False)
    _global_flags(group, suppress=True)
    nested = group.add_subparsers(dest="label_command", metavar="<add|remove>", required=True)
    for action in ("add", "remove"):
        p = _command(nested, f"label {action}", f"{action} labels on a job", mutating=True)
        p.add_argument("id", type=v.job_id)
        p.add_argument("labels", nargs="+", type=v.label_pair, metavar="K=V")


def _add_capacity(sub: Any) -> None:
    _command(sub, "capacity", "report capacity").add_argument(
        "--queue", type=v.queue_name, metavar="Q"
    )


def _add_queues(sub: Any) -> None:
    _command(sub, "queues", "list known queues")


def _add_run(sub: Any) -> None:
    p = _command(sub, "run", "run one pass")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=v.duration, metavar="DURATION")
    p.add_argument("--max-starts", type=v.positive_int, metavar="N")


def _add_reconcile(sub: Any) -> None:
    _command(sub, "reconcile", "identity checks, delivery and retention").add_argument(
        "--dry-run", action="store_true"
    )


def _add_prune(sub: Any) -> None:
    p = _command(sub, "prune", "delete old terminal jobs", mutating=True)
    p.add_argument("--older-than", type=v.duration, required=True, metavar="DURATION")


def _add_export(sub: Any) -> None:
    p = _command(sub, "export", "print audit or log records as JSON lines")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--audit", action="store_true")
    which.add_argument("--log", action="store_true")
    p.add_argument("--since", type=v.timestamp_or_duration, metavar="TIMESTAMP|DURATION")
    p.add_argument("--after-seq", type=v.nonneg_int, metavar="N")


def _add_cron(sub: Any) -> None:
    group = sub.add_parser("cron", help="manage the cron entry", allow_abbrev=False)
    _global_flags(group, suppress=True)
    nested = group.add_subparsers(dest="cron_command", metavar="<install|uninstall>", required=True)
    _command(nested, "cron install", "install the cron entry").add_argument(
        "--dry-run", action="store_true"
    )
    _command(nested, "cron uninstall", "remove the cron entry")


def _add_browse(sub: Any) -> None:
    p = _command(sub, "browse", "the TUI")
    p.add_argument("--focus", type=v.job_id, metavar="ID")
    p.add_argument("--result-file", metavar="PATH")


# Internal commands: registered, hidden from help, not part of the stable interface.


def _add_shim(sub: Any) -> None:
    p = _command(sub, "_shim", None)
    p.add_argument("id", type=v.job_id)
    p.add_argument("claim_id", type=v.claim_id)


def _add_deliver(sub: Any) -> None:
    _command(sub, "_deliver", None)


def _add_kill(sub: Any) -> None:
    _command(sub, "_kill", None).add_argument("id", type=v.job_id)


_BUILDERS: tuple[Callable[[Any], None], ...] = (
    _add_add, _add_list, _add_show, _add_wait, _add_move, _add_pin, _add_unpin, _add_priority,
    _add_hold, _add_release, _add_resolve, _add_cancel, _add_retry, _add_label, _add_capacity,
    _add_queues, _add_run, _add_reconcile, _add_prune, _add_export, _add_cron, _add_browse,
    _add_shim, _add_deliver, _add_kill,
)  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="jobsctl",
        description="Generic job queue with concurrency caps, ordering and held jobs.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"jobsctl {__version__}")
    _global_flags(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=Parser)
    for build in _BUILDERS:
        build(sub)
    return parser


# --- usage checks (exit 2) ---------------------------------------------------------------


def _check_global(a: argparse.Namespace) -> None:
    if a.verbose and a.quiet:
        raise usage_error("--verbose and --quiet cannot be combined")


def _check_add(a: argparse.Namespace) -> None:
    argv: list[str] = a.argv
    if not argv:
        raise usage_error("add needs a command: put its argv after --")
    if len(argv) > MAX_ARGV_ITEMS:
        raise usage_error(f"argv has more than {MAX_ARGV_ITEMS} arguments")
    if any("\x00" in item for item in argv):
        raise usage_error("an argv element contains a NUL byte")
    if sum(v.byte_length(item) for item in argv) > MAX_ARGV_BYTES:
        raise usage_error("argv is larger than 1 MiB in total")
    if len(a.env) > MAX_ENTRIES:
        raise usage_error(f"more than {MAX_ENTRIES} --env entries")
    if len(a.pass_env) > MAX_ENTRIES:
        raise usage_error(f"more than {MAX_ENTRIES} --pass-env entries")
    _check_label_count(a.label)
    if a.dry_run and a.wait:
        raise usage_error("--dry-run cannot be combined with --wait")
    if a.wait_timeout is not None and not a.wait:
        raise usage_error("--wait-timeout needs --wait")


def _check_label_count(pairs: list[tuple[str, str]]) -> None:
    if len(set(pairs)) > MAX_LABELS:
        raise usage_error(f"a job has at most {MAX_LABELS} labels", limit=MAX_LABELS)


def _check_show(a: argparse.Namespace) -> None:
    if (a.id is None) == (a.unique_key is None):
        raise usage_error("show needs exactly one of <id> and --unique-key")


def _check_cancel(a: argparse.Namespace) -> None:
    selectors = {
        "--queue": a.queue is not None,
        "--label": bool(a.label),
        "--unique-key": a.unique_key is not None,
    }
    used = [name for name, present in selectors.items() if present]
    if a.id is not None and used:
        raise usage_error("an id cannot be combined with selectors")
    if a.id is None and not used:
        raise usage_error("cancel needs an id or a selector (--queue, --label, --unique-key)")
    if a.all and not used:
        raise usage_error("--all needs a selector")
    if a.force and used and used != ["--unique-key"]:
        raise usage_error("--force cannot be combined with selectors other than --unique-key")
    if a.wait_timeout is not None and not a.force:
        raise usage_error("--wait-timeout needs --force")


def _check_label_add(a: argparse.Namespace) -> None:
    _check_label_count(a.labels)


USAGE_CHECKS: dict[str, Callable[[argparse.Namespace], None]] = {
    "add": _check_add,
    "show": _check_show,
    "cancel": _check_cancel,
    "label add": _check_label_add,
}


def check_usage(a: argparse.Namespace) -> None:
    """Flag-combination rules of the command in `a.command_key`; raises a usage error."""
    _check_global(a)
    check = USAGE_CHECKS.get(a.command_key)
    if check is not None:
        check(a)
