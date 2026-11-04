"""``jobsctl add [options] -- <argv...>`` (interface.md, add).

The flow: static checks (usage, secrets), then one `BEGIN IMMEDIATE` transaction that decides
between a unique-key hit, a refusal and a new job, then the log records, written from
`Tx.after_commit` only. `--dry-run` runs the same decisions in a read transaction and writes
nothing: no row, no queue, no counter, no log record.
"""

from __future__ import annotations

import json
import os
import posixpath
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jobsctl import gateway
from jobsctl.cli_output import print_json
from jobsctl.cli_registry import Context, command
from jobsctl.env_canonical import build_canonical_env
from jobsctl.env_file import EnvFile, load_env_file
from jobsctl.env_logging import log_env_file_state
from jobsctl.env_mask import MaskingSet, masking_values
from jobsctl.errors import JobsctlError, usage_error
from jobsctl.ids import generate_job_id
from jobsctl.job_view import job_object, job_row
from jobsctl.log_records import argv_sha256
from jobsctl.log_writer import RecordWriter
from jobsctl.positions import head_position, tail_position
from jobsctl.store_db import OpenMode, Store, Tx
from jobsctl.store_dir import resolve_state_dir
from jobsctl.store_ops import insert_job
from jobsctl.store_schema import NON_TERMINAL_STATUSES
from jobsctl.textsafe import sanitize
from jobsctl.timestamps import format_timestamp

MIN_SECRET_LENGTH = 8  # the masking set holds only values this long (env_mask)


@dataclass(frozen=True)
class Request:
    """What the command line asked for, validated and normalized."""

    argv: list[str]
    queue: str
    unique_key: str | None
    priority: int
    top: bool
    pin: bool
    cwd: str
    env: dict[str, str] = field(repr=False)
    pass_env: list[str]
    labels: list[tuple[str, str]]
    overrides: dict[str, Any]
    actor: str
    decision_ids: list[str]


@dataclass
class Outcome:
    job: dict[str, Any] | None
    created: bool = False
    queue_created: bool = False
    warnings: list[dict[str, Any]] = field(default_factory=list)
    log: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)


# -- request ---------------------------------------------------------------------------------


def caller_cwd() -> str:
    """The caller's working directory, symlinks kept: `$PWD` when it names the current
    directory (as a shell sets it), else the resolved path."""
    pwd = os.environ.get("PWD")
    try:
        if pwd and os.path.isabs(pwd) and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass  # a stale or unreadable $PWD: fall back to the resolved directory
    try:
        return os.getcwd()
    except OSError:
        raise usage_error("the current directory is not available; pass --cwd") from None


def normalize_cwd(text: str) -> str:
    """Absolute and lexically normalized (`.` and `..` resolved, symlinks kept); else exit 2."""
    if "\x00" in text or "\n" in text:
        raise usage_error("--cwd contains a NUL byte or a newline")
    if not text.startswith("/"):
        raise usage_error("--cwd must be an absolute path")
    path = "/" + posixpath.normpath(text).lstrip("/")  # a leading `//` is not special here
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise usage_error("--cwd is not valid UTF-8") from None
    return path


def _overrides(args: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if args.timeout is not None:
        found["timeout"] = args.timeout
    if args.max_retries is not None:
        found["max_retries"] = args.max_retries
    if args.kill_grace is not None:
        found["kill_grace"] = args.kill_grace
    return found


def build_request(ctx: Context) -> Request:
    args = ctx.args
    env: dict[str, str] = {}
    for name, value in args.env:
        if name in env:
            raise usage_error(f"--env {name} is given more than once")
        env[name] = value
    pass_env = list(dict.fromkeys(args.pass_env))  # a repeated name is the same name
    for name in pass_env:
        if name in env:
            raise usage_error(f"{name} is given with both --env and --pass-env")
    cwd = normalize_cwd(args.cwd if args.cwd is not None else caller_cwd())
    return Request(
        argv=list(args.argv),
        queue=args.queue if args.queue is not None else "default",
        unique_key=args.unique_key,
        priority=args.priority,
        top=args.top,
        pin=args.pin,
        cwd=cwd,
        env=env,
        pass_env=pass_env,
        labels=sorted(set(args.label)),
        overrides=_overrides(args),
        actor=ctx.actor,
        decision_ids=list(args.decision_id),
    )


# -- secrets ---------------------------------------------------------------------------------


def check_secrets(
    req: Request, masking: MaskingSet, config_mask_env: Sequence[str], env_file: EnvFile
) -> None:
    """Refuse (exit 2, naming an index or a variable, never a value) any argv element or `--env`
    value holding a masked value, and any `--env` name that is in `mask_env` or `env_file`."""
    for index, element in enumerate(req.argv):
        if masking.contains_secret(element):
            raise usage_error(
                f"argv element {index} contains a secret value; pass secrets in a file the "
                "command reads",
                code="secret_in_argv",
                index=index,
            )
    for name, value in req.env.items():
        if name in config_mask_env or name in env_file.values:
            raise usage_error(
                f"--env {name} is a secret variable; use --pass-env {name}",
                code="secret_in_env",
                variable=name,
            )
        if masking.contains_secret(value):
            raise usage_error(
                f"the value of --env {name} contains a secret value; use --pass-env",
                code="secret_in_env",
                variable=name,
            )


# -- the decision ----------------------------------------------------------------------------


def _holder(tx: Tx | None, key: str | None) -> sqlite3.Row | None:
    if tx is None or key is None:
        return None
    live = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
    return tx.one(
        f"SELECT * FROM jobs WHERE unique_key = ? AND status IN ({live})",
        [key, *NON_TERMINAL_STATUSES],
    )


def _queue_known(tx: Tx | None, ctx: Context, queue: str) -> bool:
    """Configured, or already used (a `queues_seen` row, or any job)."""
    if queue in ctx.config.queues:
        return True
    if tx is None:
        return False
    return (
        tx.one("SELECT 1 FROM queues_seen WHERE name = ?", [queue]) is not None
        or tx.one("SELECT 1 FROM jobs WHERE queue = ? LIMIT 1", [queue]) is not None
    )


def _job_values(req: Request, job_id: str, position: str, now: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "queue": req.queue,
        "argv": json.dumps(req.argv),
        "cwd": req.cwd,
        "env": json.dumps(req.env),
        "pass_env": json.dumps(req.pass_env),
        "unique_key": req.unique_key,
        "priority": req.priority,
        "pinned": int(req.pin),
        "position": position,
        "status": "queued",
        "overrides": json.dumps(req.overrides),
        "created_at": now,
        "enqueued_at": now,
        "status_changed_at": now,
        "actor": req.actor,
        "decision_ids": json.dumps(req.decision_ids),
    }


def _taken(tx: Tx, job_id: str) -> bool:
    """An id in `jobs` or in `pruned_ids` (ids are never reused)."""
    return (
        tx.one("SELECT 1 FROM jobs WHERE id = ?", [job_id]) is not None
        or tx.one("SELECT 1 FROM pruned_ids WHERE id = ?", [job_id]) is not None
    )


def _pass_env_warnings(req: Request, env_file: EnvFile) -> list[dict[str, Any]]:
    return [
        {
            "code": "pass_env_undefined",
            "message": f"{name} is not defined in env_file; the job fails missing_env at start",
            "variable": name,
        }
        for name in req.pass_env
        if name not in env_file.values
    ]


def _reuse(
    tx: Tx, ctx: Context, req: Request, held: sqlite3.Row, env_file: EnvFile, now: str
) -> Outcome:
    """A live job holds the key: create nothing, ignore the new settings."""
    job = job_object(
        tx, held, ctx.config, env_file_unavailable=env_file.status == "unavailable", now=now
    )
    out = Outcome(job=job)
    if held["status"] == "held":
        out.warnings.append(
            {
                "code": "existing_job_held",
                "message": f"the job holding this unique key is held ({held['id']})",
                "job_id": held["id"],
            }
        )
    digest = argv_sha256(req.argv)
    if json.loads(held["argv"]) != req.argv:
        out.log.append(
            (
                "unique_key_reused",
                {
                    "job_id": held["id"],
                    "queue": held["queue"],
                    "message": "an add with a different argv reused a live unique key",
                    "details": {"argv_sha256": digest, "unique_key": req.unique_key},
                },
                "info",
            )
        )
    return out


def decide(
    tx: Tx | None, ctx: Context, req: Request, env_file: EnvFile, now: str, *, write: bool
) -> Outcome:
    """Decide and, with `write`, store. `tx` is a write transaction (`write`), a read
    transaction (dry run) or `None` (dry run with no state directory yet: nothing exists)."""
    held = _holder(tx, req.unique_key)
    if held is not None:
        assert tx is not None
        return _reuse(tx, ctx, req, held, env_file, now)
    known = _queue_known(tx, ctx, req.queue)
    if ctx.config.strict_queues and not known:
        raise JobsctlError(
            "unknown_queue",
            f"queue {req.queue} is not configured and has no jobs (strict_queues is on)",
        )
    out = Outcome(job=None, warnings=_pass_env_warnings(req, env_file))
    if not write:
        return out
    assert tx is not None
    position = (
        head_position(tx, req.queue, pinned=req.pin, priority=req.priority)
        if req.top
        else tail_position(tx, req.queue)
    )
    job_id = generate_job_id(lambda candidate: _taken(tx, candidate))
    if not insert_job(tx, _job_values(req, job_id, position, now)):
        raced = _holder(tx, req.unique_key)  # the partial index decided: reuse that job
        if raced is None:
            raise JobsctlError("internal", "the job could not be stored")
        return _reuse(tx, ctx, req, raced, env_file, now)
    for key, value in req.labels:
        tx.execute(
            "INSERT INTO job_labels (job_id, key, value) VALUES (?, ?, ?)", [job_id, key, value]
        )
    if tx.one("SELECT 1 FROM queues_seen WHERE name = ?", [req.queue]) is None:
        tx.execute("INSERT INTO queues_seen (name, first_seen) VALUES (?, ?)", [req.queue, now])
    row = job_row(tx, job_id)
    assert row is not None
    out.created = True
    out.queue_created = not known
    out.job = job_object(
        tx, row, ctx.config, env_file_unavailable=env_file.status == "unavailable", now=now
    )
    if out.queue_created:
        out.log.append(
            (
                "queue_created",
                {
                    "job_id": job_id,
                    "queue": req.queue,
                    "message": f"queue {req.queue} was created on first use",
                    "details": {"cap": ctx.config.queue(req.queue).cap},
                },
                "warn",
            )
        )
    for warning in out.warnings:
        out.log.append(
            (
                "pass_env_undefined",
                {
                    "job_id": job_id,
                    "queue": req.queue,
                    "message": "a --pass-env variable is not defined in env_file",
                    "details": {"variable": warning["variable"]},
                },
                "warn",
            )
        )
    return out


# -- the command -----------------------------------------------------------------------------


def _dry_run(ctx: Context, req: Request, env_file: EnvFile, now: str) -> Outcome:
    """The same decisions with nothing written: no state directory or database is created."""
    db = resolve_state_dir() / "state.db"
    if not os.path.lexists(db):
        return decide(None, ctx, req, env_file, now, write=False)
    with Store.open(busy_timeout=ctx.config.busy_timeout) as store, store.read() as tx:
        return decide(tx, ctx, req, env_file, now, write=False)


def _write(ctx: Context, req: Request, env_file: EnvFile, masking: MaskingSet, now: str) -> Outcome:
    with Store.open(busy_timeout=ctx.config.busy_timeout, mode=OpenMode.CLI) as store:
        writer = RecordWriter(store, masking=masking)
        log_env_file_state(writer, ctx.config.env_file, env_file)
        with store.write() as tx:
            out = decide(tx, ctx, req, env_file, now, write=True)
            records = list(out.log)

            def log_all() -> None:
                for event, fields, level in records:
                    writer.log(event, level=level, fields={**fields, "actor": req.actor})

            tx.after_commit(log_all)
        return out


def _report(ctx: Context, out: Outcome, *, dry_run: bool) -> None:
    if ctx.json:
        print_json(
            {
                "created": out.created,
                "queue_created": out.queue_created,
                "dry_run": dry_run,
                "waited": False,
                "warnings": out.warnings,
                "job": out.job,
            }
        )
        return
    if not ctx.quiet:
        if out.queue_created:
            sys.stderr.write("jobsctl: warning: the queue was created on first use\n")
        for warning in out.warnings:
            sys.stderr.write(f"jobsctl: warning: {sanitize(warning['message'], inline=True)}\n")
        sys.stderr.flush()
    if out.job is not None:
        sys.stdout.write(out.job["id"] + "\n")
        sys.stdout.flush()


@command("add")
def add(ctx: Context) -> int:
    args = ctx.args
    if args.wait:
        # TODO(#17): `--wait` behaves as `wait` on the added job; refused until `wait` exists.
        raise JobsctlError("internal", "add --wait is not available yet")
    req = build_request(ctx)
    env_file = load_env_file(ctx.config.env_file)
    canonical = build_canonical_env(ctx.config, dict(os.environ))
    masking = masking_values(ctx.config, canonical, env_file)
    check_secrets(req, masking, ctx.config.mask_env, env_file)
    # TODO(#34): the pre-add hook runs here (1.x); TODO(#25): pre-start hooks run in the shim.
    now = format_timestamp(gateway.utc_now())
    if args.dry_run:
        out = _dry_run(ctx, req, env_file, now)
    else:
        out = _write(ctx, req, env_file, masking, now)
        # TODO(#14): request a pass unless --no-start (auto_run) once the runner exists.
    _report(ctx, out, dry_run=args.dry_run)
    return 0
