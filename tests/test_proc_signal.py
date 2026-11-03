"""Exact and group signals against a fake host: ordering, refusals, fallbacks."""

from __future__ import annotations

import errno
import signal

import pytest

from jobsctl import gateway_proc
from jobsctl.proc_identity import Identity
from jobsctl.proc_signal import Result, signal_group, signal_process

from .proc_fakes import FakeHost, oserror, proc

IDENT = Identity("boot-1", 5, 9)


def host_with(**kw) -> FakeHost:
    return FakeHost(procs={5: proc(5, start=9)}, **kw)


def names(host: FakeHost) -> list[str]:
    return [c[0] for c in host.calls]


def test_pidfd_path_opens_checks_then_signals_then_closes():
    host = host_with()
    out = signal_process(host, IDENT, signal.SIGTERM)
    assert out.sent and out.method == "pidfd"
    order = names(host)
    assert (
        order.index("pidfd_open") < order.index("process_info") < order.index("pidfd_send_signal")
    )
    assert order[-1] == "close"
    assert ("kill", 5, signal.SIGTERM) not in host.calls


def test_pid_reused_before_open_is_refused_and_nothing_sent():
    host = FakeHost(procs={5: proc(5, start=10)})
    out = signal_process(host, IDENT, signal.SIGKILL)
    assert out.result is Result.NOT_ALIVE and out.detail == "reused"
    assert host.sent() == []
    assert ("close", 5) in host.calls  # fd not leaked


def test_pid_reused_between_open_and_check_is_refused():
    host = host_with()

    def swap() -> None:
        host.procs[5] = proc(5, start=77)

    host.after_open = swap
    out = signal_process(host, IDENT, signal.SIGKILL)
    assert out.result is Result.NOT_ALIVE
    assert host.sent() == []


def test_pidfd_process_mismatch_or_exited_is_refused():
    for value in (-1, 6):
        host = host_with(pidfd_pid_value=value)
        out = signal_process(host, IDENT, signal.SIGTERM)
        assert out.result is Result.NOT_ALIVE and out.detail == "pidfd_process_mismatch"
        assert host.sent() == []


def test_zombie_not_signaled_unless_reserved_leader():
    host = FakeHost(procs={5: proc(5, start=9, zombie=True)})
    assert signal_process(host, IDENT, signal.SIGTERM).result is Result.NOT_ALIVE
    assert host.sent() == []
    assert signal_process(host, IDENT, signal.SIGTERM, allow_zombie=True).sent


def test_unknown_identity_is_not_signaled():
    host = FakeHost(info_errors={5: PermissionError(errno.EACCES, "x")})
    host.procs[5] = proc(5, start=9)
    out = signal_process(host, IDENT, signal.SIGKILL)
    assert out.result is Result.UNKNOWN
    assert host.sent() == []


def test_boot_changed_is_not_signaled():
    out = signal_process(host_with(boot="boot-2"), IDENT, signal.SIGTERM)
    assert out.result is Result.NOT_ALIVE and out.detail == "boot_changed"


def test_open_esrch_is_gone():
    host = FakeHost()
    assert signal_process(host, IDENT, signal.SIGTERM).result is Result.NOT_ALIVE


def test_send_esrch_is_gone():
    host = host_with(send_error_all=oserror(errno.ESRCH))
    assert signal_process(host, IDENT, signal.SIGTERM).result is Result.NOT_ALIVE


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EPERM])
def test_open_failure_falls_back_to_recheck_then_kill(code: int):
    host = host_with(pidfd_open_error=oserror(code))
    out = signal_process(host, IDENT, signal.SIGTERM)
    assert out.sent and out.method == "kill"
    order = names(host)
    assert order.index("process_info") < order.index("kill")


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EPERM])
def test_send_failure_falls_back_and_rechecks_right_before_kill(code: int):
    host = host_with(send_error_all=oserror(code))
    out = signal_process(host, IDENT, signal.SIGTERM)
    assert out.sent and out.method == "kill"
    order = names(host)
    # the last identity read precedes the kill with nothing in between
    assert order[-2:] == ["process_info", "kill"]


def test_fallback_recheck_catches_reuse_after_pidfd_failure():
    host = host_with(pidfd_open_error=oserror(errno.ENOSYS))
    host.procs[5] = proc(5, start=1234)
    out = signal_process(host, IDENT, signal.SIGKILL)
    assert out.result is Result.NOT_ALIVE and host.sent() == []


def test_no_pidfd_support_uses_kill_path():
    host = host_with(pidfd_ok=False)
    out = signal_process(host, IDENT, signal.SIGTERM)
    assert out.sent and out.method == "kill"
    assert "pidfd_open" not in names(host)


def test_kill_permission_denied():
    host = host_with(pidfd_ok=False, kill_error=PermissionError(errno.EPERM, "x"))
    assert signal_process(host, IDENT, signal.SIGTERM).result is Result.DENIED


def test_kill_esrch_is_gone():
    host = host_with(pidfd_ok=False, kill_error=ProcessLookupError(errno.ESRCH, "x"))
    assert signal_process(host, IDENT, signal.SIGTERM).result is Result.NOT_ALIVE


def test_other_open_error_signals_nothing():
    host = host_with(pidfd_open_error=oserror(errno.EMFILE))
    out = signal_process(host, IDENT, signal.SIGTERM)
    assert out.result is Result.ERROR and host.sent() == []


@pytest.mark.parametrize("sig", [0, -1, 65, 10**9, True])
def test_unsafe_signal_numbers_refused(sig: int):
    host = host_with()
    with pytest.raises(ValueError):
        signal_process(host, IDENT, sig)
    assert host.calls == []


# --- group signals -------------------------------------------------------------------------


def test_group_requires_leader_and_uses_killpg_with_recheck():
    host = host_with()
    out = signal_group(host, IDENT, signal.SIGTERM)
    assert out.sent and out.method == "killpg"
    assert names(host)[-2:] == ["process_info", "killpg"]
    assert host.calls[-1] == ("killpg", 5, signal.SIGTERM)


def test_group_signaled_by_leader_pid_after_leader_left_the_group():
    # leader 5 moved to group 2000; member 11 still is in group 5, numbered by the leader's PID
    host = FakeHost(procs={5: proc(5, start=9, pgid=2000), 11: proc(11, pgid=5)})
    out = signal_group(host, IDENT, signal.SIGKILL, use_pidfd=True)
    assert out.sent and out.method == "killpg"
    assert host.sent() == [("killpg", 5, signal.SIGKILL)]
    assert not any(c[0] == "pidfd_send_signal" for c in host.calls)


def test_group_leader_left_and_group_gone_is_not_alive():
    host = FakeHost(
        procs={5: proc(5, start=9, pgid=2000)}, killpg_error=ProcessLookupError(errno.ESRCH, "x")
    )
    out = signal_group(host, IDENT, signal.SIGKILL)
    assert out.result is Result.NOT_ALIVE and out.detail == "gone"


def test_group_leader_left_rechecks_identity_before_killpg():
    host = FakeHost(procs={5: proc(5, start=9, pgid=2000)})
    signal_group(host, IDENT, signal.SIGKILL)
    assert names(host)[-2:] == ["process_info", "killpg"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: gateway_proc.kill(-1, 15),
        lambda: gateway_proc.kill(0, 15),
        lambda: gateway_proc.kill(1, 15),
        lambda: gateway_proc.kill(5, 0),
        lambda: gateway_proc.kill(5, 65),
        lambda: gateway_proc.kill(True, 15),
        lambda: gateway_proc.killpg(0, 9),
        lambda: gateway_proc.killpg(-1, 9),
        lambda: gateway_proc.killpg(1, 9),
        lambda: gateway_proc.killpg(5, 0),
        lambda: gateway_proc.pidfd_send_signal(3, 0),
        lambda: gateway_proc.pidfd_send_signal(3, 65),
    ],
)
def test_gateway_sinks_refuse_unsafe_arguments(call, monkeypatch: pytest.MonkeyPatch):
    def fail(*a: object) -> None:
        pytest.fail("the OS was called")

    monkeypatch.setattr("os.kill", fail)
    monkeypatch.setattr("os.killpg", fail)
    monkeypatch.setattr("signal.pidfd_send_signal", fail, raising=False)
    monkeypatch.setattr(gateway_proc, "_raw_syscall", fail)
    with pytest.raises(ValueError):
        call()


@pytest.mark.parametrize("method", ["kill", "killpg"])
def test_fake_host_refuses_unsafe_arguments_like_the_gateway(method: str):
    host = host_with()
    with pytest.raises(ValueError):
        getattr(host, method)(0, 9)
    with pytest.raises(ValueError):
        getattr(host, method)(5, 0)


def test_fake_pidfd_signal_after_removal_is_esrch():
    host = host_with()
    fd = host.pidfd_open(5)
    del host.procs[5]
    with pytest.raises(OSError) as ei:
        host.pidfd_send_signal(fd, 15)
    assert ei.value.errno == errno.ESRCH


def test_group_held_zombie_leader_is_signaled():
    host = FakeHost(procs={5: proc(5, start=9, zombie=True)})
    assert signal_group(host, IDENT, signal.SIGTERM).sent


def test_group_leader_gone_signals_nothing():
    host = FakeHost()
    assert signal_group(host, IDENT, signal.SIGTERM).result is Result.NOT_ALIVE
    assert host.sent() == []


def test_group_leader_pid_reused_signals_nothing():
    host = FakeHost(procs={5: proc(5, start=2)})
    assert signal_group(host, IDENT, signal.SIGKILL).result is Result.NOT_ALIVE
    assert host.sent() == []


def test_group_pidfd_flag_path_then_einval_falls_back_to_killpg():
    flag = gateway_proc.PIDFD_SIGNAL_PROCESS_GROUP
    host = host_with(pidfd_send_error={flag: oserror(errno.EINVAL)})
    out = signal_group(host, IDENT, signal.SIGTERM, use_pidfd=True)
    assert out.sent and out.method == "killpg"
    assert ("pidfd_send_signal", 5, signal.SIGTERM, flag) in host.calls


def test_group_pidfd_flag_path_success():
    host = host_with()
    out = signal_group(host, IDENT, signal.SIGTERM, use_pidfd=True)
    assert out.sent and out.method == "pidfd-group"
    assert ("killpg", 5, signal.SIGTERM) not in host.calls


def test_group_never_signals_pgid_zero_or_one():
    for pid in (0, 1):
        with pytest.raises(ValueError):
            Identity("boot-1", pid, 1)


# --- the ctypes fallback allow list --------------------------------------------------------


@pytest.mark.parametrize(
    "machine", ["x86_64", "i386", "i686", "aarch64", "armv7l", "riscv64", "ppc64le", "s390x"]
)
def test_syscall_fallback_allowed_arches(machine: str):
    size = 8 if machine in {"x86_64", "aarch64", "riscv64", "ppc64le", "s390x"} else 4
    assert gateway_proc.syscall_fallback_allowed("Linux", machine, size)


@pytest.mark.parametrize(
    ("system", "machine", "size"),
    [
        ("Linux", "x86_64", 4),  # x32 / i386 on x86_64 kernel
        ("Linux", "mips64", 8),
        ("Linux", "alpha", 8),
        ("Linux", "", 8),
        ("Darwin", "arm64", 8),
        ("Darwin", "x86_64", 8),
    ],
)
def test_syscall_fallback_refused(system: str, machine: str, size: int):
    assert not gateway_proc.syscall_fallback_allowed(system, machine, size)


def test_ctypes_fallback_refused_off_allow_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delattr("os.pidfd_open", raising=False)
    monkeypatch.setattr(gateway_proc, "_fallback_allowed_here", lambda: False)
    with pytest.raises(OSError) as ei:
        gateway_proc.pidfd_open(12345)
    assert ei.value.errno == errno.ENOSYS
    with pytest.raises(OSError) as ei2:
        gateway_proc.pidfd_send_signal(3, signal.SIGTERM, 4)
    assert ei2.value.errno == errno.ENOSYS
    monkeypatch.delattr("signal.pidfd_send_signal", raising=False)
    assert gateway_proc.pidfd_supported() is False
