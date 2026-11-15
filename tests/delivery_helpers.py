"""Helpers of the delivery tests: hook commands that record what they got, fake hook results."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jobsctl.hook_run import HookResult
from jobsctl.store_db import OpenMode, Store


def recorder(out: Path, name: str = "ran") -> str:
    """A hook command that saves its stdin to `<out>/<name>-<job id>-<n>.json` (n = run count)."""
    out.mkdir(exist_ok=True)
    return f'n=$(ls {out} | grep -c "^{name}-$JOBSCTL_ID-"); cat > {out}/{name}-$JOBSCTL_ID-$n.json'


def ran(out: Path, name: str = "ran") -> list[Path]:
    return sorted(out.glob(f"{name}-*.json")) if out.exists() else []


def fake_result(hook: str, hook_id: str = "h", *, stopped: str | None = None) -> HookResult:
    return HookResult(
        hook=hook,
        hook_id=hook_id,
        outcome="failed" if stopped else "allow",
        exit=None if stopped else 0,
        signal=None,
        decision_ids=(),
        warnings=(),
        stderr="" if stopped else None,
        message=None,
        stdout_truncated=False,
        stderr_truncated=False,
        leftovers=0,
        stopped=stopped,
        start_error=None,
        signals_sent=(),
        unkillable=False,
        duration=0.0,
    )


def open_store() -> Store:
    return Store.open(busy_timeout=5.0, mode=OpenMode.CLI)


def pending(store: Store, *, is_async: bool | None = None) -> list[Any]:
    sql = "SELECT * FROM hook_outbox WHERE delivered_at IS NULL"
    if is_async is not None:
        sql += f" AND async = {int(is_async)}"
    return store.query_all(sql + " ORDER BY seq")
