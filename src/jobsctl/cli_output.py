"""Output helpers: the error envelope (stdout with --json, stderr otherwise) and JSON lines."""

from __future__ import annotations

import json
import sys
from typing import Any

from jobsctl.errors import JobsctlError
from jobsctl.textsafe import escape_surrogates, sanitize


def _without_lone_surrogates(value: Any) -> Any:
    """`value` with every string (and key) free of lone surrogates, as `\\xNN` text."""
    if isinstance(value, str):
        return escape_surrogates(value)
    if isinstance(value, dict):
        return {_without_lone_surrogates(k): _without_lone_surrogates(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_without_lone_surrogates(v) for v in value]
    return value


def print_json(document: Any) -> None:
    """One JSON document on stdout, ASCII-only so no byte sequence can break the stream.

    An undecodable byte in a string becomes the text `\\xNN`, so a consumer that decodes the JSON
    gets strings that encode to UTF-8.
    """
    sys.stdout.write(json.dumps(_without_lone_surrogates(document)) + "\n")
    sys.stdout.flush()


def print_error(err: JobsctlError, *, as_json: bool) -> None:
    if as_json:
        print_json(err.envelope())
        return
    lines = [f"jobsctl: error: {sanitize(err.message, inline=True)}"]
    if err.candidates:
        lines.append("candidates: " + ", ".join(sanitize(c, inline=True) for c in err.candidates))
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
