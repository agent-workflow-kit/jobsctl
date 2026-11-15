"""A worker for the hold/release/cancel race tests (run as ``python -I manual_worker.py``).

Protocol: print ``ready``, wait for the ``go`` file, do the work, print one JSON line.
``cli <go> <argv...>`` runs the real command; ``claim <go> <job id>`` is what a runner pass does
to a queued job: one compare-and-set to ``starting`` with a new attempt, in one transaction.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time

from jobsctl.cli import main as cli_main
from jobsctl.store_db import Store
from jobsctl.store_ops import transition_job

NOW = "2026-01-01T00:00:00.000Z"


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def barrier(go: str) -> None:
    say(ready=True)
    deadline = time.monotonic() + 60
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.001)


def claim(job_id: str) -> None:
    with Store.open() as store, store.write() as tx:
        row = tx.one("SELECT status, attempt FROM jobs WHERE id = ?", [job_id])
        assert row is not None
        if row["status"] != "queued":
            say(claimed=False, status=row["status"])
            return
        won = transition_job(
            tx,
            job_id,
            expect_status="queued",
            expect_attempt=row["attempt"],
            to_status="starting",
            updates={"attempt": row["attempt"] + 1},
        )
        tx.execute(
            "INSERT INTO attempts (job_id, n, claim_id, status, claimed_at, claimed_by, "
            "jobsctl_version) VALUES (?, ?, 'claim', 'starting', ?, 'pass:1', '0')",
            [job_id, row["attempt"] + 1, NOW],
        )
    say(claimed=won)


def main() -> int:
    kind, go, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
    barrier(go)
    if kind == "claim":
        claim(rest[0])
        return 0
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli_main(rest)
    say(exit=int(code), out=out.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
