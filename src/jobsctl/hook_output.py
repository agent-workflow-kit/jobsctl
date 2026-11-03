"""A hook's captured output: bounded capture, masked excerpts and decision ids.

Capture keeps the first 64 KiB of each stream in memory and counts the rest; hook output is never
stored whole (interface.md, Hooks). Text that leaves this module (the `stderr` of a `hook_ran`
record, `hook_messages`, warnings) is decoded, stripped of terminal escapes, masked and escaped
in the order of Masking and sanitizing, and cut to its byte limit **after** masking.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from .cli_values import DECISION_ID_RE
from .textsafe import sanitize

__all__ = [
    "CAPTURE_LIMIT",
    "LOG_STDERR_LIMIT",
    "MAX_DECISION_IDS",
    "MESSAGE_LIMIT",
    "Capture",
    "excerpt",
    "parse_decision_ids",
]

CAPTURE_LIMIT = 64 * 1024
LOG_STDERR_LIMIT = 2048
MESSAGE_LIMIT = 4096
MAX_DECISION_IDS = 32

Mask = Callable[[str], str]


@dataclass
class Capture:
    """The first `limit` bytes of a stream; `seen` counts every byte read."""

    limit: int = CAPTURE_LIMIT
    seen: int = 0

    def __post_init__(self) -> None:
        self._buf = bytearray()

    def add(self, chunk: bytes) -> None:
        self.seen += len(chunk)
        room = self.limit - len(self._buf)
        if room > 0:
            self._buf += chunk[:room]

    @property
    def data(self) -> bytes:
        return bytes(self._buf)

    @property
    def truncated(self) -> bool:
        return self.seen > len(self._buf)


def _cut_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def excerpt(capture: Capture, limit: int, mask: Mask) -> str:
    """The captured text as an untrusted, masked and sanitized excerpt of at most `limit` bytes.

    A capture that dropped bytes ends in a cut, which could hold the start of a masked value, so
    the last partial line is discarded before masking (all of it when there is no newline).
    """
    raw = capture.data
    if capture.truncated:
        raw = raw[: raw.rfind(b"\n") + 1]
    text = raw.decode("utf-8", errors="backslashreplace")
    return _cut_bytes(sanitize(text, mask=mask), limit)


_MAX_DEPTH = 32
_NESTED = "<nested value>"


def _masked(value: object, mask: Mask, depth: int = 0) -> object:
    """``value`` with every string, and every key, masked as decoded text (so a JSON escape
    cannot hide a masked value). Containers nested deeper than ``_MAX_DEPTH`` are replaced by a
    fixed text, so hostile nesting can neither recurse without bound nor leak unmasked."""
    if isinstance(value, str):
        return mask(value[:CAPTURE_LIMIT])
    if isinstance(value, (list, dict)) and depth >= _MAX_DEPTH:
        return _NESTED
    if isinstance(value, list):
        return [_masked(item, mask, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            mask(str(key)[:CAPTURE_LIMIT]): _masked(item, mask, depth + 1)
            for key, item in value.items()
        }
    return value


def _clean_for_warning(value: object, mask: Mask) -> str:
    """A short echo of an invalid value: masked in full first, then cut to 64 bytes."""
    if isinstance(value, str):
        text = mask(value[:CAPTURE_LIMIT])
    else:
        try:
            text = json.dumps(_masked(value, mask), ensure_ascii=True)
        except RecursionError:
            text = _NESTED
        text = mask(text[:CAPTURE_LIMIT])
    return _cut_bytes(sanitize(text, inline=True), 64)


def parse_decision_ids(capture: Capture, mask: Mask) -> tuple[list[str], list[str]]:
    """`(decision ids, invalid_decision_id details)` from a hook's JSON stdout.

    Stdout must be one JSON object; anything else (plain text, a list, truncated or malformed
    JSON, invalid UTF-8) yields no ids and no warning, since a hook may print what it likes.
    `decision_id` is a string or a list of strings; every value must match the decision-id
    grammar or is dropped with a detail (a sanitized, masked, shortened echo, never the raw
    value). Duplicates are kept once, in order; at most `MAX_DECISION_IDS` are kept.
    """
    if capture.truncated or not capture.data.strip():
        return [], []
    try:
        document = json.loads(capture.data.decode("utf-8"))
    except (ValueError, RecursionError):
        return [], []
    if not isinstance(document, dict) or "decision_id" not in document:
        return [], []
    raw = document["decision_id"]
    candidates = raw if isinstance(raw, list) else [raw]
    ids: list[str] = []
    invalid: list[str] = []
    for value in candidates:
        if not isinstance(value, str) or DECISION_ID_RE.match(value) is None:
            invalid.append(_clean_for_warning(value, mask))
        elif value in ids:
            continue
        elif len(ids) >= MAX_DECISION_IDS:
            invalid.append("too many decision ids")
        else:
            ids.append(value)
    return ids, invalid
