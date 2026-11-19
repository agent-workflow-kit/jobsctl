"""``signal_group`` with ``use_pidfd`` against real processes, on whatever kernel this is (#35).

On Linux before 6.9 the real kernel answers ``EINVAL`` to ``PIDFD_SIGNAL_PROCESS_GROUP``, so the
re-checked ``killpg`` fallback is exercised for real; on 6.9+ the same assertions hold with the
pidfd path. The strict 6.9+ checks are ``tests/test_pidfd_group_live.py``.
"""

from __future__ import annotations

import signal
import sys

import pytest

from jobsctl.proc_host import default_host
from jobsctl.proc_identity import Identity
from jobsctl.proc_signal import Result, signal_group
from tests.pidfd_group_helpers import Group, group, kernel_at_least  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfds and /proc")


def test_a_real_group_is_signaled_through_the_pidfd_or_the_killpg_fallback(group: Group) -> None:
    out = signal_group(default_host(), group.leader, signal.SIGKILL, use_pidfd=True)
    assert out.sent
    assert out.method == ("pidfd-group" if kernel_at_least(6, 9) else "killpg")
    assert group.wait_gone(group.member)
    group.leader_proc.wait(timeout=30)


def test_a_real_group_with_a_reused_leader_identity_is_not_signaled(group: Group) -> None:
    wrong = Identity(group.leader.boot_id, group.leader.pid, group.leader.start + 1)
    out = signal_group(default_host(), wrong, signal.SIGKILL, use_pidfd=True)
    assert out.result is Result.NOT_ALIVE
    assert group.alive(group.leader) and group.alive(group.member)


def test_a_real_group_is_not_signaled_without_use_pidfd_on_the_old_path_either(
    group: Group,
) -> None:
    wrong = Identity(group.leader.boot_id, group.leader.pid, group.leader.start + 1)
    assert signal_group(default_host(), wrong, signal.SIGKILL).result is Result.NOT_ALIVE
    assert group.alive(group.member)
