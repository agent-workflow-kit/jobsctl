"""Internal commands: `_shim`, `_deliver` and `_kill` are not part of the stable interface."""

from __future__ import annotations

from jobsctl.cli_registry import Context, command
from jobsctl.diag import install
from jobsctl.env_file import disable_dumpable
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
