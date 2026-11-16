"""``jobsctl show`` (interface.md, show).

Selection, attempt checks and every read happen in one read transaction. The output tail is
untrusted: it is sanitized again here, in `--json` as well as in human output.
"""

from __future__ import annotations

import sys
from typing import Any

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_file import load_env_file
from jobsctl.errors import JobsctlError
from jobsctl.job_text import show_text
from jobsctl.job_view import attempt_object, current_attempt, job_object
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.textsafe import sanitize
from jobsctl.timestamps import format_timestamp
from jobsctl.view_lookup import job_by_id, job_by_unique_key


def decode_tail(blob: bytes, compressed: int) -> str:
    """The stored tail as text; an undecodable byte becomes `\\xNN` (interface.md, step 2)."""
    if compressed:
        # TODO(#16): output capture owns the compression format; read it here once it exists.
        raise JobsctlError("internal", "a compressed output tail cannot be read yet")
    return bytes(blob).decode("utf-8", "backslashreplace")


def _output(tx: Tx, job_id: str, n: int) -> dict[str, Any] | None:
    row = tx.one("SELECT * FROM outputs WHERE job_id = ? AND n = ?", [job_id, n])
    if row is None:
        return None
    return {
        "tail": sanitize(decode_tail(row["tail"], row["compressed"])),
        "bytes_seen": row["bytes_seen"],
        "truncated": bool(row["truncated"]),
        "updated_at": row["updated_at"],
    }


def build_show(tx: Tx, ctx: Context) -> dict[str, Any]:
    args = ctx.args
    row = job_by_unique_key(tx, args.unique_key) if args.id is None else job_by_id(tx, args.id)
    attempts = tx.all("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", [row["id"]])
    current = current_attempt(tx, row)
    wanted: int | None = int(current["n"]) if current is not None else None
    if args.attempt is not None:
        if all(a["n"] != args.attempt for a in attempts):
            raise JobsctlError("not_found", f"job {row['id']} has no attempt {args.attempt}")
        wanted = int(args.attempt)
    unavailable = False
    if row["pass_env"] != "[]":
        unavailable = load_env_file(ctx.config.env_file).status == "unavailable"
    now = format_timestamp(gateway.utc_now())
    job = job_object(tx, row, ctx.config, env_file_unavailable=unavailable, now=now)
    job["attempts"] = [attempt_object(a) for a in attempts]
    job["output"] = None
    if wanted is not None and not args.no_output:
        job["output"] = _output(tx, row["id"], wanted)
    return job


@command("show")
def show(ctx: Context) -> int:
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        with store.read() as tx:
            job = build_show(tx, ctx)
    if ctx.json:
        print_json(job)
    else:
        sys.stdout.write(show_text(job))
        sys.stdout.flush()
    return 0
