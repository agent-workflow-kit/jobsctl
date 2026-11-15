"""``jobsctl label add`` and ``label remove`` (interface.md, label add / label remove).

One `BEGIN IMMEDIATE` transaction resolves the job, applies the change and builds the job
object; the `label` log record is appended after the commit. `--dry-run` runs the same
decisions in a read transaction on a read-only connection and writes nothing. Labels are
identifier fields the log builders do not mask, so a secret is refused here, before the
database is opened (`add` is to get the same refusal for its labels from issue #46); `label
remove` skips the check so that a label stored with a secret can still be removed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_canonical import build_canonical_env
from jobsctl.env_file import load_env_file
from jobsctl.env_mask import MaskingSet, masking_values
from jobsctl.errors import JobsctlError, usage_error
from jobsctl.job_view import job_labels, job_object
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.store_dir import resolve_state_dir
from jobsctl.timestamps import format_timestamp
from jobsctl.validate import MAX_LABELS
from jobsctl.view_lookup import job_by_id

Pair = tuple[str, str]


@dataclass
class Outcome:
    changed: list[str]  # the `key=value` pairs actually added or removed, sorted
    job: dict[str, Any]
    job_id: str
    queue: str


def check_label_secrets(pairs: list[Pair], masking: MaskingSet) -> None:
    """Exit 2 (`secret_in_label`) for a label holding a masked value. `details.label` names the
    key for a value hit and is left out for a key or a joined `key=value` hit: naming the key
    would echo the secret (or part of it)."""
    for key, value in pairs:
        if masking.contains_secret(key):
            raise usage_error("a label key contains a secret value", code="secret_in_label")
        if masking.contains_secret(value):
            raise usage_error(
                f"the value of label {key} contains a secret value",
                code="secret_in_label",
                label=key,
            )
        if masking.contains_secret(f"{key}={value}"):  # a secret spanning the `=`
            raise usage_error("a label contains a secret value", code="secret_in_label")


def _decide(tx: Tx, ctx: Context, *, write: bool) -> Outcome:
    adding = ctx.command == "label add"
    wanted = set(ctx.args.labels)
    row = job_by_id(tx, ctx.args.id)
    have = {tuple(label.split("=", 1)) for label in job_labels(tx, row["id"])}
    delta = wanted - have if adding else wanted & have
    after = have | delta if adding else have - delta
    if len(after) > MAX_LABELS:
        raise usage_error(f"a job has at most {MAX_LABELS} labels", limit=MAX_LABELS)
    if write:
        for key, value in sorted(delta):
            if adding:
                tx.execute(
                    "INSERT INTO job_labels (job_id, key, value) VALUES (?, ?, ?)",
                    [row["id"], key, value],
                )
            else:
                tx.execute(
                    "DELETE FROM job_labels WHERE job_id = ? AND key = ? AND value = ?",
                    [row["id"], key, value],
                )
    unavailable = False
    if row["pass_env"] != "[]":
        unavailable = load_env_file(ctx.config.env_file).status == "unavailable"
    job = job_object(
        tx,
        row,
        ctx.config,
        env_file_unavailable=unavailable,
        now=format_timestamp(gateway.utc_now()),
        labels=[f"{k}={v}" for k, v in after],
    )
    return Outcome([f"{k}={v}" for k, v in sorted(delta)], job, str(row["id"]), str(row["queue"]))


def _drop_new_sidecars(db: Path) -> None:
    """Let SQLite remove the `-wal`/`-shm` files that a read-only open created.

    A read-only connection cannot delete them on close. Opening and closing an ordinary
    connection with no wait does: SQLite deletes them only when that connection is the last
    one open, under its own locks, so a live writer keeps its files and is never disturbed
    (deleting the files by name would not be safe). It writes nothing to the database itself.
    """
    try:
        conn = sqlite3.connect(db, timeout=0)
        try:
            conn.execute("PRAGMA user_version").fetchone()  # connecting alone opens nothing
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # busy or unwritable: leaving the sidecars is harmless


def _dry_run(ctx: Context) -> Outcome:
    db = resolve_state_dir() / "state.db"
    if os.path.lexists(db):
        sidecars = [Path(f"{db}-wal"), Path(f"{db}-shm")]
        had_sidecars = any(os.path.lexists(p) for p in sidecars)
        try:
            with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.READ_ONLY) as store:
                if store.schema_version > 0:
                    with store.read() as tx:
                        return _decide(tx, ctx, write=False)
        finally:
            if not had_sidecars:
                _drop_new_sidecars(db)
    raise JobsctlError("not_found", f"no job matches '{ctx.args.id}'")


def _write(ctx: Context, masking: MaskingSet) -> Outcome:
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        writer = RecordWriter(store, masking=masking)
        with store.write() as tx:
            out = _decide(tx, ctx, write=True)
            if out.changed:

                def log() -> None:
                    try:
                        writer.log(
                            "label",
                            fields={
                                "job_id": out.job_id,
                                "queue": out.queue,
                                "actor": ctx.actor,
                                "decision_ids": list(ctx.args.decision_id),
                                "message": f"{ctx.command} {out.job_id}",
                                "details": {"op": ctx.command.split()[1], "labels": out.changed},
                            },
                        )
                    except Exception as exc:  # the change is committed: do not fail the command
                        sys.stderr.write(
                            f"jobsctl: warning: the change was stored but appending to "
                            f"log.jsonl failed ({type(exc).__name__})\n"
                        )

                tx.after_commit(log)
        return out


def _label(ctx: Context) -> int:
    env_file = load_env_file(ctx.config.env_file)
    canonical = build_canonical_env(ctx.config, dict(os.environ))
    masking = masking_values(ctx.config, canonical, env_file)
    if ctx.command == "label add":  # removing only deletes: a stored secret must stay removable
        check_label_secrets(ctx.args.labels, masking)
    out = _dry_run(ctx) if ctx.args.dry_run else _write(ctx, masking)
    if ctx.json:
        print_json(
            {"changed": bool(out.changed), "dry_run": bool(ctx.args.dry_run), "job": out.job}
        )
    elif not ctx.quiet:
        sys.stdout.write(f"{out.job_id}{'' if out.changed else ' (unchanged)'}\n")
    return 0


command("label add")(_label)
command("label remove")(_label)
