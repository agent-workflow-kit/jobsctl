"""``jobsctl browse [--focus ID] [--result-file PATH]`` (interface.md, TUI).

The TUI is the optional `tui` extra. Textual is imported only inside the handler (every command
module is imported at startup, `commands.load_all`), so without the extra the command exits 2
`tui_missing` and nothing else in jobsctl needs the library.

Order of the checks: the extra (exit 2), a terminal on stdin and stdout (exit 2), the database
(read-only; an older schema is refused like the other read-only commands, a newer one is exit 12
`version_skew`), then the TUI.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from jobsctl.cli_registry import Context, command
from jobsctl.errors import ExitCode, JobsctlError, usage_error
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir

INSTALL_HINT = (
    "browse needs the tui extra: uv tool install 'jobsctl[tui] @ git+https://github.com/"
    "agent-workflow-kit/jobsctl' (or pip install 'jobsctl[tui]')"
)


def _import_app() -> Any:
    try:
        import textual  # noqa: F401  (the extra; nothing else in jobsctl imports it)
    except ImportError:
        raise JobsctlError("tui_missing", INSTALL_HINT) from None
    from jobsctl.tui import app

    return app


def _is_tty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False


def _opener(ctx: Context) -> Any:
    def open_store() -> Store | None:
        """The database read-only; `None` while there is none (nothing is created)."""
        if not os.path.lexists(resolve_state_dir() / "state.db"):
            return None
        return Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY)

    return open_store


@command("browse")
def browse(ctx: Context) -> int:
    app_module = _import_app()
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        raise usage_error("browse needs a terminal on standard input and output")
    open_store = _opener(ctx)
    first = open_store()  # an older schema is refused here, before the screen changes
    if first is not None:
        first.raise_if_newer()  # a newer one is version_skew, exit 12
    app = app_module.BrowseApp(
        open_store=lambda: first if first is not None else open_store(),
        config=ctx.config,
        config_path=ctx.config_path,
        me_name=ctx.me_name,
        focus=ctx.args.focus,
    )
    result = app.run()
    if result is None:
        raise JobsctlError("internal", "browse stopped unexpectedly")
    if result.error is not None:
        raise result.error
    if ctx.args.result_file is not None:
        from jobsctl.tui.result import write_result

        write_result(ctx.args.result_file, result.intent, list(result.selected))
    return int(ExitCode.INTERRUPTED) if result.interrupted else 0
