"""Timestamps (interface.md, Timestamps): RFC 3339 in UTC with `Z` and millisecond precision.

Everything here is pure: the clock is always an argument, never read, so callers inject it
(clock access belongs to `gateway.py`).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Final

_TIMESTAMP_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)


class TimestampError(ValueError):
    """The text is not a valid timestamp, or it is out of range; the message never echoes it."""


def parse_timestamp(text: str) -> dt.datetime:
    """An RFC 3339 timestamp with any offset, as an aware UTC datetime.

    The fraction is truncated to microseconds. A leap second (`:60`) is refused.
    """
    match = _TIMESTAMP_RE.fullmatch(text)
    if match is None:
        raise TimestampError("not an RFC 3339 timestamp (e.g. 2026-10-31T12:00:00.000Z)")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int(((match.group(7) or "") + "000000")[:6])
    zone = match.group(8)
    if zone in ("Z", "z"):
        tz = dt.UTC
    else:
        off_h, off_m = int(zone[1:3]), int(zone[4:6])
        if off_h > 23 or off_m > 59:
            raise TimestampError("timestamp offset out of range")
        delta = dt.timedelta(hours=off_h, minutes=off_m)
        tz = dt.timezone(-delta if zone[0] == "-" else delta)
    try:
        return dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=tz).astimezone(
            dt.UTC
        )
    except OverflowError:
        raise TimestampError("timestamp out of range") from None
    except ValueError:
        raise TimestampError("timestamp has a field out of range") from None


def format_timestamp(when: dt.datetime) -> str:
    """RFC 3339 in UTC with `Z` and millisecond precision (sub-millisecond digits truncated)."""
    if when.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    try:
        utc = when.astimezone(dt.UTC)
    except OverflowError:
        raise TimestampError("timestamp out of range") from None
    # Not strftime: `%Y` is not zero-padded for years below 1000 on glibc.
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond // 1000:03d}Z"
    )


def since_cutoff(seconds_back: int, now: dt.datetime) -> dt.datetime:
    """The instant `seconds_back` before `now` (`export --since DURATION`).

    Raises `TimestampError` (a `ValueError`, not a `JobsctlError`) when the result is before
    year 1; callers map it to a `usage` error.
    """
    # TODO(#6): map TimestampError from since_cutoff to a usage error in export --since
    if now.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    try:
        return now.astimezone(dt.UTC) - dt.timedelta(seconds=seconds_back)
    except OverflowError:
        raise TimestampError("timestamp out of range") from None
