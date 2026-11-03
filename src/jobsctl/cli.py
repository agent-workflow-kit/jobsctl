"""Command-line entry point for jobsctl: parse, check usage, dispatch, print errors."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from jobsctl import commands
from jobsctl.cli_output import print_error
from jobsctl.cli_registry import Context, lookup, os_user_name
from jobsctl.cli_surface import build_parser, check_usage
from jobsctl.errors import ExitCode, JobsctlError, usage_error

__all__ = ["build_parser", "main", "parse_args"]


def _command_of(head: list[str]) -> str | None:
    """The first non-flag token of `head`, skipping the value of `--config`."""
    skip = False
    for token in head:
        if skip:
            skip = False
        elif token == "--config":
            skip = True
        elif not token.startswith("-"):
            return token
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse `argv`; a failure is a `usage` JobsctlError. `add` takes its job argv after `--`."""
    parser = build_parser()
    if "--" in argv and _command_of(argv[: argv.index("--")]) == "add":
        cut = argv.index("--")
        args = parser.parse_args(argv[:cut])
        args.argv = argv[cut + 1 :]
    else:
        args = parser.parse_args(argv)
        if getattr(args, "command_key", None) == "add":
            args.argv = []
    if args.command is None:
        raise usage_error("a command is required (see --help)")
    return args


def _stdin_isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _context(args: argparse.Namespace) -> Context:
    return Context(
        args=args,
        command=args.command_key,
        json=args.json,
        verbose=args.verbose,
        quiet=args.quiet,
        config_path=Path(os.path.abspath(args.config)) if args.config is not None else None,
        stdin_isatty=_stdin_isatty(),
        me_name=os_user_name(),
        explicit_actor=getattr(args, "actor", None),
    )


def _has_flag(argv: list[str], flag: str) -> bool:
    end = argv.index("--") if "--" in argv else len(argv)
    return flag in argv[:end]


def _wants_json(argv: list[str]) -> bool:
    return _has_flag(argv, "--json")


def _dispatch(argv: list[str]) -> tuple[int, bool]:
    """Run the command; returns its exit code and whether `--json` was in effect."""
    try:
        args = parse_args(argv)
    except JobsctlError as err:
        print_error(err, as_json=_wants_json(argv))
        return err.exit_code, _wants_json(argv)
    except Exception as exc:
        if _has_flag(argv, "--verbose"):
            traceback.print_exc()
        err = JobsctlError("internal", f"internal error: {type(exc).__name__}")
        print_error(err, as_json=_wants_json(argv))
        return err.exit_code, _wants_json(argv)
    try:
        check_usage(args)
        # TODO(#3): load and validate config here (`config_invalid`, exit 2).
        commands.load_all()
        handler = lookup(args.command_key)
        if handler is None:
            raise JobsctlError("internal", f"{args.command_key}: not implemented yet")
        return handler(_context(args)), args.json
    except JobsctlError as err:
        print_error(err, as_json=args.json)
        return err.exit_code, args.json
    except KeyboardInterrupt:
        print_error(JobsctlError("interrupted", "interrupted"), as_json=args.json)
        return int(ExitCode.INTERRUPTED), args.json
    except Exception as exc:
        if args.verbose:
            traceback.print_exc()
        err = JobsctlError("internal", f"internal error: {type(exc).__name__}")
        print_error(err, as_json=args.json)
        return err.exit_code, args.json


def main(argv: list[str] | None = None) -> int:
    """Entry point. `--version` and `--help` exit 0 through argparse's SystemExit."""
    code, _ = _dispatch(list(sys.argv[1:] if argv is None else argv))
    return code
