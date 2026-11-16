"""The Textual application of `jobsctl browse` (interface.md, TUI).

Only `commands/browse.py` imports this module, and only inside its handler: nothing else in
jobsctl imports Textual, so `browse` can report `tui_missing`.

Untrusted text. Every string shown comes from `tui/model.py`, already sanitized, and is handed to
the widgets as plain text: `Static(markup=False)` and `rich.text.Text` cells (a `DataTable` cell
given as a `str` would be parsed as markup). Nothing a job, a queue or a hook wrote is ever
interpreted as markup.

Reads. The store is opened read-only and read in short transactions; `Store.raise_if_newer()` runs
before each refresh and a newer schema ends the TUI with `version_skew` (exit 12). Refreshes run
from a timer (no busy loop). Writes. Only the actions of `tui/actions.py`, each a CLI command.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.textsafe import sanitize
from jobsctl.tui import actions, model
from jobsctl.tui.result import Intent

REFRESH_SECONDS: Final = 2.0
RESOLVE_CHOICES: Final = ("done", "failed", "canceled")
HELP_TEXT: Final = """\
j/k, arrows   move            tab   queues / jobs        enter  details (scroll with arrows)
K / J         move job before / after its neighbor        t / b  move to top / bottom
p             pin / unpin     + / -   priority up / down by 1
h             hold            e   release (an escalated hold asks first)
v             resolve (asks for the status and a note)    c   cancel (a running job asks first)
R             retry           r   refresh         /   filter (empty clears)
space         mark for open   o   open the marked jobs (or this one) and quit
?             this help       q   quit
"""


@dataclass(frozen=True)
class BrowseResult:
    """What the app returns: how it ended."""

    intent: Intent = "back"
    selected: tuple[str, ...] = ()
    error: JobsctlError | None = None
    interrupted: bool = False


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question; the text is plain."""

    BINDINGS: ClassVar = [
        Binding("y,enter", "answer(True)", "yes"),
        Binding("n,escape", "answer(False)", "no"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._question, markup=False, id="question")
            yield Static("y = yes, n = no", markup=False)

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar = [Binding("escape,q,question_mark,enter", "close", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(HELP_TEXT, markup=False, id="help")

    def action_close(self) -> None:
        self.dismiss(None)


class ResolveScreen(ModalScreen["tuple[str, str] | None"]):
    """Asks for `--as` (done, failed or canceled) and an optional note."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("resolve as (done, failed, canceled):", markup=False)
            yield Input(value="done", id="as", max_length=16)
            yield Static("note (optional), enter to resolve, escape to cancel:", markup=False)
            yield Input(value="", id="note", max_length=1000)
            yield Static("", markup=False, id="problem")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        status = self.query_one("#as", Input).value.strip()
        if status not in RESOLVE_CHOICES:
            self.query_one("#problem", Static).update("status must be done, failed or canceled")
            self.query_one("#as", Input).focus()
            return
        if event.input.id == "as":
            self.query_one("#note", Input).focus()
            return
        self.dismiss((status, self.query_one("#note", Input).value))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class _State:
    data: model.BrowseData = field(default_factory=model.BrowseData)
    queue: str | None = None
    job: str | None = None
    focus: str | None = None
    filter: str = ""
    marked: set[str] = field(default_factory=set)
    rows: tuple[model.JobItem, ...] = ()


class BrowseApp(App[BrowseResult]):
    CSS = """
    #queues { width: 40; border-right: solid $primary; }
    #right { width: 1fr; }
    #jobs { height: 2fr; }
    #detail-scroll { height: 1fr; border-top: solid $primary; }
    #status { dock: bottom; height: 1; }
    #filter { dock: bottom; }
    ConfirmScreen, HelpScreen, ResolveScreen { align: center middle; }
    #dialog { width: 80%; max-width: 100; height: auto; border: thick $primary; padding: 1 2;
              background: $surface; }
    """
    BINDINGS: ClassVar = [
        Binding("j", "cursor(1)", "down", show=False),
        Binding("k", "cursor(-1)", "up", show=False),
        Binding("K", "move('before')", "move before", show=False),
        Binding("J", "move('after')", "move after", show=False),
        Binding("t", "move('top')", "top", show=False),
        Binding("b", "move('bottom')", "bottom", show=False),
        Binding("p", "pin", "pin", show=False),
        Binding("plus", "priority(1)", "priority up", show=False),
        Binding("minus", "priority(-1)", "priority down", show=False),
        Binding("h", "hold", "hold", show=False),
        Binding("e", "release", "release", show=False),
        Binding("v", "resolve", "resolve", show=False),
        Binding("c", "cancel", "cancel", show=False),
        Binding("R", "retry", "retry", show=False),
        Binding("r", "refresh", "refresh", show=False),
        Binding("slash", "filter", "filter", show=False),
        Binding("space", "mark", "mark", show=False),
        Binding("o", "open", "open", show=False),
        Binding("question_mark", "help", "help", show=False),
        Binding("q", "quit_back", "quit", show=False),
        Binding("ctrl+c", "quit_back", "quit", show=False, priority=True),
        Binding("tab", "switch", "switch", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        open_store: Callable[[], Any],
        config: Config,
        config_path: Path | None,
        me_name: str,
        focus: str | None = None,
        refresh_seconds: float = REFRESH_SECONDS,
    ) -> None:
        super().__init__()
        self._open_store = open_store
        self._store: Any = None
        self._config = config
        self._config_path = config_path
        self._actor = actions.human_actor(me_name)
        self._refresh_seconds = refresh_seconds
        self._state = _State(focus=focus)
        self._populating = False
        self._ending = False

    # -- layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield DataTable(id="queues", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="detail-scroll"):
                    yield Static("", markup=False, id="detail")
        yield Input(placeholder="filter", id="filter", classes="hidden")
        yield Static("", markup=False, id="status")

    def on_mount(self) -> None:
        queues = self.query_one("#queues", DataTable)
        queues.add_columns(*model.QUEUE_COLUMNS)
        jobs = self.query_one("#jobs", DataTable)
        jobs.add_columns(*model.JOB_COLUMNS)
        self.query_one("#filter", Input).display = False
        jobs.focus()
        self._install_signal_handlers()
        self.action_refresh()
        self.set_interval(self._refresh_seconds, self.action_refresh)

    def _install_signal_handlers(self) -> None:
        # SIGINT, SIGTERM and SIGHUP end the TUI through the normal exit path, so the terminal
        # is restored by Textual's driver; without this the process would die in raw mode.
        try:
            loop = asyncio.get_running_loop()
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(number, self._interrupted)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # not the main thread of a Unix loop: the default handlers stay

    def _remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.remove_signal_handler(number)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    def _interrupted(self) -> None:
        self._finish(BrowseResult(interrupted=True))

    def on_unmount(self) -> None:
        self._remove_signal_handlers()
        if self._store is not None:
            self._store.close()
            self._store = None

    def _handle_exception(self, error: Exception) -> None:
        # Textual would print a traceback (with local variables) after restoring the terminal;
        # jobsctl reports `internal error: <type>` and exits 1.
        self._finish(
            BrowseResult(error=JobsctlError("internal", f"internal error: {type(error).__name__}"))
        )

    def _finish(self, result: BrowseResult) -> None:
        if self._ending:
            return
        self._ending = True
        try:
            self.exit(result)
        except Exception:  # the screen stack is already gone
            self._ending = True

    # -- status line -------------------------------------------------------------------

    def _say(self, text: str) -> None:
        try:
            self.query_one("#status", Static).update(sanitize(text, inline=True))
        except Exception:
            pass

    # -- reading -----------------------------------------------------------------------

    def action_refresh(self) -> None:
        try:
            if self._store is None:
                self._store = self._open_store()
            if self._store is None:
                self._show(model.BrowseData(note="no state database yet"))
                return
            if self._store.current_version() == 0:
                self._show(model.BrowseData(note="the state database is empty"))
                return
            self._store.raise_if_newer()
            s = self._state
            data = model.load(self._store, self._config, queue=s.queue, job_id=s.job, focus=s.focus)
        except JobsctlError as err:
            if err.code == "version_skew":
                self._finish(BrowseResult(error=err))
            else:
                self._say(f"{err.code}: {err.message}")
            return
        except Exception as exc:
            self._say(f"internal error: {type(exc).__name__}")
            return
        missing = None
        if self._state.focus is not None:
            if data.detail_id != self._state.focus:
                missing = f"job {self._state.focus} not found"
            self._state.job = data.detail_id
            self._state.focus = None
        self._show(data)
        if missing is not None:
            self._say(missing)

    def _jobs_table(self) -> DataTable[Text]:
        return self.query_one("#jobs", DataTable)

    def _queues_table(self) -> DataTable[Text]:
        return self.query_one("#queues", DataTable)

    def _visible(self, data: model.BrowseData) -> tuple[model.JobItem, ...]:
        needle = self._state.filter.lower()
        return tuple(j for j in data.jobs if needle in j.haystack)

    def _show(self, data: model.BrowseData) -> None:
        """Fill both tables and the detail pane from `data`, keeping the cursors by key."""
        s = self._state
        s.data = data
        s.queue = data.queue
        rows = self._visible(data)
        s.rows = rows
        self._populating = True
        try:
            queues = self._queues_table()
            queues.clear()
            for item in data.queues:
                queues.add_row(*(Text(c) for c in item.cells), key=item.name)
            names = [q.name for q in data.queues]
            if data.queue in names:
                queues.move_cursor(row=names.index(data.queue))
            jobs = self._jobs_table()
            jobs.clear()
            for job in rows:
                mark = "*" if job.id in s.marked else " "
                jobs.add_row(Text(mark), *(Text(c) for c in job.cells), key=job.id)
            ids = [j.id for j in rows]
            if s.job not in ids:
                s.job = ids[0] if ids else None
            if s.job is not None:
                jobs.move_cursor(row=ids.index(s.job))
        finally:
            self._populating = False
        self._show_detail(data)
        extra = "  (list cut)" if data.truncated else ""
        filt = f"  filter: {s.filter}" if s.filter else ""
        self._say(f"{data.note or data.totals}{extra}{filt}")

    def _show_detail(self, data: model.BrowseData | None = None) -> None:
        s = self._state
        text = ""
        if s.job is not None:
            if data is not None and data.detail_id == s.job:
                text = data.detail
            else:
                try:
                    text = model.detail_for(self._store, self._config, s.job) or ""
                except JobsctlError as err:
                    if err.code == "version_skew":
                        self._finish(BrowseResult(error=err))
                    text = ""
                except Exception:
                    text = ""
        self.query_one("#detail", Static).update(text)

    # -- events ------------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._populating or event.row_key.value is None:
            return
        key = str(event.row_key.value)
        table = event.data_table
        try:
            current = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return
        if current != key:  # a stale event: the table was refilled since
            return
        s = self._state
        if event.data_table.id == "queues":
            if key != s.queue:
                s.queue, s.job = key, None
                self.action_refresh()
        elif key != s.job:
            s.job = key
            self._show_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # enter on a row: the details
        self.action_details()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter":
            return
        event.stop()
        self._state.filter = event.value.strip()
        self._close_filter()
        self._show(self._state.data)

    def on_key(self, event: Any) -> None:
        if event.key == "escape" and self.query_one("#filter", Input).display:
            event.stop()
            self._close_filter()

    def _close_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = False
        self._jobs_table().focus()

    # -- navigation --------------------------------------------------------------------

    def _focused_table(self) -> DataTable[Text]:
        focused = self.focused
        return focused if isinstance(focused, DataTable) else self._jobs_table()

    def action_cursor(self, delta: int) -> None:
        table = self._focused_table()
        if delta > 0:
            table.action_cursor_down()
        else:
            table.action_cursor_up()

    def action_switch(self) -> None:
        if self.focused is self._jobs_table():
            self._queues_table().focus()
        else:
            self._jobs_table().focus()

    def action_details(self) -> None:
        self.query_one("#detail-scroll").focus()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = self._state.filter
        box.display = True
        box.focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_mark(self) -> None:
        job = self._current()
        if job is None:
            return
        marked = self._state.marked
        marked.symmetric_difference_update({job.id})
        self._show(self._state.data)

    def action_open(self) -> None:
        job = self._current()
        marked = self._state.marked
        shown = [j.id for j in self._state.rows if j.id in marked]
        hidden = sorted(marked.difference(shown))  # marked in another queue or filtered out
        selected = tuple(shown + hidden or ([job.id] if job else []))
        self._finish(BrowseResult(intent="open", selected=selected))

    def action_quit_back(self) -> None:
        self._finish(BrowseResult())

    # -- actions (each one a CLI command) -----------------------------------------------

    def _current(self) -> model.JobItem | None:
        s = self._state
        return next((j for j in s.rows if j.id == s.job), None)

    def _run(self, argv: list[str]) -> None:
        outcome = actions.run_command(argv, config_path=self._config_path, actor=self._actor)
        if outcome.error_code == "version_skew":
            self._finish(
                BrowseResult(error=JobsctlError("version_skew", outcome.message.split(": ", 2)[-1]))
            )
            return
        self.action_refresh()
        self._say(outcome.message)

    def _with_job(self, build: Callable[[model.JobItem], list[str]]) -> None:
        job = self._current()
        if job is not None:
            self._run(build(job))

    def action_move(self, where: str) -> None:
        job = self._current()
        if job is None:
            return
        rows = self._state.rows
        index = rows.index(job)
        if where == "top":
            self._run(actions.move_top(job.id))
        elif where == "bottom":
            self._run(actions.move_bottom(job.id))
        elif where == "before":
            if index == 0:
                self._say("already first in the list")
            else:
                self._run(actions.move_before(job.id, rows[index - 1].id))
        elif index == len(rows) - 1:
            self._say("already last in the list")
        else:
            self._run(actions.move_after(job.id, rows[index + 1].id))

    def action_pin(self) -> None:
        self._with_job(lambda j: actions.pin(j.id, pinned=j.pinned))

    def action_priority(self, step: int) -> None:
        self._with_job(lambda j: actions.priority(j.id, j.priority + step))

    def action_hold(self) -> None:
        self._with_job(lambda j: actions.hold(j.id))

    def action_retry(self) -> None:
        self._with_job(lambda j: actions.retry(j.id))

    def _confirm(self, question: str, then: Callable[[], None]) -> None:
        def answered(answer: bool | None) -> None:
            if answer:
                then()
            else:
                self._say("not confirmed")

        self.push_screen(ConfirmScreen(question), answered)

    def action_release(self) -> None:
        job = self._current()
        if job is None:
            return
        if job.status == "held" and job.hold_reason == "escalated":
            self._confirm(
                f"Release {sanitize(job.id, inline=True)} over its escalation? A hook asked for "
                f"a human decision; this records {self._actor} as the override.",
                lambda: self._run(actions.release(job.id, override=True)),
            )
        else:
            self._run(actions.release(job.id, override=False))

    def action_cancel(self) -> None:
        job = self._current()
        if job is None:
            return
        if job.status in ("starting", "running"):
            self._confirm(
                f"Force-cancel the live job {sanitize(job.id, inline=True)}? Its process group "
                f"is signaled; this is recorded as {self._actor}.",
                lambda: self._run(actions.cancel(job.id, force=True)),
            )
        else:
            self._run(actions.cancel(job.id, force=False))

    def action_resolve(self) -> None:
        job = self._current()
        if job is None:
            return

        def answered(answer: tuple[str, str] | None) -> None:
            if answer is None:
                return
            status, note = answer
            with actions.note_file(note) as path:
                self._run(actions.resolve(job.id, status, path))

        self.push_screen(ResolveScreen(), answered)
