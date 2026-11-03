"""Job ids (interface.md, Identifiers): generation and prefix resolution.

Pure and storage-free. Collision checks and prefix lookups take callables that the state store
supplies: `taken` must answer for both `jobs` and `pruned_ids` (ids are never reused), while the
prefix `find` consults `jobs` only, so a pruned id resolves to `not_found`. Randomness is
injectable for tests.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable, Iterable
from typing import Final

from jobsctl.errors import JobsctlError, usage_error
from jobsctl.textsafe import echo_input

ID_PREFIX: Final = "jb-"
ID_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
ID_BODY_LEN: Final = 8
MIN_PREFIX_LEN: Final = 4
MAX_GENERATE_ATTEMPTS: Final = 32

JOB_ID_RE: Final = re.compile(r"jb-[a-z2-7]{8}")
_BODY_RE: Final = re.compile(r"[a-z2-7]{4,8}")
_ID_BYTES: Final = 5  # 40 bits = exactly 8 base32 characters, no padding and no modulo bias


class IdError(ValueError):
    """The input is not a job id or prefix; the message states the rule only."""


def is_job_id(text: str) -> bool:
    """Whether `text` is a full id (`jb-` and 8 characters of a-z2-7)."""
    return JOB_ID_RE.fullmatch(text) is not None


def new_job_id(random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """One random id. `random_bytes` must return exactly the requested number of bytes."""
    raw = random_bytes(_ID_BYTES)
    if len(raw) != _ID_BYTES:
        raise ValueError("random_bytes returned the wrong number of bytes")
    return ID_PREFIX + base64.b32encode(raw).decode("ascii").lower()


def generate_job_id(
    taken: Callable[[str], bool],
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    max_attempts: int = MAX_GENERATE_ATTEMPTS,
) -> str:
    """A random id for which `taken(id)` is false, retrying on a collision.

    `taken` must cover live jobs and `pruned_ids`. Gives up with an `internal` error after
    `max_attempts` (a real store never gets there: 2^40 ids).
    """
    for _ in range(max_attempts):
        candidate = new_job_id(random_bytes)
        if not taken(candidate):
            return candidate
    raise JobsctlError("internal", f"no free job id after {max_attempts} attempts")


def normalize_id_input(text: str) -> str:
    """An id or prefix as typed, normalised to carry `jb-`: `jb-` optional, then 4 to 8
    characters of a-z2-7 (lowercase only)."""
    body = text[len(ID_PREFIX) :] if text.startswith(ID_PREFIX) else text
    if _BODY_RE.fullmatch(body) is None:
        raise IdError(
            "a job id is jb- plus 8 characters of a-z2-7, or a prefix of at least 4 characters"
        )
    return ID_PREFIX + body


def resolve_job_id(text: str, find: Callable[[str], Iterable[str]]) -> str:
    """The full id that `text` (an id or unique prefix) names.

    `find(prefix)` returns the ids in `jobs` that start with the normalised prefix (including
    `jb-`). Raises exit-2 `usage` for a malformed or too-short input, exit-21 `ambiguous` with
    sorted `candidates` for several matches and exit-10 `not_found` for none (an unknown id, or
    a pruned one, which `find` does not return).
    """
    try:
        prefix = normalize_id_input(text)
    except IdError as exc:
        raise usage_error(f"invalid job id '{echo_input(text)}': {exc}") from None
    matches = sorted(set(find(prefix)))
    for match in matches:
        if not is_job_id(match) or not match.startswith(prefix):
            raise JobsctlError("internal", "id lookup returned an id that does not match")
    if not matches:
        raise JobsctlError("not_found", f"no job matches '{prefix}'")
    if len(matches) > 1:
        raise JobsctlError(
            "ambiguous", f"'{prefix}' matches {len(matches)} jobs", candidates=matches
        )
    return matches[0]
