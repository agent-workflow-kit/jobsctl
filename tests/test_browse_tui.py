"""The browse TUI (interface.md, TUI), driven headlessly with Textual's pilot.

Assertions are on the text the widgets hold (table cells, the detail pane, the status line), not
on snapshots. Every key acts through a CLI command, so the effects are read back from the
database and the audit log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from typing import Any

import pytest

pytest.importorskip("textual")  # the unconditional tests are in test_browse_cli.py

from textual import events  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.pilot import Pilot  # noqa: E402

from jobsctl import gateway  # noqa: E402
from jobsctl.timestamps import format_timestamp  # noqa: E402
from jobsctl.tui import actions  # noqa: E402
from jobsctl.tui import app as tui_app
from jobsctl.tui.app import BrowseApp, BrowseResult  # noqa: E402
from tests.browse_helpers import (  # noqa: E402
    add_quiet,
    detail,
    drive,
    everything_shown,
    job_ids,
    job_rows,
    queue_rows,
    status,
)
from tests.manual_helpers import (  # noqa: E402
    add_job,
    attempt_row,
    audit_records,
    make_held,
    make_running,
    row,
    sql,
    table_dump,
)


def fresh(job_id: str) -> None:
    """Make a terminal job recent enough for the 24 hour window of the listing."""
    sql("UPDATE jobs SET ended_at = ? WHERE id = ?", format_timestamp(gateway.utc_now()), job_id)


async def press(pilot: Pilot[BrowseResult], *keys: str) -> None:
    for key in keys:
        await pilot.press(key)
        await pilot.pause()


@pytest.fixture
def three(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Three queued jobs in queue q1, in rank order."""
    ids = [add_job(capsys, "--queue", "q1") for _ in range(3)]
    capsys.readouterr()
    return ids


# -- views ---------------------------------------------------------------------------------


def test_shows_the_queues_and_the_jobs_of_the_first_queue_in_rank_order(
    three: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    other = add_job(capsys, "--queue", "q2")
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        seen["queues"] = queue_rows(app)
        seen["jobs"] = job_ids(app)
        seen["detail"] = detail(app)

    drive(script)
    assert [q[0] for q in seen["queues"]] == ["q1", "q2"]
    assert seen["queues"][0][3] == "3"  # queued
    assert seen["jobs"] == three and other not in seen["jobs"]
    assert seen["detail"].startswith(three[0])


def test_moving_to_another_queue_lists_its_jobs(
    three: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    other = add_job(capsys, "--queue", "q2")
    seen: list[list[str]] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "tab", "j", "tab")
        seen.append(job_ids(app))

    drive(script)
    assert seen == [[other]]


def test_the_detail_pane_shows_attempts_and_the_live_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_running(job_id)
    sql(
        "INSERT INTO outputs (job_id, n, tail, updated_at) VALUES (?, 1, ?, ?)",
        job_id,
        b"first line\n",
        "2026-01-01T00:00:02.000Z",
    )
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        seen.append(detail(app))
        sql("UPDATE outputs SET tail = ? WHERE job_id = ?", b"first line\nsecond line\n", job_id)
        await press(pilot, "r")
        seen.append(detail(app))

    drive(script)
    assert "attempt 1: running" in seen[0] and "first line" in seen[0]
    assert "second line" not in seen[0] and "second line" in seen[1]


def test_terminal_jobs_of_the_last_day_are_listed_older_ones_are_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recent, old = add_job(capsys), add_job(capsys)
    from tests.manual_helpers import make_done

    make_done(recent)
    make_done(old)
    fresh(recent)
    shown: list[list[str]] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        shown.extend(job_rows(app))

    drive(script)
    assert [r[1] for r in shown] == [recent] and shown[0][2].startswith("done")


def test_an_empty_database_and_a_missing_one_show_a_note_and_pick_up_jobs_later(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        seen.append(status(app))
        add_quiet()
        await press(pilot, "r")
        seen.append(status(app))
        seen.append(",".join(job_ids(app)))

    drive(script)
    assert "no state database" in seen[0]
    assert seen[1].startswith("global:") and seen[2].startswith("jb-")


def test_viewing_and_refreshing_writes_nothing(three: list[str]) -> None:
    before = table_dump()

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(
            pilot, "r", "j", "k", "tab", "tab", "slash", "escape", "question_mark", "escape"
        )

    drive(script)
    assert table_dump() == before


# -- hostile data --------------------------------------------------------------------------

HOSTILE = (
    "[bold red]X[/] [link=https://evil.example]L[/link] \x1b[31mred\x1b]0;title\x07 \x9b2J "
    "\u202eRTL\u2066 :smile:"
)


def test_stored_text_is_sanitized_and_never_read_as_markup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys, "--queue", "q1")
    make_running(job_id)
    sql("UPDATE jobs SET argv = ?, queue = ? WHERE id = ?", json.dumps([HOSTILE]), HOSTILE, job_id)
    sql(
        "UPDATE jobs SET reason = ?, note = ?, actor = ? WHERE id = ?",
        HOSTILE,
        HOSTILE,
        HOSTILE,
        job_id,
    )
    sql(
        "INSERT INTO outputs (job_id, n, tail, updated_at) VALUES (?, 1, ?, ?)",
        job_id,
        HOSTILE.encode() + b"\nline\rrewritten\n",
        "2026-01-01T00:00:02.000Z",
    )
    sql(
        "UPDATE attempts SET hook_messages = ?, note = ? WHERE job_id = ?",
        json.dumps([{"hook_id": HOSTILE, "outcome": "veto", "message": HOSTILE}]),
        HOSTILE,
        job_id,
    )
    sql("INSERT INTO job_labels (job_id, key, value) VALUES (?, ?, ?)", job_id, "k", HOSTILE)
    shown: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        shown.append(everything_shown(app))

    drive(script)
    text = shown[0]
    # markup-looking text is displayed literally, not interpreted ...
    assert "[bold red]X[/]" in text and "[link=https://evil.example]L[/link]" in text
    assert ":smile:" in text
    # ... and control characters, escape sequences and bidi controls are gone or escaped
    assert not any(ch in text for ch in "\x1b\x07\x9b\u202e\u2066")
    assert "\\u202e" in text and "\\u2066" in text
    assert "output:" in text  # the tail itself is shown
    assert "rewritten" in text and "line\rrewritten" not in text


def test_markup_text_is_not_styled_in_the_rendered_screen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    sql(
        "UPDATE jobs SET argv = ? WHERE id = ?",
        json.dumps(["[link=https://evil.example]L[/]"]),
        job_id,
    )
    svg: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        svg.append(app.export_screenshot())

    drive(script)
    assert "evil.example" not in svg[0].replace("[link=https://evil.example]", "")
    assert "[link=" in svg[0] or "link=" in svg[0]


# -- keys, each one a CLI command ----------------------------------------------------------


def test_J_and_K_move_a_job_after_and_before_its_neighbor(three: list[str]) -> None:
    a, b, c = three
    orders: list[list[str]] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "J")
        orders.append(job_ids(app))
        await press(pilot, "K")
        orders.append(job_ids(app))
        await press(pilot, "K")  # already at the top
        assert "already first" in status(app)

    drive(script)
    assert orders == [[b, a, c], [a, b, c]]


def test_t_and_b_move_to_the_top_and_the_bottom(three: list[str]) -> None:
    a, b, c = three
    orders: list[list[str]] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "b")
        orders.append(job_ids(app))
        await press(pilot, "t")
        orders.append(job_ids(app))

    drive(script)
    assert orders == [[b, c, a], [a, b, c]]


def test_p_pins_and_unpins_and_plus_minus_change_the_priority(three: list[str]) -> None:
    a = three[0]

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "p")
        assert row(a)["pinned"] == 1 and job_rows(app)[0][4] == "^0"
        await press(pilot, "p")
        assert row(a)["pinned"] == 0
        await press(pilot, "plus", "plus")
        assert row(a)["priority"] == 2
        await press(pilot, "minus")
        assert row(a)["priority"] == 1 and job_rows(app)[0][4] == "1"

    drive(script)


def test_h_holds_and_e_releases_a_manual_hold(three: list[str]) -> None:
    a = three[0]

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "h")
        assert row(a)["status"] == "held"
        assert [r[2] for r in job_rows(app) if r[1] == a] == ["held(manual)"]
        await press(pilot, "e")
        assert row(a)["status"] == "queued"

    drive(script)


def test_releasing_an_escalated_hold_asks_and_overrides_only_after_yes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated", ("approve",))

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "e")
        assert type(app.screen).__name__ == "ConfirmScreen"
        await press(pilot, "n")
        assert row(job_id)["status"] == "held" and "not confirmed" in status(app)
        assert audit_records() == []
        await press(pilot, "e", "y")
        assert row(job_id)["status"] == "queued"
        assert json.loads(row(job_id)["override_hooks"]) == ["approve"]

    drive(script)
    (record,) = [r for r in audit_records() if r["action"] == "release_override"]
    assert record["actor"] == "human:kim" and record["target"] == job_id


def test_an_exit_13_hold_release_shows_the_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "exit_13")

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "e")
        assert "not_releasable" in status(app)
        assert row(job_id)["status"] == "held"

    drive(script)


def test_v_resolves_a_held_job_with_status_and_note(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "manual")

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "v")
        assert type(app.screen).__name__ == "ResolveScreen"
        await pilot.press(*"failed")
        await press(pilot, "enter")
        await pilot.press(*"checked by hand")
        await press(pilot, "enter")
        assert row(job_id)["status"] == "failed" and row(job_id)["note"] == "checked by hand"
        assert row(job_id)["resolved_by"] == "human:kim"

    drive(script)


def test_resolve_rejects_an_unknown_status_and_escape_cancels(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "manual")

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "v")
        await pilot.press("backspace", "backspace", "backspace", "backspace", *"nope")
        await press(pilot, "enter")
        assert type(app.screen).__name__ == "ResolveScreen"
        await press(pilot, "escape")
        assert type(app.screen).__name__ != "ResolveScreen"
        assert row(job_id)["status"] == "held"

    drive(script)


def test_c_cancels_a_queued_job_without_asking(three: list[str]) -> None:
    a = three[0]

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "c")
        assert row(a)["status"] == "canceled"

    drive(script)


def test_c_on_a_running_job_asks_then_force_cancels_and_shows_canceling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_running(job_id)

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "c")
        assert type(app.screen).__name__ == "ConfirmScreen"
        await press(pilot, "escape")
        assert attempt_row(job_id)["force_requested_by"] is None
        await press(pilot, "c", "y")
        assert attempt_row(job_id)["force_requested_by"] == "human:kim"
        assert "canceling" in job_rows(app)[0][2]

    drive(script)
    assert any(r["action"] == "cancel_force" and r["actor"] == "human:kim" for r in audit_records())


def test_R_retries_a_failed_job(capsys: pytest.CaptureFixture[str]) -> None:
    from tests.manual_helpers import make_done

    job_id = add_job(capsys)
    make_done(job_id, "failed")
    fresh(job_id)

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "R")
        assert row(job_id)["status"] == "queued"

    drive(script)


def test_a_refusal_is_shown_in_the_status_line(three: list[str]) -> None:
    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "R")  # a queued job cannot be retried
        assert "retry: refused(not_terminal)" in status(app)

    drive(script)


def test_slash_filters_the_jobs_and_an_empty_filter_clears_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = add_job(capsys, "--label", "team=red")
    b = add_job(capsys, "--label", "team=blue")
    seen: list[list[str]] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "slash")
        await pilot.press(*"TEAM=blue")
        await press(pilot, "enter")
        seen.append(job_ids(app))
        assert "filter: TEAM=blue" in status(app)
        await press(pilot, "slash", "ctrl+u", "enter")
        seen.append(job_ids(app))
        await press(pilot, "slash", "escape")  # j,k etc. are typed text only while it is open
        seen.append(job_ids(app))

    drive(script)
    assert seen == [[b], [a, b], [a, b]]


def test_space_marks_and_o_opens_the_marked_jobs_into_the_result(three: list[str]) -> None:
    a, b, c = three

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "space", "j", "j", "space", "o")

    result = drive(script)
    assert result == BrowseResult(intent="open", selected=(a, c))


def test_o_without_marks_opens_the_current_job_and_q_goes_back(three: list[str]) -> None:
    async def opened(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "j", "o")

    async def back(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "space", "q")

    assert drive(opened) == BrowseResult(intent="open", selected=(three[1],))
    assert drive(back) == BrowseResult(intent="back", selected=())


def test_help_and_enter_open_and_close(three: list[str]) -> None:
    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "question_mark")
        assert type(app.screen).__name__ == "HelpScreen"
        await press(pilot, "escape")
        assert type(app.screen).__name__ != "HelpScreen"
        await press(pilot, "enter")
        assert app.focused is not None and app.focused.id == "detail-scroll"

    drive(script)


def test_focus_opens_on_that_job_in_its_queue(
    three: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    target = add_job(capsys, "--queue", "q2")
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        seen.append(detail(app).splitlines()[0])
        seen.append(job_ids(app)[0])

    drive(script, focus=target)
    assert seen[1] == target and seen[0].startswith(target)


def test_focus_on_an_unknown_job_is_said_not_fatal(three: list[str]) -> None:
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        seen.append(status(app))
        assert job_ids(app) == three

    drive(script, focus="jb-zzzzzzzz")
    assert seen[0] == "job jb-zzzzzzzz not found"


# -- ending --------------------------------------------------------------------------------


def test_a_newer_schema_underneath_ends_the_app_with_version_skew(three: list[str]) -> None:
    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        sql("PRAGMA user_version = 9999")
        await press(pilot, "r")

    result = drive(script)
    assert result is not None and result.error is not None
    assert result.error.code == "version_skew" and result.error.exit_code == 12


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_interrupt_signals_end_the_app_through_its_normal_exit(
    three: list[str], number: signal.Signals
) -> None:
    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        signal.raise_signal(number)
        await pilot.pause(0.2)

    result = drive(script)
    assert result == BrowseResult(interrupted=True)


def test_an_unexpected_exception_ends_with_an_internal_error_and_no_traceback(
    three: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise ZeroDivisionError("secret detail")

    monkeypatch.setattr(actions, "run_command", boom)

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        with contextlib.suppress(Exception):  # the app ends while the key is processed
            await pilot.press("h")
        await asyncio.sleep(0.3)

    result = drive(script)
    assert result is not None and result.error is not None
    assert result.error.code == "internal" and "ZeroDivisionError" in result.error.message
    assert "secret detail" not in result.error.message
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err


def test_a_refresh_failure_is_a_status_message_not_a_crash(
    three: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl.errors import JobsctlError
    from jobsctl.tui import model

    def busy(*args: object, **kwargs: object) -> object:
        raise JobsctlError("busy", "state.db is busy after 5s; try again")

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        monkeypatch.setattr(model, "load", busy)
        await press(pilot, "r")
        assert "busy" in status(app)
        assert app.is_running
        await press(pilot, "q")

    drive(script)


def test_the_timer_refreshes_without_a_key(capsys: pytest.CaptureFixture[str]) -> None:
    first = add_job(capsys)
    seen: list[list[str]] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        second = add_quiet()
        for _ in range(100):  # bounded: 10 s at most
            await pilot.pause(0.1)
            if second in job_ids(app):
                break
        seen.append(job_ids(app))
        assert second in seen[0]

    drive(script, refresh=0.2)
    assert seen[0][0] == first


def test_the_module_binds_every_key_of_the_spec() -> None:
    keys = {b.key for b in tui_app.BrowseApp.BINDINGS if isinstance(b, Binding)}
    wanted = {"j", "k", "K", "J", "t", "b", "p", "plus", "minus", "h", "e", "v", "c", "R", "r"}
    assert wanted <= keys and {"slash", "question_mark", "q"} <= keys


def test_ctrl_q_goes_back_like_q_and_the_command_palette_is_off(three: list[str]) -> None:
    assert tui_app.BrowseApp.ENABLE_COMMAND_PALETTE is False

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "ctrl+p")
        assert type(app.screen).__name__ == "Screen"  # no palette screen
        await press(pilot, "ctrl+q")

    assert drive(script) == BrowseResult(intent="back", selected=())


def test_enter_does_not_confirm_only_y_does(capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "escalated", ("approve",))

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "e", "enter")
        assert type(app.screen).__name__ == "ConfirmScreen"
        assert row(job_id)["status"] == "held"
        await press(pilot, "y")
        assert row(job_id)["status"] == "queued"

    drive(script)


def test_a_fast_key_sequence_acts_on_the_job_under_the_cursor(three: list[str]) -> None:
    a, b, _c = three

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        # both keys are queued before the highlight event of the first is handled
        app.post_message(events.Key("j", "j"))
        app.post_message(events.Key("h", "h"))
        await pilot.pause()
        await pilot.pause()

    drive(script)
    assert row(b)["status"] == "held" and row(a)["status"] == "queued"


def test_tab_inside_a_dialog_moves_between_its_inputs_not_the_lists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(capsys)
    make_held(job_id, "manual")

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "v")
        assert app.focused is not None and app.focused.id == "as"
        await press(pilot, "tab")
        assert type(app.screen).__name__ == "ResolveScreen"
        assert app.focused is not None and app.focused.id == "note"

    drive(script)


def test_arrow_keys_tab_and_enter_navigate(three: list[str]) -> None:
    a, b, _c = three

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "down")
        assert detail(app).startswith(b)
        await press(pilot, "up")
        assert detail(app).startswith(a)
        await press(pilot, "tab")
        assert app.focused is not None and app.focused.id == "queues"
        await press(pilot, "tab")
        assert app.focused is not None and app.focused.id == "jobs"
        await press(pilot, "enter")
        assert app.focused is not None and app.focused.id == "detail-scroll"

    drive(script)


def test_marks_of_jobs_that_no_longer_exist_are_dropped_but_filtered_ones_stay(
    three: list[str],
) -> None:
    a, b, c = three

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        await press(pilot, "space", "j", "space", "j", "space")  # a, b, c marked
        await press(pilot, "slash")
        await pilot.press(*"zzz")  # matches nothing: every job is hidden by the filter
        await press(pilot, "enter")
        assert job_ids(app) == []
        sql("DELETE FROM jobs WHERE id = ?", b)
        await press(pilot, "r")
        await press(pilot, "o")

    result = drive(script)
    assert result == BrowseResult(intent="open", selected=tuple(sorted((a, c))))


@pytest.mark.parametrize(
    ("case", "keys", "expected"),
    [
        ("queued", ["J"], lambda a, b, c: ["move", a, "--after", b]),
        ("queued", ["j", "K"], lambda a, b, c: ["move", b, "--before", a]),
        ("queued", ["t"], lambda a, b, c: ["move", a, "--top"]),
        ("queued", ["b"], lambda a, b, c: ["move", a, "--bottom"]),
        ("queued", ["p"], lambda a, b, c: ["pin", a]),
        ("pinned", ["p"], lambda a, b, c: ["unpin", a]),
        ("queued", ["plus"], lambda a, b, c: ["priority", a, "1"]),
        ("queued", ["minus"], lambda a, b, c: ["priority", a, "-1"]),
        ("queued", ["h"], lambda a, b, c: ["hold", a]),
        ("held", ["e"], lambda a, b, c: ["release", a]),
        ("escalated", ["e", "y"], lambda a, b, c: ["release", a, "--override-escalation"]),
        ("queued", ["c"], lambda a, b, c: ["cancel", a]),
        (
            "running",
            ["c", "y"],
            lambda a, b, c: ["cancel", a, "--force", "--wait-timeout", "0"],
        ),
        ("done", ["R"], lambda a, b, c: ["retry", a]),
        ("held", ["v", "enter", "enter"], lambda a, b, c: ["resolve", a, "--as", "done"]),
    ],
)
def test_each_key_runs_exactly_this_cli_command_with_the_human_actor(
    case: str,
    keys: list[str],
    expected: Any,
    three: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.manual_helpers import make_done

    # a job whose state changed sorts after the queued ones: give all three the state, so the
    # job under the cursor is the first one listed
    for job_id in three:
        if case == "pinned":
            sql("UPDATE jobs SET pinned = 1 WHERE id = ?", job_id)
        elif case == "held":
            make_held(job_id, "manual")
        elif case == "escalated":
            make_held(job_id, "escalated", ("approve",))
        elif case == "running":
            make_running(job_id)
        elif case == "done":
            make_done(job_id, "failed")
            fresh(job_id)
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(actions.cli, "main", fake_main)

    listed: list[str] = []

    async def script(app: BrowseApp, pilot: Pilot[BrowseResult]) -> None:
        listed.extend(job_ids(app))
        await press(pilot, *keys)

    drive(script)
    assert calls == [["--json", *expected(*listed), "--actor", "human:kim"]]
