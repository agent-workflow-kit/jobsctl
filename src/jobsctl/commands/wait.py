"""``jobsctl wait <id> [--timeout DURATION]`` and the wait of ``add --wait`` (interface.md, wait).

The wait polls the database read-only, one cheap transaction per second (`POLL_SECONDS`), and
sleeps through `gateway.sleep` so tests drive it with a fake clock. It never writes, so it
creates no state directory and cannot disturb a job: interrupting it (SIGINT, and SIGTERM or
SIGHUP, which are treated the same way) prints the job and exits 130 and leaves the job alone.

A job ends the wait when it is `done`, `failed`, `canceled` or `held`. A `queued` job (also one
waiting for its automatic retry, `not_before` set) does not.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import load_env_file
from jobsctl.errors import ExitCode, JobsctlError, wait_exit_code
from jobsctl.job_detail import job_detail
from jobsctl.job_lookup import find_job
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from jobsctl.textsafe import sanitize
from jobsctl.timestamps import format_timestamp

POLL_SECONDS: Final = 1.0
_ENDS: Final = ("done", "failed", "canceled", "held")


@dataclass(frozen=True)
class WaitResult:
    code: int
    job: dict[str, Any]


@contextmanager
def interrupts_as_keyboard_interrupt() -> Iterator[None]:
    """SIGTERM and SIGHUP interrupt a wait like Ctrl-C does (only on the main thread)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    old = {s: signal.signal(s, raise_interrupt) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)


def _open(ctx: Context) -> Store | None:
    """The database read-only; `None` when there is none (nothing is created)."""
    if not os.path.lexists(resolve_state_dir() / "state.db"):
        return None
    store = Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY)
    if store.schema_version == 0:  # an empty file: no job exists
        store.close()
        return None
    return store


def _snapshot(ctx: Context, store: Store, job_id: str) -> dict[str, Any] | None:
    with store.read() as tx:
        row = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
        if row is None:
            return None
        unavailable = load_env_file(ctx.config.env_file).status == "unavailable"
        return job_detail(
            tx,
            row,
            ctx.config,
            env_file_unavailable=unavailable,
            now=format_timestamp(gateway.utc_now()),
        )


def _pruned(job_id: str) -> JobsctlError:
    return JobsctlError("not_found", f"job {job_id} no longer exists (it was pruned)")


def _poll(ctx: Context, store: Store, job_id: str, timeout: int | None) -> WaitResult:
    deadline = None if timeout is None else gateway.monotonic() + timeout
    while True:
        store.raise_if_newer()
        with store.read() as tx:
            row = tx.one("SELECT status FROM jobs WHERE id = ?", [job_id])
        if row is None:
            raise _pruned(job_id)
        status = row["status"]
        remaining = None if deadline is None else deadline - gateway.monotonic()
        ended = status in _ENDS
        if ended or (remaining is not None and remaining <= 0):
            job = _snapshot(ctx, store, job_id)
            if job is None:
                raise _pruned(job_id)
            if job["status"] in _ENDS:  # re-checked in the snapshot's own transaction
                return WaitResult(wait_exit_code(job["status"]), job)
            if remaining is not None and remaining <= 0:
                return WaitResult(int(ExitCode.WAIT_TIMEOUT), job)
            continue  # the job left its end state between the two reads (a retry)
        step = POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining)
        gateway.sleep(step)


def _resolve(store: Store | None, text: str) -> str:
    if store is None:
        return str(find_job(None, text)["id"])
    with store.read() as tx:
        return str(find_job(tx, text)["id"])


def wait_for_job(ctx: Context, text: str, timeout: int | None) -> WaitResult:
    """Wait for the job `text` names (an id or prefix); a timeout (22) and an interrupt (130)
    are results, not errors."""
    store = _open(ctx)
    job_id: str | None = None
    try:
        with interrupts_as_keyboard_interrupt():
            try:
                job_id = _resolve(store, text)
                assert store is not None
                return _poll(ctx, store, job_id, timeout)
            except KeyboardInterrupt:
                return _interrupted(ctx, store, job_id)
    finally:
        if store is not None:
            store.close()


def _interrupted(ctx: Context, store: Store | None, job_id: str | None) -> WaitResult:
    """The job in its current status for exit 130. A further signal while it is read is
    ignored; when the job cannot be read the exit is still 130, with no job."""
    interrupt = JobsctlError("interrupted", "interrupted")
    if store is None or job_id is None:
        raise interrupt
    old = {
        s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        job = _snapshot(ctx, store, job_id)
    except Exception:
        raise interrupt from None
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)
    if job is None:
        raise interrupt
    return WaitResult(int(ExitCode.INTERRUPTED), job)


def print_summary(ctx: Context, job: dict[str, Any]) -> None:
    """Human output. TODO(#11): `show`'s human rendering replaces this."""
    detail = job["reason"] or job["hold_reason"]
    suffix = f" ({sanitize(str(detail), inline=True)})" if detail else ""
    sys.stdout.write(f"{sanitize(job['id'], inline=True)}: {job['status']}{suffix}\n")
    output = job["output"]
    if output is not None and output["tail"] and not ctx.quiet:
        sys.stdout.write(sanitize(output["tail"]).rstrip("\n") + "\n")
    sys.stdout.flush()


@command("wait")
def wait_command(ctx: Context) -> int:
    result = wait_for_job(ctx, ctx.args.id, ctx.args.timeout)
    if ctx.json:
        print_json(result.job)
    else:
        print_summary(ctx, result.job)
    return result.code
