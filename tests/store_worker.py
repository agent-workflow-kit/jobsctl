"""A worker process for the multi-process store tests (run as ``python -I store_worker.py``).

Protocol: print ``ready`` once set up, wait for the ``go`` file, do the work, print one JSON
line of results. Commands are in ``COMMANDS``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from jobsctl.errors import JobsctlError
from jobsctl.store_db import OpenMode, SchemaOlderError, Store
from jobsctl.store_ops import (
    counter_incr,
    insert_job,
    take_lease,
    transition_job,
)
from jobsctl.store_schema import MIGRATIONS

V2 = MIGRATIONS + (("ALTER TABLE jobs ADD COLUMN score INTEGER NOT NULL DEFAULT 7",),)
NOW = "2026-01-01T00:00:00.000Z"


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def barrier(go: str) -> None:
    say(ready=True)
    deadline = time.monotonic() + 60
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.002)


def job(job_id: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": job_id, "queue": "q", "argv": "[]", "cwd": "/", "position": "m",
        "status": "queued", "created_at": NOW, "enqueued_at": NOW,
        "status_changed_at": NOW, "actor": "x",
    }  # fmt: skip
    base.update(over)
    return base


def open_store(state: str, timeout: float, migrations: str = "v1") -> Store:
    return Store.open(
        Path(state),
        busy_timeout=timeout,
        migrations=V2 if migrations == "v2" else MIGRATIONS,
    )


def main(argv: list[str]) -> int:
    cmd, state, go, wid = argv[1:5]
    rest = argv[5:]
    try:
        return COMMANDS[cmd](state, go, wid, rest)
    except JobsctlError as exc:
        say(error=exc.code, exit=exc.exit_code)
        return 0


def cmd_open(state: str, go: str, wid: str, rest: list[str]) -> int:
    barrier(go)
    with open_store(state, 20.0, rest[0] if rest else "v1") as store:
        say(version=store.current_version())
    return 0


def cmd_cas(state: str, go: str, wid: str, rest: list[str]) -> int:
    with open_store(state, 20.0) as store:
        barrier(go)
        with store.write() as tx:
            won = transition_job(
                tx, "jb-aaaaaaaa", expect_status="queued", expect_attempt=0,
                to_status="starting", updates={"attempt": 1, "note": wid},
            )  # fmt: skip
        say(won=won)
    return 0


def cmd_insert_key(state: str, go: str, wid: str, rest: list[str]) -> int:
    with open_store(state, 20.0) as store:
        barrier(go)
        with store.write() as tx:
            created = insert_job(tx, job(f"jb-{wid}", unique_key=rest[0]))
        say(created=created)
    return 0


def cmd_counter(state: str, go: str, wid: str, rest: list[str]) -> int:
    count = int(rest[0])
    with open_store(state, 60.0) as store:
        barrier(go)
        values = []
        for _ in range(count):
            with store.write() as tx:
                values.append(counter_incr(tx, "seq"))
        say(values=values)
    return 0


def cmd_lease(state: str, go: str, wid: str, rest: list[str]) -> int:
    liveness = {"alive": True, "dead": False, "unknown": None}[rest[0]]
    with open_store(state, 20.0) as store:
        barrier(go)
        won = take_lease(
            store, "runner", wid, now=NOW, expires_at="2026-01-01T00:01:00.000Z",
            is_alive=lambda _h: liveness,
        )  # fmt: skip
        say(won=won)
    return 0


def cmd_hold(state: str, go: str, wid: str, rest: list[str]) -> int:
    """Hold the write lock with an uncommitted insert until killed."""
    with open_store(state, 5.0) as store:
        with store.write() as tx:
            tx.execute(
                "INSERT INTO queues_seen (name, first_seen) VALUES ('uncommitted', ?)", [NOW]
            )
            say(ready=True, in_tx=True)
            time.sleep(120)
    return 0


def cmd_stream(state: str, go: str, wid: str, rest: list[str]) -> int:
    """Commit (counter, row) pairs forever, until killed; report the first commit."""
    with open_store(state, 20.0) as store:
        say(ready=True)
        while True:
            with store.write() as tx:
                n = counter_incr(tx, "stream")
                tx.execute(
                    "INSERT INTO queues_seen (name, first_seen) VALUES (?, ?)", [f"q{n}", NOW]
                )
            if n == 1:  # once only, so an unread pipe never blocks the writer
                say(committed=n)


def cmd_internal_open(state: str, go: str, wid: str, rest: list[str]) -> int:
    try:
        with Store.open(Path(state), mode=OpenMode.INTERNAL, migrations=V2) as store:
            say(opened=True, version=store.current_version())
    except SchemaOlderError as exc:
        say(older=True, found=exc.found, known=exc.known)
    return 0


COMMANDS = {
    "open": cmd_open,
    "cas": cmd_cas,
    "insert-key": cmd_insert_key,
    "counter": cmd_counter,
    "lease": cmd_lease,
    "hold": cmd_hold,
    "stream": cmd_stream,
    "internal-open": cmd_internal_open,
}

if __name__ == "__main__":
    sys.exit(main(sys.argv))
