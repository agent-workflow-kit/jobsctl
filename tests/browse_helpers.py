"""Headless driving of the browse app (Textual's `run_test` pilot) and reading what it shows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual.pilot import Pilot
from textual.widgets import DataTable, Static

from jobsctl.config import Config
from jobsctl.store_db import OpenMode, Store
from jobsctl.tui.app import BrowseApp, BrowseResult

Script = Callable[[BrowseApp, Pilot[BrowseResult]], Awaitable[None]]


def open_ro() -> Store | None:
    from jobsctl.store_dir import resolve_state_dir

    if not (resolve_state_dir() / "state.db").exists():
        return None
    return Store.open(mode=OpenMode.READ_ONLY)


def drive(
    script: Script,
    *,
    focus: str | None = None,
    config: Config | None = None,
    config_path: Path | None = None,
    open_store: Callable[[], Any] = open_ro,
    refresh: float = 3600.0,
) -> BrowseResult | None:
    """Run `script` against a running app; the result is what the app returned (`None` while it
    still runs when the script ends: the pilot then closes it)."""

    async def main() -> BrowseResult | None:
        app = BrowseApp(
            open_store=open_store,
            config=config or Config(),
            config_path=config_path,
            me_name="kim",
            focus=focus,
            refresh_seconds=refresh,
        )
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await script(app, pilot)
            if app.is_running:
                await pilot.pause()
        return app.return_value

    return asyncio.run(asyncio.wait_for(main(), 60))


def job_rows(app: BrowseApp) -> list[list[str]]:
    table = app.query_one("#jobs", DataTable)
    return [[c.plain for c in table.get_row_at(i)] for i in range(table.row_count)]


def queue_rows(app: BrowseApp) -> list[list[str]]:
    table = app.query_one("#queues", DataTable)
    return [[c.plain for c in table.get_row_at(i)] for i in range(table.row_count)]


def job_ids(app: BrowseApp) -> list[str]:
    return [r[1] for r in job_rows(app)]


def detail(app: BrowseApp) -> str:
    return app.query_one("#detail", Static).visual.plain  # type: ignore[attr-defined]


def status(app: BrowseApp) -> str:
    return app.query_one("#status", Static).visual.plain  # type: ignore[attr-defined]


def everything_shown(app: BrowseApp) -> str:
    return "\n".join(
        [*(" ".join(r) for r in job_rows(app)), *(" ".join(r) for r in queue_rows(app))]
        + [detail(app), status(app)]
    )


def add_quiet(*flags: str) -> str:
    """`add --no-start` from inside a running app (the app owns stdout there, so `capsys` sees
    nothing): the job id."""
    import contextlib
    import io
    import json

    from jobsctl.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(["--json", "add", "--no-start", *flags, "--cwd", "/work", "--", "x"])
    assert code == 0
    return str(json.loads(out.getvalue())["job"]["id"])
