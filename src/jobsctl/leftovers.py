"""Leftovers: processes that outlive the job leader (docs/interface.md, Leftovers).

They are counted, never signaled. Two sources, both probed while the shim still holds the leader
unreaped (so its process group id is reserved):

* members of the job's process group, other than the leader;
* on Linux, processes reparented to the shim as child subreaper (found by their parent PID): a
  descendant that left the group (a daemon that called `setsid`) is adopted when its parent
  exits. Those that already exited are zombie children of the shim and are reaped by their exact
  PIDs; the others only count.

A scan that could not read every `/proc` entry (`hidepid`, a vanished entry) is incomplete: the
count is then a lower bound, and a count of 0 is not proof that nothing is left.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from jobsctl import gateway_proc
from jobsctl.proc_group import probe_group
from jobsctl.proc_host import Host


@dataclass(frozen=True)
class Leftovers:
    pids: tuple[int, ...]
    complete: bool

    @property
    def count(self) -> int:
        return len(self.pids)

    @property
    def recorded(self) -> int | None:
        """The attempt's `leftovers`: the count, or `None` for an empty but incomplete probe."""
        if self.pids or self.complete:
            return len(self.pids)
        return None


def count_leftovers(
    host: Host,
    *,
    leader_pid: int,
    pgid: int,
    own_pid: int,
    exclude: Iterable[int] = (),
    reap: Callable[[int], bool] = gateway_proc.reap_if_exited,
) -> Leftovers:
    """Count the live processes left behind by the job whose leader is `leader_pid` (group
    `pgid`); `own_pid` is the shim. `exclude`: PIDs that never count (the shim's hook leaders)."""
    skip = frozenset(exclude) | {leader_pid, own_pid}
    group = probe_group(host, pgid, exclude=skip)
    found = {m.pid for m in group.live}
    unreadable = group.unreadable
    children = host.scan_children(own_pid)
    unreadable += children.unreadable
    for info in children.members:
        if info.pid in skip:
            continue
        if info.zombie:
            reap(info.pid)  # an adopted orphan that already exited: collect it, do not count it
        else:
            found.add(info.pid)
    return Leftovers(tuple(sorted(found)), unreadable == 0)
