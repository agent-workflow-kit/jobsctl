"""Finding jobs: an id or unique prefix, and the cancel selectors (interface.md, Identifiers,
Selectors)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from jobsctl.ids import resolve_job_id
from jobsctl.store_db import Tx
from jobsctl.store_schema import NON_TERMINAL_STATUSES


def find_job(tx: Tx | None, text: str) -> sqlite3.Row:
    """The job `text` (an id or unique prefix) names; exit 2, 10 or 21 as the interface says.

    `tx` is `None` when no database exists yet: nothing matches."""

    def find(prefix: str) -> list[str]:
        if tx is None:
            return []
        rows = tx.all(
            "SELECT id FROM jobs WHERE substr(id, 1, ?) = ?  ORDER BY id", [len(prefix), prefix]
        )
        return [str(r["id"]) for r in rows]

    job_id = resolve_job_id(text, find)
    assert tx is not None  # a match needs a database
    row = tx.one("SELECT * FROM jobs WHERE id = ?", [job_id])
    assert row is not None
    return row


def select_live_jobs(
    tx: Tx | None,
    *,
    queue: str | None,
    labels: Sequence[tuple[str, str]],
    unique_key: str | None,
) -> list[sqlite3.Row]:
    """The non-terminal jobs matching every selector (labels combine with AND), ordered by id."""
    if tx is None:
        return []
    marks = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
    where = [f"status IN ({marks})"]
    params: list[object] = [*NON_TERMINAL_STATUSES]
    if queue is not None:
        where.append("queue = ?")
        params.append(queue)
    if unique_key is not None:
        where.append("unique_key = ?")
        params.append(unique_key)
    for key, value in sorted(set(labels)):
        where.append("EXISTS (SELECT 1 FROM job_labels l WHERE l.job_id = jobs.id AND l.key = ? "
                     "AND l.value = ?)")  # fmt: skip
        params += [key, value]
    return tx.all(f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY id", params)
