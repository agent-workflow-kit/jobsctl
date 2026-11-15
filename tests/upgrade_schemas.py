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
SCHEMAS = {"old": MIGRATIONS, "new": V2}
