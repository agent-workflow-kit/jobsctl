"""`run`: one pass, or passes in the foreground with `--loop` (interface.md, Pass and `run`)."""

from __future__ import annotations

import signal
import sqlite3
import sys
from collections.abc import Callable
from types import FrameType

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.config import Config
from jobsctl.config_files import load_config
from jobsctl.env_file import disable_dumpable
from jobsctl.errors import JobsctlError
from jobsctl.runner import LeaseLost, PassResult, Runner
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from jobsctl.textsafe import sanitize
from jobsctl.triggers import spawn_pass
from jobsctl.upgrade import UpgradeWatch

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
    retried: int | None = args.triggered  # its diagnostics file is installed by the dispatcher
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

    watch = UpgradeWatch()
    old = {s: signal.signal(s, on_signal) for s in _STOP_SIGNALS}
    if watch.previous is not None:  # started by a re-exec of ours
        # the previous image may have left SIGTERM/SIGINT blocked with one pending: deliver it
        # now that the handlers are installed
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)
        _adopt(runner)
    interval = ctx.args.interval if ctx.args.interval is not None else ctx.config.run_interval
    try:
        while not stop:
            if not _reexec_if_upgraded(watch, runner, lambda: stop):
                break  # a stop arrived around the exec: no further pass
            try:
                result = runner.run_pass(max_starts=ctx.args.max_starts)
            except LeaseLost as err:
                _pass_failed(err.message)
            except JobsctlError as err:
                if err.code != "busy":
                    raise  # config_invalid and the like stay fatal
                _pass_failed(err.message)
            else:
                _print(ctx, result)
            runner.reap(PassResult())
            _wait(interval, lambda: stop)
            if not stop:
                runner.config = _reload(ctx, runner.config)
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)
    return 0


_STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def _adopt(runner: Runner) -> None:
    """After a re-exec the shims the previous image spawned are still this process's children."""
    try:
        runner.adopt_shims()
    except (JobsctlError, sqlite3.Error) as err:
        _warn(f"could not adopt the shims of the previous image ({err})")


def _reexec_if_upgraded(watch: UpgradeWatch, runner: Runner, stopped: Callable[[], bool]) -> bool:
    """Between passes (never inside one): replace this process after an upgrade (interface.md,
    Upgrades). Returns `True` to go on with the next pass (nothing to do, or the exec failed and
    the same reason is not tried again) and `False` when a stop was requested."""
    if stopped():
        return False
    try:
        reason = watch.check(runner.store)
    except (JobsctlError, sqlite3.Error):
        return True  # a busy database now: the next iteration checks again
    if reason is None:
        return True
    _warn(f"re-executing after an upgrade ({reason})")
    # a stop request must not be lost with this image: block the signals, and if one is already
    # pending (or was handled meanwhile) stay; a later one is delivered by the new image
    signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    try:
        if stopped() or _STOP_SIGNALS & signal.sigpending():
            return False
        try:
            watch.reexec(reason)
        except OSError as err:
            watch.failed(reason)
            _warn(f"re-exec failed ({err.strerror or err}); continuing with this image")
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)  # delivers a pending signal
    return not stopped()


def _warn(message: str) -> None:
    sys.stderr.write(f"jobsctl: warning: {message}\n")
    sys.stderr.flush()


def _pass_failed(message: str) -> None:
    """A pass of `--loop` that stopped on a busy database or a lost lease: the next one retries."""
    sys.stderr.write(f"jobsctl: warning: pass failed ({message}); retrying after the interval\n")
    sys.stderr.flush()


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
