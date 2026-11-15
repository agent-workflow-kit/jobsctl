"""A worker process for the multi-process log tests (run as ``python -I log_worker.py``).

Protocol: print ``ready``, wait for the ``go`` file, do the work, print one JSON line.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from jobsctl import log_writer
from jobsctl.log_writer import RecordWriter
from jobsctl.store_db import Store


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def barrier(go: str) -> None:
    say(ready=True)
    deadline = time.monotonic() + 60
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.002)


def main(argv: list[str]) -> int:
    cmd, state, go, wid = argv[1:5]
    count = int(argv[5]) if len(argv) > 5 else 0
    store = Store.open(Path(state), busy_timeout=30)
    writer = RecordWriter(store)
    barrier(go)
    if cmd == "write":
        seqs = [
            writer.log("pass", fields={"message": f"{wid} line {i} " + "x" * (i % 500)})
            for i in range(count)
        ]
        say(seqs=seqs)
    elif cmd == "audit":
        seqs = [
            writer.audit(
                action="prune",
                actor="auto:jobsctl",
                target=None,
                inputs={"ids": ["jb-aaaaaaaa"], "older_than": 60, "trigger": "retention"},
                result="deleted",
            )
            for _ in range(count)
        ]
        say(seqs=seqs)
    elif cmd == "die_before_append":  # the counter is incremented, then the process dies

        def die(*_a: object, **_k: object) -> None:
            os._exit(9)

        log_writer.append_line = die  # type: ignore[assignment]
        writer.log("pass", fields={"message": "never written"})
    elif cmd == "die_mid_line":  # half a line reaches the file, then the process dies

        def half(fd: int, data: bytes, **_k: object) -> None:
            os.write(fd, data[: len(data) // 2])
            os._exit(9)

        log_writer.append_line = half  # type: ignore[assignment]
        writer.log("pass", fields={"message": "torn"})
    elif cmd == "hold_lock":  # holds the file lock until the go2 file appears
        from jobsctl.log_files import locked

        with locked(store.paths.root / "log.jsonl"):
            say(locked=True)
            while not os.path.exists(go + "2"):
                time.sleep(0.005)
        say(released=True)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
