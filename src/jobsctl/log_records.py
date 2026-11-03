"""Shape, cleaning and serialization of ``log.jsonl`` and ``audit.jsonl`` records.

Interface.md, Audit and log. A record is built from caller fields, cleaned (masked values
replaced, terminal escapes and control characters made visible, lone surrogates escaped) and
serialized as one ASCII-only JSON line, so no field can add a line, forge a record or write a
raw control character. The writer adds ``seq`` and ``ts`` under the file lock.

The key sets below mirror ``schemas/log.json`` and ``schemas/audit.json``; a test compares them,
and every record the tests write is validated against the schemas.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from jobsctl.textsafe import sanitize

LEVELS: Final = frozenset({"info", "warn", "error"})
LOG_EVENTS: Final = frozenset(
    {
        "transition", "reorder", "label", "queue_created", "shim_spawned", "shim_spawn_failed",
        "shim_lost", "leftovers", "short_secret_not_masked", "env_file_unsafe",
        "env_file_line_ignored", "env_file_line_invalid", "path_entry_dropped",
        "pass_env_undefined", "missing_env", "unique_key_reused", "hook_ran",
        "invalid_decision_id", "unknown_queue_selector", "config_invalid", "runner_stuck",
        "reconcile", "pass",
    }
)  # fmt: skip
LOG_FIELDS: Final = frozenset(
    {
        "job_id", "queue", "attempt", "from", "to", "reason", "hold_reason", "actor",
        "decision_ids", "attempt_status", "attempt_reason", "retry_pending", "hook", "hook_id",
        "outcome", "exit", "stderr", "finding", "action", "message", "details",
    }
)  # fmt: skip
AUDIT_ACTIONS: Final = frozenset(
    {"cancel_force", "cancel_held", "release_override", "resolve", "retry", "prune"}
)
AUDIT_RESULTS: Final = frozenset(
    {"queued", "starting", "running", "held", "done", "failed", "canceled", "deleted"}
)

# Keys that would carry what is never logged (interface.md, Audit and log): argv, environment
# values, raw hook output and output tails. `details` is free-form, so these names are refused.
FORBIDDEN_DETAIL_KEYS: Final = frozenset(
    {
        "argv", "env", "pass_env", "env_file", "environ", "environment", "value", "values",
        "secret", "output", "tail", "stdout", "stderr",
    }
)  # fmt: skip

MIN_MASKED_LENGTH: Final = 8  # shorter values are not masked (interface.md, Masking)
MASK: Final = "***"
MAX_RECORD_BYTES: Final = 256 * 1024
_SEQ_TS_OVERHEAD: Final = 96  # `"seq":<20 digits>,"ts":"...",` and the newline, rounded up
MAX_MESSAGE_CHARS: Final = 4096
MAX_STDERR_CHARS: Final = 2048  # schemas/log.json stderr maxLength

_ACTOR_RE: Final = re.compile(r"(human|auto):[A-Za-z0-9._@-]{1,64}\Z")
_DECISION_RE: Final = re.compile(r"[A-Za-z0-9._:@/-]{1,128}\Z")
_JOB_ID_RE: Final = re.compile(r"jb-[a-z2-7]{8}\Z")  # TODO(#4): the shared job-id grammar
_TS_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z\Z"
)


class RecordTooLarge(ValueError):
    """The serialized record exceeds :data:`MAX_RECORD_BYTES`."""


def format_ts(when: dt.datetime) -> str:
    """UTC with ``Z`` and millisecond precision. TODO(#4): ``timestamps.format_timestamp``."""
    if when.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    utc = when.astimezone(dt.UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond // 1000:03d}Z"
    )


def parse_ts(text: object) -> dt.datetime | None:
    """A record's fixed-width ``ts``, or ``None`` when it is not one. TODO(#4)."""
    if not isinstance(text, str):
        return None
    match = _TS_RE.match(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, milli = (int(g) for g in match.groups())
    try:
        return dt.datetime(year, month, day, hour, minute, second, milli * 1000, tzinfo=dt.UTC)
    except ValueError:
        return None


def argv_sha256(argv: Sequence[str]) -> str:
    """SHA-256 over each argv element in UTF-8 followed by a NUL byte."""
    digest = hashlib.sha256()
    for element in argv:
        digest.update(element.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
    return digest.hexdigest()


# -- cleaning ------------------------------------------------------------------------------------


def _mask_text(text: str, masked: Sequence[str]) -> str:
    for value in masked:
        if value in text:
            text = text.replace(value, MASK)
    return text


def clean_text(text: str, masked: Sequence[str] = ()) -> str:
    """Mask, make control and escape characters visible, then mask again.

    Masking runs on both sides of the sanitizer so a value is caught as given and as
    sanitized. ``masked`` is longest first (see :func:`masking_set`).
    """
    return _mask_text(sanitize(_mask_text(text, masked), inline=True), masked)


def masking_set(values: Iterable[str]) -> tuple[str, ...]:
    """The values to mask: at least 8 characters, longest first, without duplicates."""
    unique = {v for v in values if len(v) >= MIN_MASKED_LENGTH}
    return tuple(sorted(unique, key=lambda v: (-len(v), v)))


def clean_value(value: Any, masked: Sequence[str] = ()) -> Any:
    """``value`` with every string (and mapping key) cleaned. Only JSON types are allowed."""
    if isinstance(value, str):
        return clean_text(value, masked)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("NaN and infinity are not valid JSON")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("record keys must be strings")
            out[clean_text(key, masked)] = clean_value(item, masked)
        return out
    if isinstance(value, list | tuple):
        return [clean_value(item, masked) for item in value]
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _check_detail_keys(details: Any) -> None:
    if isinstance(details, Mapping):
        for key, item in details.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_DETAIL_KEYS:
                raise ValueError(f"details must not carry {key!r}")
            _check_detail_keys(item)
    elif isinstance(details, list | tuple):
        for item in details:
            _check_detail_keys(item)


# -- builders ------------------------------------------------------------------------------------


def build_log_record(
    event: str,
    level: str,
    fields: Mapping[str, Any] | None,
    masked: Sequence[str] = (),
) -> dict[str, Any]:
    """The cleaned fields of a log record (``seq`` and ``ts`` are added by the writer)."""
    fields = dict(fields or {})
    if event not in LOG_EVENTS:
        raise ValueError(f"unknown log event {event!r}")
    if level not in LEVELS:
        raise ValueError(f"unknown log level {level!r}")
    unknown = sorted(set(fields) - LOG_FIELDS)
    if unknown:
        raise ValueError(f"unknown log fields: {', '.join(unknown)}")
    if "details" in fields:
        if not isinstance(fields["details"], Mapping):
            raise ValueError("details must be an object")
        _check_detail_keys(fields["details"])
    for name in ("job_id",):
        value = fields.get(name)
        if value is not None and not (isinstance(value, str) and _JOB_ID_RE.match(value)):
            raise ValueError(f"invalid {name}")
    actor = fields.get("actor")
    if actor is not None and not (isinstance(actor, str) and _ACTOR_RE.match(actor)):
        raise ValueError("invalid actor")
    _check_decision_ids(fields.get("decision_ids", []))
    record: dict[str, Any] = {"level": level, "event": event}
    record.update(clean_value(fields, masked))
    if "message" in record:
        record["message"] = _clip(str(record["message"]), MAX_MESSAGE_CHARS)
    if "stderr" in record:
        record["stderr"] = _clip(str(record["stderr"]), MAX_STDERR_CHARS)
    return record


def _check_decision_ids(value: object) -> None:
    if not isinstance(value, list | tuple) or not all(
        isinstance(v, str) and _DECISION_RE.match(v) for v in value
    ):
        raise ValueError("invalid decision id")


def build_audit_record(
    *,
    action: str,
    actor: str,
    decision_id: Sequence[str],
    target: str | None,
    inputs: Mapping[str, Any],
    result: str,
    masked: Sequence[str] = (),
) -> dict[str, Any]:
    """The cleaned fields of an audit record, in schema order (without ``seq`` and ``ts``)."""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    if result not in AUDIT_RESULTS:
        raise ValueError(f"unknown audit result {result!r}")
    if not _ACTOR_RE.match(actor):
        raise ValueError("invalid actor")
    _check_decision_ids(decision_id)
    if (action == "prune") != (target is None) or (action == "prune") != (result == "deleted"):
        raise ValueError("prune has target null and result deleted, and only prune does")
    if target is not None and not _JOB_ID_RE.match(target):
        raise ValueError("invalid target")
    if not isinstance(inputs, Mapping):
        raise ValueError("inputs must be an object")
    _check_detail_keys({k: v for k, v in inputs.items() if k != "argv_sha256"})
    return {
        "actor": actor,
        "decision_id": list(clean_value(list(decision_id), masked)),
        "target": target,
        "action": action,
        "inputs": clean_value(dict(inputs), masked),
        "result": result,
    }


# -- serialization -------------------------------------------------------------------------------


def dumps(record: Mapping[str, Any]) -> str:
    """One compact ASCII-only JSON object, without a newline."""
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def encode_line(record: Mapping[str, Any], *, shrink_details: bool) -> bytes:
    """``record`` as one newline-terminated line, within :data:`MAX_RECORD_BYTES`.

    A log record that is too large loses its ``details`` first (replaced by
    ``{"truncated": true}``); anything still too large raises :class:`RecordTooLarge`.
    """
    limit = MAX_RECORD_BYTES - _SEQ_TS_OVERHEAD
    line = dumps(record)
    if len(line) > limit and shrink_details and "details" in record:
        line = dumps({**record, "details": {"truncated": True}})
    if len(line) > limit:
        raise RecordTooLarge(f"record is {len(line)} bytes; the limit is {MAX_RECORD_BYTES}")
    return line.encode("ascii") + b"\n"
