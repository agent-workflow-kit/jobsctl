"""`run`: one pass, or passes in the foreground with `--loop` (interface.md, Pass and `run`)."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from types import FrameType

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.config import Config
from jobsctl.config_files import load_config
from jobsctl.diag import install
from jobsctl.env_file import disable_dumpable
from jobsctl.errors import JobsctlError
from jobsctl.runner import PassResult, Runner
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from jobsctl.textsafe import sanitize
from jobsctl.triggers import spawn_pass

BUSY_BACKOFF_SECONDS = 2.0
_SLEEP_SLICE = 0.2


def _print(ctx: Context, result: PassResult) -> None:
    if ctx.json:
        print_json(result.as_json())
        return
    if ctx.quiet:
        return
    if result.already_running:
        pid = result.holder.pid if result.holder is not None else None
        sys.stdout.write(f"already running (pid {pid})\n")
    for job_id in result.started:
        sys.stdout.write(f"started {sanitize(job_id, inline=True)}\n")
    for item in result.reconciled:
        sys.stdout.write(f"{item['job_id']}: {item['finding']} ({item['action']})\n")
    for warning in result.warnings:
        sys.stderr.write(f"jobsctl: warning: {sanitize(warning['message'], inline=True)}\n")
    sys.stdout.flush()
    sys.stderr.flush()


@command("run")
def run(ctx: Context) -> int:
    args = ctx.args
    disable_dumpable()
    retried: int | None = args.triggered
    if retried is not None:
        # diagnostics of a triggered pass: diag/run-<pid>.log, created on the first write
        install(resolve_state_dir() / "diag" / f"run-{os.getpid()}.log")
    store: Store | None = None
    runner: Runner | None = None
    try:
        store = Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI)
        runner = Runner(store, ctx.config, ctx.config_path)
        if args.loop:
            return _loop(ctx, runner)
        result = runner.run_pass(max_starts=args.max_starts)
        _print(ctx, result)
        return 0
    except JobsctlError as err:
        if err.code == "busy" and retried is not None and not (runner and runner.lease_taken):
            _respawn_after_busy(ctx, runner, store, retried)
        raise
    finally:
        if store is not None:
            store.close()


def _respawn_after_busy(
    ctx: Context, runner: Runner | None, store: Store | None, retried: int
) -> None:
    if retried != 0 or not ctx.config.auto_run:
        return
    gateway.sleep(BUSY_BACKOFF_SECONDS)
    try:
        if runner is not None:
            runner.respawn_pass(1)
        else:
            spawn_pass(resolve_state_dir(), ctx.config, ctx.config_path, retried=1)
    except OSError:
        return


def _loop(ctx: Context, runner: Runner) -> int:
    stop = False

    def on_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal stop
        stop = True

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    interval = ctx.args.interval if ctx.args.interval is not None else ctx.config.run_interval
    try:
        while not stop:
            # TODO(#27): re-exec after an upgrade (a newer schema or a different installed version)
            result = runner.run_pass(max_starts=ctx.args.max_starts)
            _print(ctx, result)
            runner.reap(PassResult())
            _wait(interval, lambda: stop)
            if not stop:
                runner.config = _reload(ctx, runner.config)
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)
    return 0


def _wait(seconds: float, stopped: Callable[[], bool]) -> None:
    remaining = float(seconds)
    while remaining > 0 and not stopped():
        gateway.sleep(min(_SLEEP_SLICE, remaining))
        remaining -= _SLEEP_SLICE


def _reload(ctx: Context, current: Config) -> Config:
    """Config is read at the start of each pass; a config that cannot be read keeps the last."""
    try:
        return load_config(ctx.config_path)
    except JobsctlError as err:
        sys.stderr.write(f"jobsctl: warning: config not reloaded ({err.message})\n")
        sys.stderr.flush()
        return current
