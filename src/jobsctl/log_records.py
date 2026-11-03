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
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from jobsctl.ids import is_job_id
from jobsctl.textsafe import echo, is_invisible, sanitize, strip_escapes

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
AUDIT_ACTIONS: Final = frozenset(
    {"cancel_force", "cancel_held", "release_override", "resolve", "retry", "prune"}
)
AUDIT_RESULTS: Final = frozenset(
    {"queued", "starting", "running", "held", "done", "failed", "canceled", "deleted"}
)

# Keys that would carry what is never logged (interface.md, Audit and log): argv, environment
# values, raw hook output and output tails. `details` is free-form, so these names are refused
# at any depth, compared after `normalize_key`. A heuristic backstop: callers own what they pass.
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
MAX_FREE_TEXT_CHARS: Final = 4096  # other free text in log records (hook_id)

_TS_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z\Z"
)


class RecordTooLarge(ValueError):
    """The serialized record exceeds :data:`MAX_RECORD_BYTES`."""


def parse_ts(text: object) -> dt.datetime | None:
    """A record's fixed-width ``ts`` (the form ``timestamps.format_timestamp`` writes), or ``None``
    when it is not one; stricter than ``timestamps.parse_timestamp``, which accepts any offset."""
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


def utf8_bytes(text: str) -> bytes:
    """``text`` in UTF-8, for any Python string.

    Undecodable bytes kept by ``surrogateescape`` (as in ``os.fsencode`` of a CLI argument) come
    back as those bytes; a string with any other lone surrogate is encoded with
    ``surrogatepass`` instead.
    """
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass")


def argv_sha256(argv: Sequence[str]) -> str:
    """SHA-256 over each argv element in UTF-8 followed by a NUL byte."""
    digest = hashlib.sha256()
    for element in argv:
        digest.update(utf8_bytes(element))
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


def _unused_key(key: str, taken: Mapping[str, Any]) -> str:
    """``key``, or ``key~2``, ``key~3``, ... when an earlier key already cleaned to it."""
    if key not in taken:
        return key
    n = 2
    while f"{key}~{n}" in taken:
        n += 1
    return f"{key}~{n}"


def clean_value(value: Any, masked: Sequence[str] = ()) -> Any:
    """``value`` with every string (and mapping key) cleaned. Only JSON types are allowed.

    Two keys that clean to the same text are both kept: later ones (in iteration order) get
    ``~2``, ``~3``, ... appended.
    """
    if isinstance(value, str):
        return clean_text(value, masked)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and infinity are not valid JSON")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("record keys must be strings")
            out[_unused_key(clean_text(key, masked), out)] = clean_value(item, masked)
        return out
    if isinstance(value, list | tuple):
        return [clean_value(item, masked) for item in value]
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def normalize_key(key: str) -> str:
    """The form of a ``details`` or ``inputs`` key compared with the forbidden names.

    Terminal escapes and invisible characters (controls, format and bidi characters,
    surrogates, separators) are dropped, the rest NFKC-normalized, case-folded and stripped of
    surrounding whitespace, and ``-`` reads as ``_``.
    """
    text = "".join(ch for ch in strip_escapes(key) if not is_invisible(ch))
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text) if not is_invisible(ch))
    return text.casefold().strip().replace("-", "_")


def _check_detail_keys(details: Any) -> None:
    if isinstance(details, Mapping):
        for key, item in details.items():
            if isinstance(key, str) and normalize_key(key) in FORBIDDEN_DETAIL_KEYS:
                raise ValueError(f"details and inputs must not carry {echo(key)!r}")
            _check_detail_keys(item)
    elif isinstance(details, list | tuple):
        for item in details:
            _check_detail_keys(item)


# -- schema checks -------------------------------------------------------------------------------
# Hand-written mirrors of schemas/log.json, schemas/audit.json and the defs they use; a test
# checks every rule with a refused record and every event with a record that validates.

Check = Callable[[object], bool]


def _pattern(regex: str) -> Check:
    compiled = re.compile(regex)
    return lambda v: isinstance(v, str) and compiled.fullmatch(v) is not None


def _enum(*values: str) -> Check:
    allowed = frozenset(values)
    return lambda v: isinstance(v, str) and v in allowed


def _integer(low: int | None = None, high: int | None = None) -> Check:
    def check(v: object) -> bool:
        if not isinstance(v, int) or isinstance(v, bool):
            return False
        return (low is None or v >= low) and (high is None or v <= high)

    return check


def _nullable(check: Check) -> Check:
    return lambda v: v is None or check(v)


def _array_of(check: Check, *, min_items: int = 0) -> Check:
    return lambda v: isinstance(v, list | tuple) and len(v) >= min_items and all(map(check, v))


_REASON_NAMES = (
    "exit", "resolved", "signaled", "timeout", "terminated", "lost", "spawn_failed",
    "missing_env", "cwd_missing", "canceled", "forced", "vetoed",
)  # fmt: skip
_STATUS_NAMES = ("queued", "starting", "running", "held", "done", "failed", "canceled")

_is_job_id = lambda v: isinstance(v, str) and is_job_id(v)  # noqa: E731
_is_queue = _pattern(r"[a-z0-9][a-z0-9_-]{0,63}")
_is_actor = _pattern(r"(human|auto):[A-Za-z0-9._@-]{1,64}")
_is_decision_ids = _array_of(_pattern(r"[A-Za-z0-9._:@/-]{1,128}"))
_is_status = _enum(*_STATUS_NAMES)
_is_reason = _enum(*_REASON_NAMES)
_is_text = lambda v: isinstance(v, str)  # noqa: E731
_is_seconds = _integer(1)

_LOG_FIELD_CHECKS: Final[Mapping[str, Check]] = {
    "job_id": _nullable(_is_job_id),
    "queue": _nullable(_is_queue),
    "attempt": _nullable(_integer(0)),
    "from": _nullable(_is_status),
    "to": _nullable(_is_status),
    "reason": _nullable(_is_reason),
    "hold_reason": _nullable(_enum("manual", "escalated", "exit_13")),
    "actor": _nullable(_is_actor),
    "decision_ids": _is_decision_ids,
    "attempt_status": _enum("held", "done", "failed", "canceled"),
    "attempt_reason": _nullable(_is_reason),
    "retry_pending": lambda v: isinstance(v, bool),
    "hook": _nullable(
        _enum("pre-start", "post-start", "post-finish", "post-fail", "post-held", "post-cancel")
    ),
    "hook_id": _nullable(lambda v: isinstance(v, str) and len(v) >= 1),
    "outcome": _nullable(_enum("allow", "escalate", "veto", "failed", "timeout")),
    "exit": _nullable(_integer(0, 255)),
    "stderr": _is_text,
    "finding": _nullable(
        _enum(
            "result_file_finalized",
            "boot_changed",
            "claim_orphaned",
            "shim_dead",
            "job_gone",
            "shim_lost",
            "force_resumed",
            "hooks_redelivered",
            "pruned",
        )  # fmt: skip
    ),
    "action": _nullable(
        _enum(
            "finalized",
            "failed_lost",
            "canceled_forced",
            "requeued",
            "marked_shim_lost",
            "kill_started",
            "redelivered",
            "deleted",
        )  # fmt: skip
    ),
    "message": _is_text,
    "details": lambda v: isinstance(v, Mapping),
}
LOG_FIELDS: Final = frozenset(_LOG_FIELD_CHECKS)

# Per event: (required fields, required fields that must not be null).
_EVENT_REQUIRED: Final[Mapping[str, tuple[frozenset[str], frozenset[str]]]] = {
    "transition": (
        frozenset(
            {
                "job_id",
                "queue",
                "from",
                "to",
                "reason",
                "hold_reason",
                "attempt",
                "actor",
                "decision_ids",
            }
        ),  # fmt: skip
        frozenset({"job_id", "queue", "from", "to", "attempt", "actor"}),
    ),
    "reorder": (
        frozenset({"job_id", "queue", "actor", "decision_ids"}),
        frozenset({"job_id", "queue", "actor"}),
    ),
    "label": (
        frozenset({"job_id", "queue", "actor", "decision_ids"}),
        frozenset({"job_id", "queue", "actor"}),
    ),
    "hook_ran": (
        frozenset({"job_id", "hook", "hook_id", "outcome", "exit", "decision_ids"}),
        frozenset({"job_id", "hook", "hook_id", "outcome"}),
    ),
    "reconcile": (frozenset({"job_id", "finding", "action"}), frozenset({"finding", "action"})),
}
# Fields only one event may carry.
_EVENT_ONLY: Final[Mapping[str, str]] = {
    "attempt_status": "transition",
    "attempt_reason": "transition",
    "retry_pending": "transition",
    "hook": "hook_ran",
    "hook_id": "hook_ran",
    "outcome": "hook_ran",
    "exit": "hook_ran",
    "stderr": "hook_ran",
    "finding": "reconcile",
    "action": "reconcile",
}
_ATTEMPT_FIELDS: Final = ("attempt_status", "attempt_reason", "retry_pending")
_RETRY_REASONS: Final = frozenset(
    {"exit", "signaled", "timeout", "terminated", "lost", "spawn_failed", "missing_env"}
)
# Free text: masked, sanitized and clipped. Every other field is an identifier, an enum, a
# number or a list of identifiers, checked above, so it is written as given (masking a value
# that happens to equal a status word or a hex digest would corrupt it).
_FREE_TEXT_LIMITS: Final[Mapping[str, int]] = {
    "message": MAX_MESSAGE_CHARS,
    "stderr": MAX_STDERR_CHARS,
    "hook_id": MAX_FREE_TEXT_CHARS,
}


def _check_log_fields(event: str, fields: Mapping[str, Any]) -> None:
    for name, value in fields.items():
        if not _LOG_FIELD_CHECKS[name](value):
            raise ValueError(f"invalid {name} for {event}")
        only = _EVENT_ONLY.get(name)
        if only is not None and only != event:
            raise ValueError(f"{name} is only for {only}")
    required, non_null = _EVENT_REQUIRED.get(event, (frozenset(), frozenset()))
    missing = sorted(required - set(fields))
    if missing:
        raise ValueError(f"{event} requires {', '.join(missing)}")
    nulls = sorted(name for name in non_null if fields[name] is None)
    if nulls:
        raise ValueError(f"{event} requires non-null {', '.join(nulls)}")
    present = [name for name in _ATTEMPT_FIELDS if name in fields]
    if present and len(present) != len(_ATTEMPT_FIELDS):
        raise ValueError("attempt_status, attempt_reason and retry_pending go together")
    if fields.get("retry_pending") is True and not (
        fields.get("to") == "queued"
        and fields.get("attempt_status") == "failed"
        and fields.get("attempt_reason") in _RETRY_REASONS
    ):
        raise ValueError("retry_pending needs to queued and a failed attempt with a retry reason")
    if fields.get("outcome") == "allow" and "stderr" in fields:
        raise ValueError("an allow outcome carries no stderr")
    if fields.get("outcome") == "timeout" and fields.get("exit") is not None:
        raise ValueError("a timeout outcome has exit null")


_PRUNE_INPUT_CHECKS: Final[Mapping[str, Check]] = {
    "ids": _array_of(_is_job_id, min_items=1),
    "older_than": _is_seconds,
    "trigger": _enum("command", "retention"),
}
_JOB_INPUT_CHECKS: Final[Mapping[str, Check]] = {
    "argv_sha256": _pattern(r"[0-9a-f]{64}"),
    "attempt": _integer(0),
    "unique_key": _nullable(_pattern(r"[A-Za-z0-9._:/#@+=,-]{1,256}")),
    "queue": _is_queue,
}
_OVERRIDE_CHECKS: Final[Mapping[str, Check]] = {
    "timeout": lambda v: v == "none" or _is_seconds(v),
    "max_retries": _integer(0, 100),
    "kill_grace": _is_seconds,
}


def _check_shape(what: str, value: Mapping[str, Any], checks: Mapping[str, Check]) -> None:
    unknown = sorted(set(value) - set(checks))
    if unknown:
        raise ValueError(f"unknown {what}: {', '.join(echo(k) for k in unknown)}")
    for name, check in checks.items():
        if name in value and not check(value[name]):
            raise ValueError(f"invalid {what} {name}")


def _check_audit_inputs(action: str, inputs: Mapping[str, Any]) -> None:
    if action == "prune":
        _check_shape("prune inputs", inputs, _PRUNE_INPUT_CHECKS)
        required: Iterable[str] = _PRUNE_INPUT_CHECKS
    else:
        rest = {k: v for k, v in inputs.items() if k != "overrides"}
        _check_shape("inputs", rest, _JOB_INPUT_CHECKS)
        required = _JOB_INPUT_CHECKS
        if "overrides" in inputs:
            overrides = inputs["overrides"]
            if action != "retry":
                raise ValueError("only retry records overrides")
            if not isinstance(overrides, Mapping) or not overrides:
                raise ValueError("overrides must be a non-empty object")
            _check_shape("overrides", overrides, _OVERRIDE_CHECKS)
    missing = sorted(set(required) - set(inputs))
    if missing:
        raise ValueError(f"{action} inputs require {', '.join(missing)}")


# -- builders ------------------------------------------------------------------------------------


def build_log_record(
    event: str,
    level: str,
    fields: Mapping[str, Any] | None,
    masked: Sequence[str] = (),
) -> dict[str, Any]:
    """The cleaned fields of a log record (``seq`` and ``ts`` are added by the writer).

    The record must match ``schemas/log.json``: its event's required fields, the fields that
    event may carry, enums and types are checked here, and :class:`ValueError` (or
    :class:`TypeError` for a value that is not JSON) is raised before anything is written.
    Only free text (``message``, ``stderr``, ``hook_id``, ``details``) is masked and sanitized;
    ``message`` and ``hook_id`` are clipped to 4096 characters and ``stderr`` to 2048.
    """
    fields = dict(fields or {})
    if event not in LOG_EVENTS:
        raise ValueError(f"unknown log event {echo(event)!r}")
    if level not in LEVELS:
        raise ValueError(f"unknown log level {echo(level)!r}")
    unknown = sorted(set(fields) - LOG_FIELDS)
    if unknown:
        raise ValueError(f"unknown log fields: {', '.join(echo(k) for k in unknown)}")
    _check_log_fields(event, fields)
    if "details" in fields:
        _check_detail_keys(fields["details"])
    record: dict[str, Any] = {"level": level, "event": event}
    for name, value in fields.items():
        if name == "details":
            record[name] = clean_value(value, masked)
        elif name in _FREE_TEXT_LIMITS and value is not None:
            record[name] = _clip(clean_text(value, masked), _FREE_TEXT_LIMITS[name])
        else:
            record[name] = list(value) if isinstance(value, list | tuple) else value
    return record


def build_audit_record(
    *,
    action: str,
    actor: str,
    decision_id: Sequence[str],
    target: str | None,
    inputs: Mapping[str, Any],
    result: str,
) -> dict[str, Any]:
    """The fields of an audit record, in schema order (without ``seq`` and ``ts``).

    The record must match ``schemas/audit.json``, including the shape of ``inputs`` for the
    action. Every audit field is then an identifier, an enum or a number, so nothing is masked
    (masking would corrupt a field equal to a masked value; the unique key is shown unmasked by
    ``list`` and ``show`` anyway).
    """
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {echo(str(action))!r}")
    if result not in AUDIT_RESULTS:
        raise ValueError(f"unknown audit result {echo(str(result))!r}")
    if not _is_actor(actor):
        raise ValueError("invalid actor")
    if not _is_decision_ids(decision_id):
        raise ValueError("invalid decision id")
    if (action == "prune") != (target is None) or (action == "prune") != (result == "deleted"):
        raise ValueError("prune has target null and result deleted, and only prune does")
    if target is not None and not _is_job_id(target):
        raise ValueError("invalid target")
    if not isinstance(inputs, Mapping):
        raise ValueError("inputs must be an object")
    _check_detail_keys(inputs)
    _check_audit_inputs(action, inputs)
    return {
        "actor": actor,
        "decision_id": list(decision_id),
        "target": target,
        "action": action,
        "inputs": clean_value(inputs),
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
