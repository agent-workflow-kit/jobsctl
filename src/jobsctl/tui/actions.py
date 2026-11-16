"""The TUI's actions: each one is a CLI command run through the CLI entry point.

`run_command` calls `jobsctl.cli.main`, the code path of the shell, so the usage checks, the
transitions, the audit records, the human gate and the refusals are the ones of the command and
nothing is duplicated here. The command's output is captured (nothing is printed under the TUI)
and read back as `--json`. This module builds the argument lists and reads the outcome; it knows
nothing about Textual.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobsctl import cli
from jobsctl.actors import default_actor
from jobsctl.job_text import clean
from jobsctl.textsafe import sanitize


@dataclass(frozen=True)
class Outcome:
    code: int
    ok: bool
    message: str  # sanitized, one line
    error_code: str | None = None


def human_actor(me_name: str) -> str:
    """`human:<me.name>`, as a command run from a terminal would record it."""
    return default_actor(me_name=me_name, stdin_isatty=True)


def move_before(job_id: str, other: str) -> list[str]:
    return ["move", job_id, "--before", other]


def move_after(job_id: str, other: str) -> list[str]:
    return ["move", job_id, "--after", other]


def move_top(job_id: str) -> list[str]:
    return ["move", job_id, "--top"]


def move_bottom(job_id: str) -> list[str]:
    return ["move", job_id, "--bottom"]


def pin(job_id: str, *, pinned: bool) -> list[str]:
    return ["unpin" if pinned else "pin", job_id]


def priority(job_id: str, value: int) -> list[str]:
    return ["priority", job_id, str(value)]


def hold(job_id: str) -> list[str]:
    return ["hold", job_id]


def release(job_id: str, *, override: bool) -> list[str]:
    return ["release", job_id, *(["--override-escalation"] if override else [])]


def resolve(job_id: str, as_: str, note_file: str | None) -> list[str]:
    argv = ["resolve", job_id, "--as", as_]
    if note_file is not None:
        argv += ["--note-file", note_file]
    return argv


def cancel(job_id: str, *, force: bool) -> list[str]:
    return ["cancel", job_id, *(["--force", "--wait-timeout", "0"] if force else [])]


def retry(job_id: str) -> list[str]:
    return ["retry", job_id]


@contextlib.contextmanager
def note_file(note: str) -> Iterator[str | None]:
    """The path of a private file holding `note` (`None` for an empty note), removed after.

    `resolve` reads its note from a file; standard input belongs to the terminal.
    """
    if not note:
        yield None
        return
    directory = Path(tempfile.mkdtemp(prefix="jobsctl-note-"))
    try:
        path = directory / "note"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(note.encode("utf-8", "surrogateescape"))
        yield str(path)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _document(text: str) -> dict[str, Any] | None:
    for line in reversed(text.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        return value if isinstance(value, dict) else None
    return None


def _summary(argv: list[str], document: dict[str, Any] | None) -> str:
    action = argv[0]
    if document is None:
        return f"{action}: done"
    refused = document.get("refused")
    if refused:
        reasons = ", ".join(clean(r.get("reason", "?")) for r in refused if isinstance(r, dict))
        return f"{action}: refused ({reasons})"
    if document.get("changed") is False:
        return f"{action}: no change"
    return f"{action}: done"


def run_command(argv: list[str], *, config_path: Path | None, actor: str) -> Outcome:
    """Run `jobsctl [--config PATH] --json <argv> --actor <actor>` in this process and describe
    the result. The actor is always the human at the terminal (`human:<me.name>`); for
    `cancel --force` and `release --override-escalation` it is also what satisfies the human
    gate, after the TUI has asked for confirmation."""
    full = [
        "--json",
        *(["--config", str(config_path)] if config_path is not None else []),
        *argv,
        "--actor",
        actor,
    ]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(full)
    document = _document(out.getvalue())
    error = document.get("error") if document is not None else None
    if isinstance(error, dict):
        error_code = clean(error.get("code", "internal"))
        shown = error_code + (f"({clean(error['reason'])})" if error.get("reason") else "")
        message = sanitize(str(error.get("message", "")), inline=True)
        return Outcome(code, False, f"{argv[0]}: {shown}: {message}", error_code)
    return Outcome(code, code == 0, _summary(argv, document))
