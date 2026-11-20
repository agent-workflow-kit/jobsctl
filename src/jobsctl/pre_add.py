"""The `pre-add` hooks of `add` (docs/interface.md, Hooks and `add`).

:func:`run_pre_add` runs the selected hooks in configured order after every other check of `add`
and before anything is written, with jobsctl's state directory as cwd and the canonical
environment of the `add` process (the rules of every hook). Fail-closed, like `pre-start`:

- a vetoing result (an exit other than 0 and 13, a timeout, a kill by signal, a hook that cannot
  be started) stops the loop and refuses the add with `vetoed` (exit 11); a veto beats any
  escalation;
- exit 13 escalates and the rest still run; without a veto the add is refused with
  `needs_human` (exit 13). Nothing is stored either way: there is no held state for a job that
  does not exist, and no override at add (D54);
- a hook stopped by SIGINT, SIGHUP or SIGTERM, or whose leader outlived SIGKILL, ends the add
  with `interrupted` (130, SIGINT) or `internal` (1), storing nothing.

Every run is logged as a `hook_ran` record whose `job_id` is the id the job would get. Hook
output is read in memory and never stored; `decision_id`s are logged in `hook_ran` only.
"""

from __future__ import annotations

import signal
from typing import Any

from jobsctl.config import Config
from jobsctl.delivery import Deliverer, EventPlan
from jobsctl.errors import JobsctlError
from jobsctl.hook_interrupt import InterruptGuard
from jobsctl.hook_output import MESSAGE_LIMIT, cut_bytes
from jobsctl.hook_payload import build_add_payload, encode_payload
from jobsctl.hook_run import HookResult
from jobsctl.hook_select import SelectedHook, pre_add_event_id, select_hooks
from jobsctl.store_db import Store
from jobsctl.textsafe import sanitize

__all__ = ["HOOK", "run_pre_add", "selected_pre_add"]

HOOK = "pre-add"
IDENTITY_LIMIT = 256  # an unnamed hook's identity is its whole cmd: bound what an error shows


def selected_pre_add(config: Config, queue: str, labels: list[str]) -> list[SelectedHook]:
    """The `hooks.pre-add` entries that fire for an add into `queue` with `labels` (`key=value`)."""
    return select_hooks(config, HOOK, queue, labels)


def _text(deliverer: Deliverer, text: str) -> str:
    return sanitize(deliverer.env.masking.mask(text), inline=True)


def _identity(deliverer: Deliverer, hook_id: str) -> str:
    return cut_bytes(_text(deliverer, hook_id), IDENTITY_LIMIT)


def _interrupted(signum: int | None, stopped: str | None) -> JobsctlError:
    name = signal.Signals(signum).name if signum else str(stopped)
    code = "interrupted" if signum == signal.SIGINT else "internal"
    return JobsctlError(
        code, f"interrupted by {name} while running a pre-add hook; nothing was added"
    )


def _reason(deliverer: Deliverer, result: HookResult) -> str:
    """The hook's stderr (already masked and sanitized by `run_hook`), on one line."""
    return cut_bytes(_text(deliverer, result.message or ""), MESSAGE_LIMIT)


def run_pre_add(
    store: Store,
    config: Config,
    selected: list[SelectedHook],
    *,
    job_id: str,
    payload_fields: dict[str, Any],
) -> None:
    """Run `selected` for the add that would create `job_id`; return when every hook allowed.

    Raises :class:`JobsctlError`: `vetoed`, `needs_human`, `interrupted` or `internal` (see the
    module doc). `payload_fields` are the `build_add_payload` arguments other than the event
    id and the job id.
    """
    deliverer = Deliverer(store, config)
    escalated: list[HookResult] = []
    with InterruptGuard() as guard:
        for chosen in selected:
            event_id = pre_add_event_id(job_id, chosen.index)
            payload = build_add_payload(event_id=event_id, job_id=job_id, **payload_fields)
            result = deliverer.runner(
                hook=HOOK,
                hook_id=chosen.hook_id,
                cmd=chosen.entry.cmd,
                payload=encode_payload(payload),
                timeout=float(chosen.entry.timeout),
                env=deliverer.env.for_hook(HOOK, job_id),
                cwd=deliverer.cwd,
                mask=deliverer.env.masking.mask,
                stop=guard,
                host=deliverer.host,
                tuning=deliverer.tuning,
            )
            deliverer.record(EventPlan(event_id, job_id, (), queue=payload_fields["queue"]), result)
            if result.unkillable:
                raise JobsctlError("internal", "a pre-add hook outlived SIGKILL; nothing was added")
            if result.stopped is not None or guard.signum is not None:
                raise _interrupted(guard.signum, result.stopped)
            if result.vetoes:
                reason = _reason(deliverer, result)
                shown = _identity(deliverer, result.hook_id)
                raise JobsctlError(
                    "vetoed",
                    f"the pre-add hook {shown} vetoed the add" + (f": {reason}" if reason else ""),
                    details={"hook_id": shown},
                )
            if result.escalates:
                escalated.append(result)
    # After the guard is released, a signal it recorded (one that landed after the last hook's
    # check, up to the restore of the handlers) must not be lost: it ends the add here, before
    # anything is stored. A signal after the restore takes the default disposition, before the
    # store's commit, so nothing is stored either way.
    if guard.signum is not None:
        raise _interrupted(guard.signum, None)
    if escalated:
        ids = [_identity(deliverer, r.hook_id) for r in escalated]
        reasons = [r for r in (_reason(deliverer, x) for x in escalated) if r]
        raise JobsctlError(
            "needs_human",
            f"the pre-add hook {', '.join(ids)} escalated the add to a human; nothing was added"
            + (f": {cut_bytes('; '.join(reasons), MESSAGE_LIMIT)}" if reasons else ""),
            details={"hook_ids": ids},
        )
