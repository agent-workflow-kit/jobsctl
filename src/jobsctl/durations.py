"""The duration grammar (interface.md, Durations): `<integer><unit>` in whole seconds.

Pure parsing only. `none` and the bare `0` of `cancel --wait-timeout` are per-flag exceptions
that belong to the callers (`cli_values` adapters, and config models for their own keys).
"""

from __future__ import annotations

import re
from typing import Final

_DURATION_RE: Final = re.compile(r"([1-9][0-9]*)([smhd])")
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_DURATION_SECONDS: Final = 100 * 365 * 86400
_MAX_DURATION_DIGITS: Final = 12  # more digits cannot be within the cap; never int() them


class DurationError(ValueError):
    """The text is not a valid duration; the message never includes the text."""


def looks_like_duration(text: str) -> bool:
    """Whether `text` is syntactically `<integer><unit>` (the cap is not checked)."""
    return _DURATION_RE.fullmatch(text) is not None


def parse_duration(text: str) -> int:
    """`<integer><unit>` with unit s, m, h or d, in seconds: at least 1, no leading zeros and
    at most 100 years."""
    match = _DURATION_RE.fullmatch(text)
    if match is None:
        raise DurationError(
            "a duration is <integer><unit> with unit s, m, h or d, the integer at least 1 "
            "without leading zeros (e.g. 30s, 10m)"
        )
    if len(match.group(1)) > _MAX_DURATION_DIGITS:
        raise DurationError("a duration is at most 100 years")
    seconds = int(match.group(1)) * _UNITS[match.group(2)]
    if seconds > MAX_DURATION_SECONDS:
        raise DurationError("a duration is at most 100 years")
    return seconds
