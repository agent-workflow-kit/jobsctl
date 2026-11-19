"""Live check of ``PIDFD_SIGNAL_PROCESS_GROUP`` on a real Linux 6.9+ kernel (#35). Never in CI:

    make live-pidfd
    JOBSCTL_LIVE_PIDFD=1 uv run pytest -m live tests/test_pidfd_group_live.py -v -rA

Local only and safe: it starts its own process groups (a leader and one ``sleep`` member, in a
group of their own) and signals only those, by the recorded identity (``tests/kill_guard.py``
refuses anything else, and the guard checks the flag cannot reach the runner's own group). HOME
and the XDG directories are private. No state directory, crontab or network is touched. Every
wait is bounded. Teardown kills exactly the recorded PIDs, only while their identity holds.

It skips, with the reason, when the kernel is older than 6.9 or does not accept the flag
(the probe sends signal 0 with the flag to a process this test started).

What it pins down, which no fake can: that the flag reaches the members; that it still reaches
them when the leader exited but is not reaped (the case ``_kill`` relies on); that it reports
``ESRCH`` once the leader was reaped, so nothing is signaled by a stale pidfd.
"""

from __future__ import annotations

import errno
import os
import signal
from collections.abc import Iterator

import pytest

from jobsctl import gateway_proc
from jobsctl.proc_host import default_host
from jobsctl.proc_signal import Result, signal_group
from tests.pidfd_group_helpers import Group, group_fixture_body, kernel_at_least

OPTED_IN = os.environ.get("JOBSCTL_LIVE_PIDFD") == "1"
FLAG = gateway_proc.PIDFD_SIGNAL_PROCESS_GROUP

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not OPTED_IN, reason="set JOBSCTL_LIVE_PIDFD=1 (make live-pidfd)"),
    pytest.mark.skipif(
        not kernel_at_least(6, 9), reason="PIDFD_SIGNAL_PROCESS_GROUP needs Linux 6.9 or later"
    ),
]


@pytest.fixture
def group() -> Iterator[Group]:
    yield from group_fixture_body()


@pytest.fixture(autouse=True)
def flag_supported(group: Group) -> None:
    """Skip unless the running kernel accepts the flag (a harmless signal-0 probe)."""
    if not gateway_proc.pidfd_supported():
        pytest.skip("no pidfd support here")
    fd = gateway_proc.pidfd_open(group.leader.pid)
    try:
        gateway_proc._raw_syscall(gateway_proc.SYS_PIDFD_SEND_SIGNAL, fd, 0, 0, FLAG)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOSYS):
            pytest.skip(f"the kernel does not accept PIDFD_SIGNAL_PROCESS_GROUP ({exc.strerror})")
        raise
    finally:
        os.close(fd)


def test_the_flag_reaches_every_member_and_the_leader(group: Group) -> None:
    out = signal_group(default_host(), group.leader, signal.SIGKILL, use_pidfd=True)
    assert out.sent and out.method == "pidfd-group"
    assert group.wait_gone(group.member)
    group.leader_proc.wait(timeout=30)


def test_the_flag_reaches_members_of_a_leader_that_exited_but_is_not_reaped(group: Group) -> None:
    os.kill(group.leader.pid, signal.SIGKILL)
    assert group.wait_zombie()  # the test holds the leader unreaped: its pidfd is still valid
    out = signal_group(
        default_host(),
        group.leader,
        signal.SIGKILL,
        use_pidfd=True,
        allow_zombie=True,
        fallback_allow_zombie=False,
    )
    assert out.sent and out.method == "pidfd-group", out
    assert group.wait_gone(group.member)


def test_a_reaped_leader_gets_nothing_and_the_member_is_left_alone(group: Group) -> None:
    os.kill(group.leader.pid, signal.SIGKILL)
    group.leader_proc.wait(timeout=30)  # reaped: its PID is free
    out = signal_group(
        default_host(),
        group.leader,
        signal.SIGKILL,
        use_pidfd=True,
        allow_zombie=True,
        fallback_allow_zombie=False,
    )
    assert out.result is Result.NOT_ALIVE
    assert group.alive(group.member)  # the group was not signaled through a stale reference


def test_the_flag_is_never_sent_for_a_wrong_identity(group: Group) -> None:
    from jobsctl.proc_identity import Identity

    wrong = Identity(group.leader.boot_id, group.leader.pid, group.leader.start + 1)
    out = signal_group(default_host(), wrong, signal.SIGKILL, use_pidfd=True)
    assert out.result is Result.NOT_ALIVE
    assert group.alive(group.leader) and group.alive(group.member)
