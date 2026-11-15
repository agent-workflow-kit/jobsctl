"""Internal commands: `_shim`, `_deliver` and `_kill` are not part of the stable interface."""

from __future__ import annotations

import os

from jobsctl.cli_registry import Context, command
from jobsctl.diag import install
from jobsctl.env_file import disable_dumpable
from jobsctl.kill import Killer
from jobsctl.kill import prepare_process as prepare_kill
from jobsctl.shim import Shim, StopSignal, prepare_process
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir

# TODO(#24): `_deliver` has no handler yet, so a spawned `_deliver` exits at once ("not implemented
# yet", in its diag file) and the next pass requests (and spawns) it again for as long as events
# stay undelivered. Interim churn only; the deliver issue registers the handler here.


@command("_shim")
def shim(ctx: Context) -> int:
    """The shim a pass spawns (`[python, -I, -m, jobsctl, _shim, id, claim_id]`): diagnostics in
    `shims/<id>.log`, then the start sequence (`jobsctl.shim`). A schema older than this jobsctl
    knows makes `Store.open` raise before anything is written (Upgrades)."""
    disable_dumpable()
    install(resolve_state_dir() / "shims" / f"{ctx.args.id}.log")
    stop = StopSignal()
    prepare_process(stop)  # Start sequence step 1, before anything else is done
    with Store.open(mode=OpenMode.INTERNAL, busy_timeout=ctx.config.busy_timeout) as store:
        running = Shim(
            store,
            ctx.config,
            ctx.config_path,
            ctx.args.id,
            ctx.args.claim_id,
            config_error=ctx.config_error,
            stop=stop,
        )
        return running.run()


@command("_kill")
def kill(ctx: Context) -> int:
    """The forced cancel of a job without a shim (`[python, -I, -m, jobsctl, _kill, id]`, started
    by `cancel --force` and reconcile): diagnostics in `diag/kill-<pid>.log`, then `jobsctl.kill`.
    It tolerates being started for a job that needs nothing (it then exits after its re-check)."""
    disable_dumpable()
    install(resolve_state_dir() / "diag" / f"kill-{os.getpid()}.log")
    stop = StopSignal()
    prepare_kill(stop)
    with Store.open(mode=OpenMode.INTERNAL, busy_timeout=ctx.config.busy_timeout) as store:
        return Killer(store, ctx.config, ctx.config_path, ctx.args.id, stop=stop).run()
