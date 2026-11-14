"""The state database schema: forward-only, additive-only migrations (interface.md, Upgrades).

``MIGRATIONS[i]`` holds the statements that take ``PRAGMA user_version`` from ``i`` to ``i + 1``,
one statement per string. Only additive statements are allowed: new tables, new indexes and new
columns that are nullable or carry a default, so code of the previous version keeps working
against a migrated database. A change that cannot be made additively ships only in a new major
version (interface.md, Upgrades).
"""

from __future__ import annotations

import re

NON_TERMINAL_STATUSES = ("queued", "starting", "running", "held")
TERMINAL_STATUSES = ("done", "failed", "canceled")
ALL_STATUSES = NON_TERMINAL_STATUSES + TERMINAL_STATUSES

_LIVE_STATUSES_SQL = ", ".join(f"'{s}'" for s in NON_TERMINAL_STATUSES)
_ALL_STATUSES_SQL = ", ".join(f"'{s}'" for s in ALL_STATUSES)

# Version 1: every table of design.md "Data model". JSON columns hold JSON text; timestamps are
# UTC text with millisecond precision (TODO(#4): written through the timestamp grammar).
_V1: tuple[str, ...] = (
    f"""CREATE TABLE jobs (
        id TEXT NOT NULL PRIMARY KEY,
        queue TEXT NOT NULL,
        argv TEXT NOT NULL,
        cwd TEXT NOT NULL,
        env TEXT NOT NULL DEFAULT '{{}}',
        pass_env TEXT NOT NULL DEFAULT '[]',
        unique_key TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        position TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ({_ALL_STATUSES_SQL})),
        reason TEXT,
        hold_reason TEXT,
        shim_lost INTEGER NOT NULL DEFAULT 0 CHECK (shim_lost IN (0, 1)),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        overrides TEXT NOT NULL DEFAULT '{{}}',
        not_before TEXT,
        auto_retries INTEGER NOT NULL DEFAULT 0,
        spawn_failures INTEGER NOT NULL DEFAULT 0,
        override_hooks TEXT NOT NULL DEFAULT '[]',
        retried_from_attempt INTEGER,
        created_at TEXT NOT NULL,
        enqueued_at TEXT NOT NULL,
        status_changed_at TEXT NOT NULL,
        ended_at TEXT,
        resolved_by TEXT,
        note TEXT,
        actor TEXT NOT NULL,
        decision_ids TEXT NOT NULL DEFAULT '[]'
    )""",
    f"""CREATE UNIQUE INDEX jobs_unique_key_live ON jobs (unique_key)
        WHERE unique_key IS NOT NULL AND status IN ({_LIVE_STATUSES_SQL})""",
    "CREATE INDEX jobs_order ON jobs (queue, status, pinned, priority, position)",
    """CREATE TABLE job_labels (
        job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (job_id, key, value)
    )""",
    """CREATE TABLE attempts (
        job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        n INTEGER NOT NULL CHECK (n >= 1),
        claim_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('starting', 'running', 'held', 'done', 'failed',
            'canceled')),
        reason TEXT,
        hold_reason TEXT,
        claimed_at TEXT NOT NULL,
        claimed_by TEXT NOT NULL,
        shim_identity TEXT,
        shim_confirmed_at TEXT,
        effective TEXT,
        running_at TEXT,
        ended_at TEXT,
        "exit" TEXT,
        leftovers INTEGER,
        job_identity TEXT,
        pgid INTEGER,
        resolved_path TEXT,
        escalated_hooks TEXT NOT NULL DEFAULT '[]',
        vetoed_by TEXT,
        hook_messages TEXT NOT NULL DEFAULT '[]',
        decision_ids TEXT NOT NULL DEFAULT '[]',
        force_requested_by TEXT,
        resolved_by TEXT,
        note TEXT,
        jobsctl_version TEXT NOT NULL,
        PRIMARY KEY (job_id, n)
    )""",
    """CREATE TABLE outputs (
        job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        n INTEGER NOT NULL,
        tail BLOB NOT NULL,
        compressed INTEGER NOT NULL DEFAULT 0 CHECK (compressed IN (0, 1)),
        bytes_seen INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, n)
    )""",
    # `seq` is the outbox order (delivery is in order per job); `event_id` is the payload's id.
    """CREATE TABLE hook_outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        hook TEXT NOT NULL,
        payload TEXT NOT NULL,
        async INTEGER NOT NULL DEFAULT 0 CHECK (async IN (0, 1)),
        created_at TEXT NOT NULL,
        delivered_at TEXT
    )""",
    "CREATE INDEX hook_outbox_undelivered ON hook_outbox (job_id, seq) WHERE delivered_at IS NULL",
    """CREATE TABLE leases (
        name TEXT NOT NULL PRIMARY KEY,
        holder_identity TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    """CREATE TABLE counters (
        name TEXT NOT NULL PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE queues_seen (
        name TEXT NOT NULL PRIMARY KEY,
        first_seen TEXT NOT NULL
    )""",
    "CREATE TABLE pruned_ids (id TEXT NOT NULL PRIMARY KEY)",
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1,)
SCHEMA_VERSION = len(MIGRATIONS)

_CREATE_RE = re.compile(r"^CREATE (UNIQUE )?(TABLE|INDEX) (IF NOT EXISTS )?\"?\w+\"?\b", re.I)
_ADD_COLUMN_RE = re.compile(r"^ALTER TABLE \"?\w+\"? ADD COLUMN \"?\w+\"? ", re.I)


def check_additive(statement: str) -> None:
    """Raise ValueError unless ``statement`` is one additive statement.

    Allowed: ``CREATE TABLE``, ``CREATE [UNIQUE] INDEX`` and ``ALTER TABLE ... ADD COLUMN`` whose
    column is nullable or has a default. Everything else (drop, rename, update, a second
    statement) is refused.
    """
    text = " ".join(statement.split())
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if ";" in text:
        raise ValueError("a migration step is exactly one statement")
    if _CREATE_RE.match(text):
        return
    if _ADD_COLUMN_RE.match(text):
        upper = text.upper()
        if "NOT NULL" in upper and "DEFAULT" not in upper:
            raise ValueError("an added NOT NULL column needs a default")
        if "PRIMARY KEY" in upper or " UNIQUE" in upper:
            raise ValueError("an added column cannot be a key")
        return
    raise ValueError(f"migrations are additive only; refused: {text[:60]!r}")
