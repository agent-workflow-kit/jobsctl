"""Human (non-JSON) output of a job and its attempts (interface.md, Commands, Human output).

Unstable and not for parsing. Every string that came from storage or a user passes through the
sanitizer first: fields inline (a newline cannot start a forged line), the output tail as lines
indented under a fixed prefix so no output line can pass for a field.
"""

from __future__ import annotations

import shlex
from typing import Any

from jobsctl.textsafe import sanitize

TAIL_PREFIX = "  | "


def clean(value: object) -> str:
    return sanitize(str(value), inline=True)


def exit_text(record: dict[str, Any]) -> str:
    kind = record["kind"]
    if kind == "exited":
        return f"exited {clean(record['code'])}"
    if kind == "signaled":
        return f"signaled {clean(record['signal'])}" + (" (core dumped)" if record["core"] else "")
    return clean(kind)


def job_line(job: dict[str, Any]) -> str:
    """One line for `list`."""
    state = clean(job["status"])
    detail = job["reason"] or job["hold_reason"]
    if detail:
        state += f"({clean(detail)})"
    parts = [clean(job["id"]), state, f"queue={clean(job['queue'])}"]
    if job["rank"] is not None:
        parts.append(f"rank={clean(job['rank'])}")
    if job["blocked_by"]:
        parts.append(f"blocked_by={clean(job['blocked_by'])}")
    parts.append("--")
    parts.extend(shlex.quote(clean(arg)) for arg in job["argv"])
    return " ".join(parts)


def _field(lines: list[str], name: str, value: object) -> None:
    if value is not None and value != [] and value != {}:
        lines.append(f"{name}: {clean(value)}")


def show_text(job: dict[str, Any]) -> str:
    """The report of `show`: fields, attempts, then the tail (when there is one)."""
    lines = [job_line(job)]
    _field(lines, "cwd", clean(job["cwd"]))
    _field(lines, "unique_key", None if job["unique_key"] is None else clean(job["unique_key"]))
    _field(lines, "labels", ", ".join(clean(label) for label in job["labels"]) or None)
    _field(lines, "env", ", ".join(f"{clean(k)}={clean(v)}" for k, v in job["env"].items()) or None)
    _field(lines, "pass_env", ", ".join(clean(n) for n in job["pass_env"]) or None)
    _field(lines, "actor", clean(job["actor"]))
    for name in ("created_at", "enqueued_at", "status_changed_at", "claimed_at", "ended_at"):
        _field(lines, name, job[name])
    _field(lines, "not_before", job["not_before"])
    _field(lines, "exit", None if job["exit"] is None else exit_text(job["exit"]))
    _field(lines, "resolved_by", None if job["resolved_by"] is None else clean(job["resolved_by"]))
    _field(lines, "note", None if job["note"] is None else clean(job["note"]))
    if job["shim_lost"]:
        lines.append("shim_lost: true")
    for attempt in job["attempts"]:
        state = clean(attempt["status"])
        detail = attempt["reason"] or attempt["hold_reason"]
        if detail:
            state += f"({clean(detail)})"
        lines.append(
            f"attempt {clean(attempt['attempt'])}: {state} claimed {clean(attempt['claimed_at'])}"
        )
        if attempt["exit"] is not None:
            lines.append(f"  exit: {exit_text(attempt['exit'])}")
        for message in attempt["hook_messages"]:
            lines.append(
                f"  hook {clean(message['hook_id'])} {clean(message['outcome'])}: "
                f"{clean(message['message'])}"
            )
        if attempt["note"]:
            lines.append(f"  note: {clean(attempt['note'])}")
    output = job["output"]
    if output is not None:
        flags = " (earlier output dropped)" if output["truncated"] else ""
        lines.append(f"output{flags}:")
        text = sanitize(output["tail"])
        lines.extend(TAIL_PREFIX + line for line in text.rstrip("\n").split("\n"))
        log = output.get("log")
        if log is not None:
            stopped = " (stopped at the size cap)" if log["truncated"] else ""
            lines.append(f"full output log: {clean(log['path'])} ({log['bytes']} bytes){stopped}")
    return "\n".join(lines) + "\n"
