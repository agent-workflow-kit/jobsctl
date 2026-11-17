"""Schema sets for the skew tests: the previous release's and one with an additive migration."""

from __future__ import annotations

from jobsctl.store_schema import MIGRATIONS

V2 = (
    *MIGRATIONS,
    (
        "ALTER TABLE jobs ADD COLUMN score INTEGER NOT NULL DEFAULT 7",
        "ALTER TABLE attempts ADD COLUMN note2 TEXT",
        "CREATE TABLE extra_v2 (k TEXT NOT NULL PRIMARY KEY, v TEXT)",
    ),
)
# The v0.31.2 release (before the hook-retry backoff, #72): schema version 1, no
# `hook_outbox.attempts`. Frozen: each new migration adds a fixture for its own predecessor
# (e.g. a `V2_RELEASE`) and keeps this one; it never replaces it.
V1_RELEASE = MIGRATIONS[:1]
SCHEMAS = {"old": MIGRATIONS, "new": V2, "v1_release": V1_RELEASE}
