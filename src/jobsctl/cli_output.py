"""Output helpers: the error envelope (stdout with --json, stderr otherwise) and JSON lines."""

from __future__ import annotations

import json
import sys
from typing import Any

from jobsctl.errors import JobsctlError
from jobsctl.textsafe import sanitize


def print_json(document: Any) -> None:
    """One JSON document on stdout, ASCII-only so no byte sequence can break the stream."""
    sys.stdout.write(json.dumps(document) + "\n")
    sys.stdout.flush()


def print_error(err: JobsctlError, *, as_json: bool) -> None:
    if as_json:
        print_json(err.envelope())
        return
    lines = [f"jobsctl: error: {sanitize(err.message)}"]
    if err.candidates:
        lines.append("candidates: " + ", ".join(sanitize(c) for c in err.candidates))
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
