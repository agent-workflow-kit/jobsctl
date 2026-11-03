"""Flag value grammars for the argparse surface, as `type=` callables and their parsers.

TODO(#4): the grammar, duration, timestamp and job-id rules here are the CLI's minimal copy;
replace the parsing functions with the shared grammar module (keep the `type=` adapters below,
which turn a failure into an argparse error and so into exit 2 `usage`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import posixpath
import re
from typing import Final

from jobsctl.actors import parse_actor
from jobsctl.errors import JobsctlError
from jobsctl.textsafe import echo

QUEUE_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
LABEL_KEY_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
LABEL_VALUE_RE: Final = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}\Z")
UNIQUE_KEY_RE: Final = re.compile(r"[A-Za-z0-9._:/#@+=,-]{1,256}\Z")
ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
DECISION_ID_RE: Final = re.compile(r"[A-Za-z0-9._:@/-]{1,128}\Z")
JOB_ID_PREFIX_RE: Final = re.compile(r"[a-z2-7]{4,8}\Z")
_DURATION_RE: Final = re.compile(r"([0-9]+)([smhd])\Z")
_INT_RE: Final = re.compile(r"-?[0-9]{1,18}\Z")
_TIMESTAMP_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?([Zz]|[+-][0-9]{2}:[0-9]{2})\Z"
)
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_MAX_DURATION_SECONDS: Final = 100 * 365 * 86400
ENV_VALUE_MAX_BYTES: Final = 128 * 1024


class DurationError(ValueError):
    pass


def parse_duration(text: str, *, allow_zero: bool = False) -> int:
    """`<integer><unit>` with unit s, m, h or d, in seconds; zero only with `allow_zero`."""
    match = _DURATION_RE.match(text)
    if match is None:
        raise DurationError("a duration is <integer><unit> with unit s, m, h or d (e.g. 30s, 10m)")
    seconds = int(match.group(1)) * _UNITS[match.group(2)]
    if seconds == 0 and not allow_zero:
        raise DurationError("a duration must not be zero")
    if seconds > _MAX_DURATION_SECONDS:
        raise DurationError("a duration is at most 100 years")
    return seconds


def parse_timestamp(text: str) -> dt.datetime:
    """An RFC 3339 timestamp with any offset, as an aware UTC datetime."""
    match = _TIMESTAMP_RE.match(text)
    if match is None:
        raise ValueError("not an RFC 3339 timestamp (e.g. 2026-10-31T12:00:00.000Z)")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int(((match.group(7) or "") + "000000")[:6])
    zone = match.group(8)
    if zone in ("Z", "z"):
        tz = dt.UTC
    else:
        off_h, off_m = int(zone[1:3]), int(zone[4:6])
        if off_h > 23 or off_m > 59:
            raise ValueError("timestamp offset out of range")
        delta = dt.timedelta(hours=off_h, minutes=off_m)
        tz = dt.timezone(-delta if zone[0] == "-" else delta)
    return dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=tz).astimezone(dt.UTC)


def format_timestamp(when: dt.datetime) -> str:
    """RFC 3339 in UTC with `Z` and millisecond precision."""
    if when.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    utc = when.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _bad(message: str) -> argparse.ArgumentTypeError:
    return argparse.ArgumentTypeError(message)


def duration(text: str) -> int:
    try:
        return parse_duration(text)
    except DurationError as exc:
        raise _bad(f"invalid duration {echo(text)!r}: {exc}") from None


def duration_or_zero(text: str) -> int:
    """`cancel --wait-timeout`: a duration, or a bare `0` (return at once)."""
    if text == "0":
        return 0
    try:
        return parse_duration(text, allow_zero=True)
    except DurationError as exc:
        raise _bad(f"invalid duration {echo(text)!r}: {exc}") from None


def duration_or_none(text: str) -> int | str:
    return "none" if text == "none" else duration(text)


def timestamp_or_duration(text: str) -> tuple[str, dt.datetime | int]:
    """`export --since`: an RFC 3339 timestamp (inclusive) or a duration back from now."""
    if _DURATION_RE.match(text):
        return ("duration", duration(text))
    try:
        return ("timestamp", parse_timestamp(text))
    except ValueError:
        raise _bad(
            f"invalid value {echo(text)!r}: expected an RFC 3339 timestamp or a duration"
        ) from None


def _int_in(text: str, low: int, high: int | None, what: str) -> int:
    if _INT_RE.match(text) is None:
        raise _bad(f"invalid {what} {echo(text)!r}: not an integer")
    value = int(text)
    if value < low or (high is not None and value > high):
        bound = f"at least {low}" if high is None else f"{low} to {high}"
        raise _bad(f"invalid {what} {echo(text)!r}: must be {bound}")
    return value


def priority(text: str) -> int:
    return _int_in(text, -1000, 1000, "priority")


def max_retries(text: str) -> int:
    return _int_in(text, 0, 100, "max retries")


def positive_int(text: str) -> int:
    return _int_in(text, 1, None, "number")


def nonneg_int(text: str) -> int:
    return _int_in(text, 0, None, "number")


def queue_name(text: str) -> str:
    if QUEUE_RE.match(text) is None:
        raise _bad(f"invalid queue name {echo(text)!r}: use [a-z0-9][a-z0-9_-]{{0,63}}")
    return text


def unique_key(text: str) -> str:
    if UNIQUE_KEY_RE.match(text) is None:
        raise _bad(
            f"invalid unique key {echo(text)!r}: 1 to 256 characters from [A-Za-z0-9._:/#@+=,-]"
        )
    return text


def label_pair(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep:
        raise _bad(f"invalid label {echo(text)!r}: expected key=value")
    if LABEL_KEY_RE.match(key) is None:
        raise _bad(f"invalid label key {echo(key)!r}: use [a-z0-9][a-z0-9_.-]{{0,63}}")
    if LABEL_VALUE_RE.match(value) is None:
        raise _bad(
            f"invalid label value for {key!r}: 0 to 256 characters from [A-Za-z0-9._:/#@+=,-]"
        )
    return key, value


def env_name(text: str) -> str:
    """A `--pass-env` name; never echoes more than the name."""
    if ENV_NAME_RE.match(text) is None:
        raise _bad(f"invalid variable name {echo(text)!r}: use [A-Za-z_][A-Za-z0-9_]{{0,127}}")
    if text.startswith("JOBSCTL_"):
        raise _bad(f"invalid variable name {echo(text)!r}: JOBSCTL_ is reserved")
    return text


def env_assign(text: str) -> tuple[str, str]:
    """An `--env NAME=value` entry. The value is never echoed: it could be a secret."""
    name, sep, value = text.partition("=")
    if not sep:
        raise _bad("invalid --env entry: expected NAME=value")
    env_name(name)
    if "\x00" in value:
        raise _bad(f"the value of {name} contains a NUL byte")
    if _byte_length(value) > ENV_VALUE_MAX_BYTES:
        raise _bad(f"the value of {name} is over 128 KiB")
    return name, value


def decision_id(text: str) -> str:
    if DECISION_ID_RE.match(text) is None:
        raise _bad(
            f"invalid decision id {echo(text)!r}: 1 to 128 characters from [A-Za-z0-9._:@/-]"
        )
    return text


def actor(text: str) -> str:
    try:
        return parse_actor(text)
    except JobsctlError as exc:
        raise _bad(exc.message) from None


def cwd(text: str) -> str:
    """`--cwd`: absolute after lexical normalization (`.` and `..` resolved, symlinks kept)."""
    if "\x00" in text:
        raise _bad("--cwd contains a NUL byte")
    if not text.startswith("/"):
        raise _bad(f"--cwd must be an absolute path, not {echo(text)!r}")
    return posixpath.normpath(text)


def job_id(text: str) -> str:
    """A job id or prefix: `jb-` is optional, then 4 to 8 characters of a-z2-7."""
    body = text[3:] if text.startswith("jb-") else text
    if JOB_ID_PREFIX_RE.match(body) is None:
        raise _bad(
            f"invalid job id {echo(text)!r}: a job id is jb- plus 8 characters of a-z2-7, "
            "or a prefix of at least 4 characters"
        )
    return "jb-" + body


def claim_id(text: str) -> str:
    if not 1 <= len(text) <= 128 or not text.isascii() or not text.isprintable() or " " in text:
        raise _bad("invalid claim id")
    return text


def _byte_length(text: str) -> int:
    try:
        return len(text.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError:
        return len(text.encode("utf-8", "surrogatepass"))


def byte_length(text: str) -> int:
    """UTF-8 length of an argument as the OS passes it (undecodable bytes count as one)."""
    return _byte_length(text)
