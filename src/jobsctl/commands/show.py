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
from jobsctl.job_detail import job_detail, require_attempt
from jobsctl.job_lookup import find_job, job_by_unique_key
from jobsctl.job_text import show_text
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.timestamps import format_timestamp


def build_show(tx: Tx, ctx: Context) -> dict[str, Any]:
    args = ctx.args
    row = job_by_unique_key(tx, args.unique_key) if args.id is None else find_job(tx, args.id)
    if args.attempt is not None:
        require_attempt(tx, row["id"], args.attempt)
    unavailable = False
    if row["pass_env"] != "[]":
        unavailable = load_env_file(ctx.config.env_file).status == "unavailable"
    return job_detail(
        tx,
        row,
        ctx.config,
        env_file_unavailable=unavailable,
        now=format_timestamp(gateway.utc_now()),
        attempt=args.attempt,
        output="off" if args.no_output else "raise",
    )


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
