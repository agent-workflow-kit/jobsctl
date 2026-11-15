"""Hook selection: which configured hooks fire for a job (interface.md, Hooks).

`queues` (default `None`: every queue), `exclude_queues` and `labels` (pairs, AND) are evaluated
against the queue and labels the job has **at transition time**, which the caller passes in.
Matching is exact string equality; there are no globs or patterns.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .config import HOOK_NAMES, Config, PostHookEntry, PreHookEntry

__all__ = ["SelectedHook", "matches", "pre_start_event_id", "select_hooks"]

AnyEntry = PreHookEntry | PostHookEntry


@dataclass(frozen=True)
class SelectedHook:
    hook: str
    index: int  # position in the configured `hooks.<hook>` list, not among the selected ones
    entry: AnyEntry

    @property
    def hook_id(self) -> str:
        return self.entry.identity


def matches(entry: AnyEntry, queue: str, labels: Collection[str]) -> bool:
    """Whether `entry` fires for a job in `queue` carrying `labels` (`key=value` strings)."""
    if entry.queues is not None and queue not in entry.queues:
        return False
    if queue in entry.exclude_queues:
        return False
    have = set(labels)
    return all(label in have for label in entry.labels)


def select_hooks(
    config: Config, hook: str, queue: str, labels: Collection[str]
) -> list[SelectedHook]:
    """The entries of `hooks.<hook>` that fire, in configured order."""
    if hook not in HOOK_NAMES:
        raise KeyError(hook)
    return [
        SelectedHook(hook, index, entry)
        for index, entry in enumerate(config.hooks.entries(hook))
        if matches(entry, queue, labels)
    ]


def pre_start_event_id(claim_id: str, hook_index: int) -> str:
    """`pre-start` is not an outbox event: its `event_id` is `<claim_id>:pre-start:<index>`."""
    return f"{claim_id}:pre-start:{hook_index}"
