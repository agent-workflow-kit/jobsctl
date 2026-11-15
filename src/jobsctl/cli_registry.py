"""Command registry and the per-invocation context handed to every command handler.

A command lives in its own module under `jobsctl/commands/` and registers itself:

    @command("cancel")
    def cancel(ctx: Context) -> int: ...

Modules there are imported automatically, so adding a command touches no shared file. A handler
returns its exit code, prints its own output, and raises `JobsctlError` for a failure; the
dispatcher prints the error envelope. The usage checks of `cli_surface.check_usage` have already
passed when a handler runs; the rest of the check order is `jobsctl.checks.run_checks`.
"""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jobsctl.actors import default_actor, require_human
from jobsctl.config import Config

Handler = Callable[["Context"], int]
_HANDLERS: dict[str, Handler] = {}


def command(key: str) -> Callable[[Handler], Handler]:
    """Register `handler` for a command key such as `add`, `label add` or `cron install`."""

    def register(handler: Handler) -> Handler:
        if key in _HANDLERS:
            raise ValueError(f"command {key!r} is registered twice")
        _HANDLERS[key] = handler
        return handler

    return register


def lookup(key: str) -> Handler | None:
    return _HANDLERS.get(key)


def os_user_name() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError, ImportError):
        return "unknown"


@dataclass(frozen=True)
class Context:
    args: argparse.Namespace
    command: str
    json: bool
    verbose: bool
    quiet: bool
    config_path: Path | None  # the absolute `--config`, for hand-off (`config_files.handoff_args`)
    config: Config
    stdin_isatty: bool
    me_name: str  # `me.name` from config, else the OS user name
    explicit_actor: str | None

    @property
    def actor(self) -> str:
        return self.explicit_actor or default_actor(
            me_name=self.me_name, stdin_isatty=self.stdin_isatty
        )

    def require_human(self, flag: str) -> None:
        """The human-only gate; call it last, and only when `flag` would take effect."""
        require_human(flag, stdin_isatty=self.stdin_isatty, explicit_actor=self.explicit_actor)
