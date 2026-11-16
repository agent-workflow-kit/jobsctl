"""Job lookup for the read commands and `label` (ids, prefixes and unique keys).

TODO(#12): `job_lookup.find_job` on feat/hold-release-cancel does the id half of this. Whichever
of the two branches merges second consolidates them into one module.
"""

from __future__ import annotations

import sqlite3

from jobsctl.errors import JobsctlError
from jobsctl.ids import resolve_job_id
from jobsctl.store_db import Tx
from jobsctl.store_schema import NON_TERMINAL_STATUSES


def resolve_id(tx: Tx, text: str) -> str:
    """The full id `text` (an id or unique prefix) names: exit 2, 10 or 21 otherwise."""
    return resolve_job_id(
        text,
        lambda prefix: [
            str(r["id"])
            for r in tx.all("SELECT id FROM jobs WHERE substr(id, 1, ?) = ?", [len(prefix), prefix])
        ],
    )


def job_by_id(tx: Tx, text: str) -> sqlite3.Row:
    row = tx.one("SELECT * FROM jobs WHERE id = ?", [resolve_id(tx, text)])
    if row is None:  # deleted between the two reads: impossible inside one transaction
        raise JobsctlError("internal", "a resolved job disappeared")
    return row


def job_by_unique_key(tx: Tx, key: str) -> sqlite3.Row:
    """The non-terminal job holding `key`, else the most recently created job with it (ties by
    insertion order); exit 10 when no job has it."""
    live = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
    row = tx.one(
        f"SELECT * FROM jobs WHERE unique_key = ? AND status IN ({live})",
        [key, *NON_TERMINAL_STATUSES],
    ) or tx.one(
        "SELECT * FROM jobs WHERE unique_key = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        [key],
    )
    if row is None:
        raise JobsctlError("not_found", "no job has that unique key")
    return row
