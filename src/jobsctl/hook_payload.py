"""The JSON document a hook gets on stdin (interface.md, Payload; `schemas/hooks/`)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = ["OUTPUT_HOOKS", "build_add_payload", "build_payload", "encode_payload"]

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


def build_add_payload(
    *,
    event_id: str,
    job_id: str,
    queue: str,
    argv: list[str],
    cwd: str,
    env_names: list[str],
    pass_env: list[str],
    labels: list[str],
    unique_key: str | None,
    priority: int,
    pinned: bool,
    top: bool,
    overrides: Mapping[str, Any],
    actor: str,
    decision_ids: list[str],
) -> dict[str, Any]:
    """The `pre-add` payload (`schemas/hooks/pre-add.json`): the job `add` would store, which
    does not exist yet. `job_id` is the id it would get; `previous_status` and `attempt` are
    `null`; `job` has no status, rank or attempt fields. Only variable *names* appear
    (`env_names`, `pass_env`): `add` has refused every secret before this is built, and no value
    of `--env` is ever included."""
    return {
        "tool": "jobsctl",
        "hook": "pre-add",
        "event_id": event_id,
        "object_type": "job",
        "object_id": job_id,
        "repo": None,
        "labels": list(labels),
        "previous_status": None,
        "job": {
            "id": job_id,
            "queue": queue,
            "argv": list(argv),
            "cwd": cwd,
            "env_names": sorted(env_names),
            "pass_env": list(pass_env),
            "labels": list(labels),
            "unique_key": unique_key,
            "priority": priority,
            "pinned": pinned,
            "top": top,
            "overrides": dict(overrides),
            "actor": actor,
            "decision_ids": list(decision_ids),
        },
        "attempt": None,
    }


def encode_payload(payload: Mapping[str, Any]) -> bytes:
    """ASCII-only JSON (`\\uNNNN` escapes) plus a newline: it encodes whatever the strings hold,
    lone surrogates included, and cannot confuse a hook's line reader."""
    return (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
