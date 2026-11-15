"""Flag value grammars for the argparse surface, as `type=` callables.

The grammars themselves live in `validate.py`, `durations.py`, `timestamps.py` and `ids.py`;
these adapters turn a failure into an argparse error and so into exit 2 `usage`, quoting the
input only through `_q`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import posixpath
import re
from collections.abc import Callable
from typing import Final

from jobsctl import validate
from jobsctl.actors import parse_actor
from jobsctl.durations import DurationError, looks_like_duration, parse_duration
from jobsctl.errors import JobsctlError
from jobsctl.ids import IdError, normalize_id_input
from jobsctl.textsafe import echo, echo_input
from jobsctl.timestamps import TimestampError, parse_timestamp

_INT_RE: Final = re.compile(r"-?[0-9]+")
_MAX_INT_DIGITS: Final = 18  # more significant digits are out of every range; never int() them
ENV_VALUE_MAX_BYTES: Final = 128 * 1024


def _bad(message: str) -> argparse.ArgumentTypeError:
    return argparse.ArgumentTypeError(message)


def _q(text: str) -> str:
    """An input quoted back in a message, through `textsafe.echo_input` (never past an `=`)."""
    return f"'{echo_input(text)}'"


def _checked(check: Callable[[str], str], text: str, what: str) -> str:
    try:
        return check(text)
    except validate.GrammarError as exc:
        raise _bad(f"{what} {_q(text)}: {exc}") from None


def duration(text: str) -> int:
    try:
        return parse_duration(text)
    except DurationError as exc:
        raise _bad(f"invalid duration {_q(text)}: {exc}") from None


def duration_or_zero(text: str) -> int:
    """`cancel --wait-timeout`: a duration, or exactly `0` (return at once)."""
    return 0 if text == "0" else duration(text)


def duration_or_none(text: str) -> int | str:
    return "none" if text == "none" else duration(text)


def timestamp_or_duration(text: str) -> tuple[str, dt.datetime | int]:
    """`export --since`: an RFC 3339 timestamp (inclusive) or a duration back from now."""
    if looks_like_duration(text):
        return ("duration", duration(text))
    try:
        return ("timestamp", parse_timestamp(text))
    except TimestampError:
        raise _bad(
            f"invalid value {_q(text)}: expected an RFC 3339 timestamp or a duration"
        ) from None


def _int_in(text: str, low: int, high: int | None, what: str) -> int:
    if _INT_RE.fullmatch(text) is None:
        raise _bad(f"invalid {what} {_q(text)}: not an integer")
    negative = text.startswith("-")
    digits = text.lstrip("-").lstrip("0")
    if len(digits) > _MAX_INT_DIGITS:
        value = None
    else:
        value = -int(digits or "0") if negative else int(digits or "0")
    if value is None or value < low or (high is not None and value > high):
        bound = f"at least {low}" if high is None else f"{low} to {high}"
        raise _bad(f"invalid {what} {_q(text)}: must be {bound}")
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
    return _checked(validate.queue_name, text, "invalid queue name")


def unique_key(text: str) -> str:
    return _checked(validate.unique_key, text, "invalid unique key")


def label_pair(text: str) -> tuple[str, str]:
    try:
        return validate.parse_label(text)
    except validate.GrammarError as exc:
        key = text.partition("=")[0]
        if exc.part == "key":
            raise _bad(f"invalid label key {_q(key)}: {exc}") from None
        if exc.part == "value":
            raise _bad(f"invalid label value for {echo(key)!r}: {exc}") from None
        raise _bad(f"invalid label {_q(text)}: expected key=value") from None


def env_name(text: str) -> str:
    """A `--pass-env` name; never echoes more than the name (an entry `NAME=value` is refused)."""
    if "=" in text:
        raise _bad(
            f"invalid variable name {_q(text.partition('=')[0])}: --pass-env takes a name, "
            "not NAME=value"
        )
    return _checked(validate.env_name, text, "invalid variable name")


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
    return _checked(validate.decision_id, text, "invalid decision id")


def actor(text: str) -> str:
    try:
        return parse_actor(text)
    except JobsctlError as exc:
        raise _bad(exc.message) from None


def cwd(text: str) -> str:
    """`--cwd`: absolute after lexical normalization (`.` and `..` resolved, symlinks kept)."""
    if "\x00" in text:
        raise _bad("--cwd contains a NUL byte")
    if "\n" in text:
        raise _bad("--cwd contains a newline")
    if not text.startswith("/"):
        raise _bad(f"--cwd must be an absolute path, not {_q(text)}")
    return posixpath.normpath(text)


def job_id(text: str) -> str:
    """A job id or prefix: `jb-` is optional, then 4 to 8 characters of a-z2-7."""
    try:
        return normalize_id_input(text)
    except IdError as exc:
        raise _bad(f"invalid job id {_q(text)}: {exc}") from None


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
