"""A worker process for the runner tests (run as ``python -I runner_worker.py``).

Arguments: ``<command> <state dir> <go file> <worker id> <config file> <fake python> <hold>``.
Protocol: print ``ready``, wait for the ``go`` file, work, print one JSON line. ``pass`` runs a
pass (its ``hold`` seconds are spent inside the pass, holding the lease, after printing
``holding``); ``claim`` claims directly, without the runner lease, as a pass that lost it would;
``claim-hang`` claims and then waits to be killed (a crash between claim and spawn).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from jobsctl.claims import claim_jobs
from jobsctl.config_files import load_config
from jobsctl.gateway import utc_now
from jobsctl.leases import own_identity
from jobsctl.proc_host import default_host
from jobsctl.runner import PassResult, Runner
from jobsctl.store_db import Store
from jobsctl.timestamps import format_timestamp


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def barrier(go: str) -> None:
    say(ready=True)
    deadline = time.monotonic() + 60
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.002)


def cmd_pass(state: str, go: str, wid: str, config: str, python: str, hold: float) -> None:
    with Store.open(Path(state), busy_timeout=30.0) as store:
        runner = Runner(store, load_config(Path(config)), Path(config), python=python)

        def probe(step: str) -> None:
            if step == "reconcile" and hold:
                say(holding=True)
                time.sleep(hold)

        runner.probe = probe
        barrier(go)
        result: PassResult = runner.run_pass()
        say(pid=os.getpid(), **result.as_json())
        if os.environ.get("RUNNER_WORKER_STAY"):  # stay alive (a hangup is sent to the group)
            time.sleep(120)


def cmd_claim(state: str, go: str, wid: str, config: str, python: str, hold: float) -> None:
    with Store.open(Path(state), busy_timeout=30.0) as store:
        conf = load_config(Path(config))
        barrier(go)
        mine: list[str] = []
        while True:
            with store.write() as tx:
                claims, _ = claim_jobs(
                    tx, conf, now=format_timestamp(utc_now()), claimed_by=f"worker:{wid}",
                    env_file_unavailable=lambda: False, limit=1,
                )  # fmt: skip
            if not claims:
                break
            mine.append(claims[0].job_id)
        say(claimed=mine)


def cmd_claim_hang(state: str, go: str, wid: str, config: str, python: str, hold: float) -> None:
    with Store.open(Path(state), busy_timeout=30.0) as store:
        conf = load_config(Path(config))
        with store.write() as tx:
            claims, _ = claim_jobs(
                tx, conf, now=format_timestamp(utc_now()),
                claimed_by=own_identity(default_host()),
                env_file_unavailable=lambda: False, limit=1,
            )  # fmt: skip
        say(claimed=[c.job_id for c in claims], claim_id=claims[0].claim_id, ready=True)
        time.sleep(120)


COMMANDS = {"pass": cmd_pass, "claim": cmd_claim, "claim-hang": cmd_claim_hang}

if __name__ == "__main__":
    name, state, go, wid, config, python, hold = sys.argv[1:8]
    COMMANDS[name](state, go, wid, config, python, float(hold))
