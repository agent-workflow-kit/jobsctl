"""Value grammars shared by every command (interface.md: Queues, Labels, Unique keys, Job
environment, Commands).

Each validator returns the value or raises `GrammarError`, whose message states the rule and
never includes the input (callers echo through `textsafe.echo` if they must). Every pattern is
matched with `fullmatch` and explicit ASCII classes, so a trailing newline, a Unicode digit or
lookalike, or a NUL never passes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from jobsctl.errors import JobsctlError, usage_error

QUEUE_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LABEL_KEY_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
LABEL_VALUE_RE: Final = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}")
UNIQUE_KEY_RE: Final = re.compile(r"[A-Za-z0-9._:/#@+=,-]{1,256}")
ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
DECISION_ID_RE: Final = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
MAX_LABELS: Final = 32
RESERVED_ENV_PREFIX: Final = "JOBSCTL_"

Label = tuple[str, str]


class GrammarError(ValueError):
    """The input breaks a grammar; the message states the rule only.

    `part` names the failing part of a compound value (`key` or `value` of a label), or is
    None when the value as a whole is malformed.
    """

    def __init__(self, message: str, *, part: str | None = None) -> None:
        super().__init__(message)
        self.part = part


def queue_name(text: str) -> str:
    if QUEUE_RE.fullmatch(text) is None:
        raise GrammarError("use [a-z0-9][a-z0-9_-]{0,63}")
    return text


def unique_key(text: str) -> str:
    if UNIQUE_KEY_RE.fullmatch(text) is None:
        raise GrammarError("1 to 256 characters from [A-Za-z0-9._:/#@+=,-]")
    return text


def label_key(text: str) -> str:
    if LABEL_KEY_RE.fullmatch(text) is None:
        raise GrammarError("use [a-z0-9][a-z0-9_.-]{0,63}")
    return text


def label_value(text: str) -> str:
    if LABEL_VALUE_RE.fullmatch(text) is None:
        raise GrammarError("0 to 256 characters from [A-Za-z0-9._:/#@+=,-]")
    return text


def parse_label(text: str) -> Label:
    """`key=value`, split at the first `=` (a value may contain further `=`).

    A bad key or value raises `GrammarError` with `part` set to `key` or `value`.
    """
    key, sep, value = text.partition("=")
    if not sep:
        raise GrammarError("expected key=value")
    try:
        label_key(key)
    except GrammarError as exc:
        raise GrammarError(str(exc), part="key") from None
    try:
        label_value(value)
    except GrammarError as exc:
        raise GrammarError(str(exc), part="value") from None
    return key, value


def env_name(text: str) -> str:
    """An `--env` / `--pass-env` variable name; `JOBSCTL_` names are reserved."""
    if ENV_NAME_RE.fullmatch(text) is None:
        raise GrammarError("use [A-Za-z_][A-Za-z0-9_]{0,127}")
    if text.startswith(RESERVED_ENV_PREFIX):
        raise GrammarError("JOBSCTL_ is reserved")
    return text


def decision_id(text: str) -> str:
    if DECISION_ID_RE.fullmatch(text) is None:
        raise GrammarError("1 to 128 characters from [A-Za-z0-9._:@/-]")
    return text


def merge_labels(existing: Iterable[Label], added: Iterable[Label]) -> list[Label]:
    """The label set after adding `added`: set semantics (a repeated pair is a no-op, a key may
    carry several values), sorted by (key, value). More than 32 distinct pairs is exit 2
    (`usage`, `details.limit`)."""
    merged = {*existing, *added}
    if len(merged) > MAX_LABELS:
        raise too_many_labels()
    return sorted(merged)


def too_many_labels() -> JobsctlError:
    return usage_error(f"a job has at most {MAX_LABELS} labels", limit=MAX_LABELS)
