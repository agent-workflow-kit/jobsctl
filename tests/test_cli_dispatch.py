"""Dispatch: registry, Context, error mapping, human gate wiring and the real entry point."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl import cli_registry, commands, gateway
from jobsctl.checks import run_checks
from jobsctl.cli_registry import Context, command
from jobsctl.errors import JobsctlError, refused
from tests.conftest import validate_against
from tests.helpers import run_cli

JID = "jb-abcd2345"


def _isolated_table(monkeypatch: pytest.MonkeyPatch) -> dict[str, cli_registry.Handler]:
    """Import every real command module now, then swap in an empty table for the test.

    The modules stay cached in `sys.modules`, so the `load_all` inside the dispatcher imports
    nothing again: a real command module never re-registers (no duplicate error) and never
    registers into the throwaway table, whatever sibling issues add under `commands/`.
    """
    commands.load_all()
    table: dict[str, cli_registry.Handler] = {}
    monkeypatch.setattr(cli_registry, "_HANDLERS", table)
    return table


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, cli_registry.Handler]]:
    yield _isolated_table(monkeypatch)


def test_handler_gets_context_and_its_return_value_is_the_exit_code(
    handlers: dict[str, cli_registry.Handler],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Context] = []

    @command("hold")
    def hold(ctx: Context) -> int:
        seen.append(ctx)
        return 0

    monkeypatch.setattr(cli_registry, "os_user_name", lambda: "kim")
    assert run_cli(capsys, "--config", "rel/c.yaml", "hold", JID, "--json").code == 0
    ctx = seen[0]
    assert ctx.command == "hold"
    assert ctx.json is True
    assert ctx.config_path == Path.cwd() / "rel/c.yaml"
    assert ctx.args.id == JID
    assert ctx.explicit_actor is None
    assert ctx.actor == "auto:unknown"  # stdin is not a TTY under pytest


def test_duplicate_registration_is_an_error(handlers: dict[str, cli_registry.Handler]) -> None:
    command("hold")(lambda ctx: 0)
    with pytest.raises(ValueError):
        command("hold")(lambda ctx: 0)


def test_handler_error_becomes_envelope(
    handlers: dict[str, cli_registry.Handler], capsys: pytest.CaptureFixture[str]
) -> None:
    @command("hold")
    def hold(ctx: Context) -> int:
        raise refused("live", "job is running", details={"job_id": JID})

    res = run_cli(capsys, "--json", "hold", JID)
    assert res.code == 20
    env = res.envelope()
    validate_against("cli/error.json", env)
    assert env["error"]["reason"] == "live"


def test_unexpected_exception_is_internal_and_does_not_leak(
    handlers: dict[str, cli_registry.Handler], capsys: pytest.CaptureFixture[str]
) -> None:
    @command("hold")
    def hold(ctx: Context) -> int:
        raise RuntimeError("secret detail /home/x")

    res = run_cli(capsys, "--json", "hold", JID)
    assert res.code == 1
    assert "secret detail" not in res.out
    validate_against("cli/error.json", res.envelope())


def test_verbose_prints_traceback_on_internal_error(
    handlers: dict[str, cli_registry.Handler], capsys: pytest.CaptureFixture[str]
) -> None:
    @command("hold")
    def hold(ctx: Context) -> int:
        raise RuntimeError("boom")

    res = run_cli(capsys, "hold", JID, "--verbose")
    assert res.code == 1
    assert "RuntimeError" in res.err


def test_keyboard_interrupt_is_130(
    handlers: dict[str, cli_registry.Handler], capsys: pytest.CaptureFixture[str]
) -> None:
    @command("hold")
    def hold(ctx: Context) -> int:
        raise KeyboardInterrupt

    res = run_cli(capsys, "--json", "hold", JID)
    assert res.code == 130
    validate_against("cli/error.json", res.envelope())


def _release_like(ctx: Context) -> int:
    """A handler written the way later commands must: usage, lookup, state, then the gate."""

    def lookup() -> None:
        if ctx.args.id == "jb-zzzz":
            raise JobsctlError("not_found", "no such job")

    def state() -> None:
        if ctx.args.id == "jb-bbbb":
            raise refused("not_escalated", "not an escalated hold")

    run_checks(
        lookup=lookup,
        state=state,
        human=lambda: (
            ctx.require_human("release --override-escalation")
            if ctx.args.override_escalation
            else None
        ),
    )
    return 0


def test_gate_is_last_and_yes_never_satisfies_it(
    handlers: dict[str, cli_registry.Handler], capsys: pytest.CaptureFixture[str]
) -> None:
    command("release")(_release_like)
    flags = ["--override-escalation"]
    assert run_cli(capsys, "release", "jb-zzzz", *flags).code == 10  # not found beats gate
    assert run_cli(capsys, "release", "jb-bbbb", *flags).code == 20  # refusal beats gate
    assert run_cli(capsys, "release", "jb-cccc", *flags).code == 13
    assert run_cli(capsys, "release", "jb-cccc", *flags, "--yes").code == 13
    assert run_cli(capsys, "release", "jb-cccc", *flags, "--yes", "--actor", "auto:x").code == 13
    assert run_cli(capsys, "release", "jb-cccc", *flags, "--actor", "human:kim").code == 0
    assert run_cli(capsys, "release", "jb-cccc").code == 0  # flag not in effect: no gate


def test_gate_passes_on_a_tty(
    handlers: dict[str, cli_registry.Handler],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command("release")(_release_like)
    monkeypatch.setattr("jobsctl.cli._stdin_isatty", lambda: True)
    assert run_cli(capsys, "release", "jb-cccc", "--override-escalation").code == 0


@pytest.fixture
def real_hold_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A command module another issue could add under `commands/`, registering `hold`."""
    package_dir = tmp_path / "fakecmds"
    package_dir.mkdir()
    (package_dir / "fake_hold.py").write_text(
        "from jobsctl.cli_registry import command\n\n"
        "@command('hold')\ndef hold(ctx):\n    return 7\n"
    )
    real_table = cli_registry._HANDLERS
    monkeypatch.setattr(commands, "__path__", [*commands.__path__, str(package_dir)])
    yield "fake_hold"
    real_table.pop("hold", None)
    sys.modules.pop("jobsctl.commands.fake_hold", None)


def test_load_all_registers_command_modules_once(real_hold_module: str) -> None:
    assert "hold" not in cli_registry._HANDLERS
    commands.load_all()
    assert cli_registry.lookup("hold") is not None
    before = dict(cli_registry._HANDLERS)
    commands.load_all()  # idempotent: no duplicate-registration error, nothing changes
    assert cli_registry._HANDLERS == before


def test_fixture_survives_a_real_module_registering_the_same_command(
    real_hold_module: str,
    handlers: dict[str, cli_registry.Handler],
    capsys: pytest.CaptureFixture[str],
) -> None:
    command("hold")(lambda ctx: 0)  # the test's own handler under the real module's name
    assert run_cli(capsys, "hold", JID).code == 0
    assert cli_registry.lookup("hold") is not None


def test_real_entry_point_version_and_usage_error(tmp_path: Path) -> None:
    ok = gateway.run([sys.executable, "-I", "-m", "jobsctl", "--version"], timeout=60)
    assert ok.returncode == 0
    assert ok.stdout.startswith("jobsctl ")
    bad = gateway.run([sys.executable, "-I", "-m", "jobsctl", "--json", "add"], timeout=60)
    assert bad.returncode == 2
    validate_against("cli/error.json", json.loads(bad.stdout))
    assert bad.stderr == ""
