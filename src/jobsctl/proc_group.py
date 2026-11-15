"""Process-group probing (docs/interface.md, Leftovers).

Linux scans ``/proc`` by process group; macOS asks libproc for the group's members only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .gateway_proc import validate_pid
from .proc_host import Host, ProcInfo

__all__ = ["GroupProbe", "probe_group"]


@dataclass(frozen=True)
class GroupProbe:
    live: tuple[ProcInfo, ...]
    unreadable: int

    @property
    def complete(self) -> bool:
        """False if some entries could not be read; a count of 0 is then not proof of empty."""
        return self.unreadable == 0

    @property
    def count(self) -> int:
        return len(self.live)


def probe_group(host: Host, pgid: int, *, exclude: Iterable[int] = ()) -> GroupProbe:
    """Members of group ``pgid`` that are still running.

    A member in state Z (or X) counts as dead, so a zombie whose parent is outside the group
    cannot keep a wait loop spinning. Pass the held leader's PID (and any other PID that must
    not count, e.g. hook leaders) in ``exclude``.
    """
    validate_pid(pgid)
    skip = frozenset(exclude)
    scan = host.scan_group(pgid)
    live = tuple(m for m in scan.members if not m.zombie and m.pid not in skip)
    return GroupProbe(live, scan.unreadable)
