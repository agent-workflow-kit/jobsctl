"""Once-per-state log warnings for the `env_file` (interface.md, Audit and log).

`env_file_unsafe`, `env_file_line_invalid` and `env_file_line_ignored` are state warnings: they
are logged when the state changes, not at every load, and re-armed when the condition is gone.
Each record names the file's configured path (never a value) and, for lines, the line numbers.
"""

from __future__ import annotations

from jobsctl.env_canonical import CanonicalEnv
from jobsctl.env_file import EnvFile
from jobsctl.log_writer import RecordWriter


def log_env_file_state(writer: RecordWriter, path: str | None, env_file: EnvFile) -> None:
    """Log the `env_file` warnings of this load once per change of state. Call outside any
    transaction."""
    if path is None:
        return  # not configured: nothing to warn about
    where = path
    unsafe = next((w for w in env_file.warnings if w.code == "env_file_unsafe"), None)
    writer.warn_once(
        f"env_file_unsafe:{where}",
        None if unsafe is None else f"reason={unsafe.reason}",
        "env_file_unsafe",
        fields={
            "message": "env_file is not trusted and is treated as unavailable",
            "details": {"reason": None if unsafe is None else unsafe.reason},
        },
    )
    invalid = sorted(
        w.line for w in env_file.warnings if w.code == "env_file_line_invalid" and w.line
    )
    ignored = sorted(w.line for w in env_file.hook_values()[1] if w.line)
    for event, lines, message in (
        ("env_file_line_invalid", invalid, "env_file has lines that are not NAME=value"),
        ("env_file_line_ignored", ignored, "env_file sets names that hooks never receive"),
    ):
        writer.warn_once(
            f"{event}:{where}",
            ",".join(map(str, lines)) if lines else None,
            event,
            fields={"message": message, "details": {"lines": lines}},
        )


def log_path_state(writer: RecordWriter, canonical: CanonicalEnv) -> None:
    """Log `path_entry_dropped` for the cleaned `PATH` once per change of state (the dropped
    entries are the state; none re-arms it). Call outside any transaction."""
    dropped = [w for w in canonical.warnings if w.code == "path_entry_dropped"]
    writer.warn_once(
        "path_entry_dropped",
        repr(canonical.state_key) if dropped else None,
        "path_entry_dropped",
        fields={
            "message": "PATH entries were dropped when cleaning the environment",
            "details": {
                "entries": [
                    {"reason": w.reason, **({"path": w.path} if w.path else {})} for w in dropped
                ]
            },
        },
    )
