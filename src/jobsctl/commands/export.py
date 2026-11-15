"""``jobsctl export (--audit | --log) [--since TIMESTAMP|DURATION] [--after-seq N]``."""

from __future__ import annotations

import datetime as dt
import os
import sys

from jobsctl import gateway
from jobsctl.cli_registry import Context, command
from jobsctl.errors import usage_error
from jobsctl.log_export import export_lines, since_cutoff
from jobsctl.log_writer import AUDIT_FILE, LOG_FILE
from jobsctl.store_dir import prepare_state_dir, resolve_state_dir


@command("export")
def export(ctx: Context) -> int:
    args = ctx.args
    since: dt.datetime | None = None
    if args.since is not None:
        kind, value = args.since
        if kind == "duration":
            try:
                since = since_cutoff(int(value), gateway.utc_now())
            except (OverflowError, ValueError):  # TODO(#4): TimestampError
                raise usage_error("--since reaches back past the supported dates") from None
        else:
            since = value  # type: ignore[assignment]
    root = resolve_state_dir()
    if not os.path.lexists(root):
        return 0  # nothing was ever written
    paths = prepare_state_dir(root, create=False)
    path = paths.root / (AUDIT_FILE if args.audit else LOG_FILE)
    try:
        for line in export_lines(path, since=since, after_seq=args.after_seq):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # quiet exit at flush
        return 0
    return 0
