"""Internal commands: `_shim`, `_deliver` and `_kill` are not part of the stable interface."""

from __future__ import annotations

import os
import sys

from jobsctl.cli_registry import Context, command
from jobsctl.diag import install
from jobsctl.env_file import disable_dumpable
from jobsctl.errors import JobsctlError
from jobsctl.store_dir import resolve_state_dir


@command("_shim")
def shim(ctx: Context) -> int:
    """The shim entry point a pass spawns (`[python, -I, -m, jobsctl, _shim, id, claim_id]`).

    This is the spawn contract only: the process starts detached with diagnostics in
    `shims/<id>.log`, and then ends. The start sequence, the output pump, the exit mapping and the
    result file belong to the shim issue; until then a claimed job is requeued by the pass that
    reaps this process (`claim_orphaned`).
    """
    disable_dumpable()
    install(resolve_state_dir() / "shims" / f"{ctx.args.id}.log")
    # TODO(#15): confirm the identity, take the job lease, run the start sequence.
    sys.stderr.write(f"_shim {ctx.args.id}: not implemented (pid {os.getpid()})\n")
    raise JobsctlError("internal", "the shim is not implemented yet")
