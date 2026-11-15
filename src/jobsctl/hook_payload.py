"""The JSON document a hook gets on stdin (interface.md, Payload; `schemas/hooks/`)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = ["OUTPUT_HOOKS", "build_payload", "encode_payload"]

# Hooks whose payload carries `output_available` and `output_tail`.
OUTPUT_HOOKS = frozenset({"post-finish", "post-fail", "post-held", "post-cancel"})
MAX_OUTPUT_TAIL_CHARS = 262144


def build_payload(
    hook: str,
    *,
    event_id: str,
    job: Mapping[str, Any],
    previous_status: str,
    attempt: Mapping[str, Any] | None,
    output_available: bool = False,
    output_tail: str | None = None,
    include_output: bool = False,
) -> dict[str, Any]:
    """The payload of `hook` for `job` after its transition.

    `job` is the job object after the transition; its `env` (values) is replaced by `env_names`
    and everything else is kept, so a value never reaches a hook. `labels` are the job's labels
    at transition time. `output_tail` (already masked and sanitized) is included only for an
    output hook with `include_output` when `output_available`; else it is `null`.
    """
    shown = {key: value for key, value in job.items() if key != "env"}
    shown["env_names"] = sorted(job.get("env") or ())
    payload: dict[str, Any] = {
        "tool": "jobsctl",
        "hook": hook,
        "event_id": event_id,
        "object_type": "job",
        "object_id": job["id"],
        "repo": None,
        "labels": list(job["labels"]),
        "previous_status": previous_status,
        "job": shown,
        "attempt": None if attempt is None else dict(attempt),
    }
    if hook in OUTPUT_HOOKS:
        payload["output_available"] = output_available
        payload["output_tail"] = (
            output_tail[:MAX_OUTPUT_TAIL_CHARS]
            if include_output and output_available and output_tail is not None
            else None
        )
    return payload


def encode_payload(payload: Mapping[str, Any]) -> bytes:
    """ASCII-only JSON (`\\uNNNN` escapes) plus a newline: it encodes whatever the strings hold,
    lone surrogates included, and cannot confuse a hook's line reader."""
    return (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
