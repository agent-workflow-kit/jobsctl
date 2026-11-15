"""Process identity: /proc parsing, boot id, liveness (Linux fixtures and macOS fakes)."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path

import pytest

from jobsctl.proc_group import probe_group
from jobsctl.proc_host import (
    BootIdUnavailable,
    DarwinHost,
    LinuxHost,
    ProcParseError,
    parse_proc_stat,
)
from jobsctl.proc_identity import (
    Identity,
    Liveness,
    ProcessGone,
    check_identity,
    identify,
)

from .proc_fakes import FakeHost, proc


def stat_line(
    pid: int, comm: bytes, state: bytes = b"S", start: int = 4242, pgrp: int = 7, threads: int = 1
) -> bytes:
    # fields 4.. after the state: ppid pgrp session tty tpgid flags minflt cminflt majflt
    # cmajflt utime stime cutime cstime priority nice threads itrealvalue starttime ...
    rest = [state, b"1", str(pgrp).encode(), b"7", b"0", b"-1", b"4194304"]
    rest += [b"0"] * 4 + [b"0"] * 4 + [b"20", b"0", str(threads).encode(), b"0"]
    rest += [str(start).encode(), b"1", b"2"]
    return str(pid).encode() + b" (" + comm + b") " + b" ".join(rest) + b"\n"


# --- parsing -------------------------------------------------------------------------------


def test_parse_plain():
    info = parse_proc_stat(stat_line(10, b"sleep", start=99, pgrp=10), 10)
    assert (info.pid, info.start, info.pgid, info.ppid, info.state, info.zombie) == (
        10,
        99,
        10,
        1,
        "S",
        False,
    )


@pytest.mark.parametrize(
    "comm",
    [b") (", b"a) (b c", b"x)", b"(", b"a b\nc) 1 2 3 (", b"\xff\xfe) (", b"", b") S 1 1 1 1 ("],
)
def test_parse_hostile_comm_uses_last_paren(comm: bytes):
    info = parse_proc_stat(stat_line(10, comm, start=555, pgrp=3), 10)
    assert info.start == 555
    assert info.pgid == 3
    assert info.state == "S"


def test_parse_zombie_state():
    info = parse_proc_stat(stat_line(10, b"z", state=b"Z"), 10)
    assert info.zombie and info.state == "Z"


def test_parse_zombie_state_with_live_threads_is_alive():
    info = parse_proc_stat(stat_line(10, b"z", state=b"Z", threads=2), 10)
    assert info.state == "Z" and not info.zombie


def test_parse_zombie_state_single_thread_is_zombie():
    assert parse_proc_stat(stat_line(10, b"z", state=b"Z", threads=1), 10).zombie


def test_parse_dead_state_x_is_dead_whatever_the_threads():
    assert parse_proc_stat(stat_line(10, b"z", state=b"X", threads=3), 10).zombie


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n",
        b"10 sleep S 1 2",  # no parens
        b"10 (sleep) S 1 2 3",  # too few fields
        b"11 (sleep) " + b"S 1 7 7 0 -1 0 " + b"0 " * 20,  # pid mismatch
        b"10 (sleep) SS 1 7 7 0 -1 0 " + b"0 " * 20,  # bad state
        b"10 (sleep) S x 7 7 0 -1 0 " + b"0 " * 20,  # non-numeric ppid
        stat_line(10, b"sleep", start=-5),  # negative start
        b"(sleep) S 1 7 7",  # no pid
        stat_line(10, b"sleep").replace(b" 20 0 1 0 ", b" 20 0 x 0 "),  # non-numeric threads
    ],
)
def test_parse_malformed_is_an_error_not_a_guess(raw: bytes):
    with pytest.raises(ProcParseError):
        parse_proc_stat(raw, 10)


def test_parse_rejects_oversized_start():
    raw = stat_line(10, b"x", start=10**30)
    with pytest.raises(ProcParseError):
        parse_proc_stat(raw, 10)


# --- LinuxHost over a fake /proc tree -------------------------------------------------------


@pytest.fixture
def proc_root(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text("1234-abcd\n")
    (root / "self").mkdir()
    write_mountinfo(root, "rw,relatime")
    return root


MOUNTINFO = "25 20 0:23 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc {opts}\n"


def write_mountinfo(root: Path, opts: str) -> None:
    (root / "self/mountinfo").write_text(MOUNTINFO.format(opts=opts))


def add_proc(root: Path, pid: int, raw: bytes) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "stat").write_bytes(raw)


def test_linux_boot_id(proc_root: Path):
    assert LinuxHost(proc_root).boot_id() == "1234-abcd"


@pytest.mark.parametrize("content", ["", "\n", "x" * 200, "a\x00b", "ab cd"])
def test_linux_boot_id_garbage_is_unavailable(proc_root: Path, content: str):
    (proc_root / "sys/kernel/random/boot_id").write_text(content)
    with pytest.raises(BootIdUnavailable):
        LinuxHost(proc_root).boot_id()


def test_linux_boot_id_missing(tmp_path: Path):
    with pytest.raises(BootIdUnavailable):
        LinuxHost(tmp_path).boot_id()


def test_linux_process_info_and_gone(proc_root: Path):
    add_proc(proc_root, 20, stat_line(20, b"a) (b", start=77))
    host = LinuxHost(proc_root)
    info = host.process_info(20)
    assert info is not None and info.start == 77
    assert host.process_info(21) is None


def test_linux_permission_denied_propagates(proc_root: Path):
    add_proc(proc_root, 20, stat_line(20, b"x"))
    stat = proc_root / "20" / "stat"
    stat.chmod(0)
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    with pytest.raises(PermissionError):
        LinuxHost(proc_root).process_info(20)


def test_linux_esrch_during_read_is_gone(proc_root: Path, monkeypatch: pytest.MonkeyPatch):
    host = LinuxHost(proc_root)

    def boom(path: Path, limit: int) -> bytes:
        raise ProcessLookupError(errno.ESRCH, "gone")

    monkeypatch.setattr("jobsctl.proc_host._read_limited", boom)
    assert host.process_info(5) is None


def test_linux_scan_group_counts_unreadable_and_skips_non_pids(proc_root: Path):
    add_proc(proc_root, 30, stat_line(30, b"a", pgrp=30))
    add_proc(proc_root, 31, stat_line(31, b"b", pgrp=30))
    add_proc(proc_root, 32, stat_line(32, b"c", pgrp=99))
    add_proc(proc_root, 33, b"garbage")
    (proc_root / "cpuinfo").write_text("x")
    scan = LinuxHost(proc_root).scan_group(30)
    assert sorted(m.pid for m in scan.members) == [30, 31]
    assert scan.unreadable == 1  # pid 33 could not be parsed


# --- hidepid and thread ids (R1-7, R1-6) ---------------------------------------------------


@pytest.mark.parametrize(
    "opts", ["rw,hidepid=2", "rw,hidepid=invisible", "rw,gid=1000,hidepid=2,subset=pid"]
)
def test_linux_hidepid_detected(proc_root: Path, opts: str):
    write_mountinfo(proc_root, opts)
    assert LinuxHost(proc_root).procs_hidden()


@pytest.mark.parametrize("opts", ["rw", "rw,hidepid=0", "rw,hidepid=1", "rw,hidepid=off"])
def test_linux_not_hidden(proc_root: Path, opts: str):
    write_mountinfo(proc_root, opts)
    assert not LinuxHost(proc_root).procs_hidden()


def test_linux_unreadable_or_proc_less_mountinfo_is_assumed_hidden(proc_root: Path):
    (proc_root / "self/mountinfo").write_text("1 0 0:1 / / rw - ext4 /dev/sda rw\n")
    assert LinuxHost(proc_root).procs_hidden()
    (proc_root / "self/mountinfo").unlink()
    assert LinuxHost(proc_root).procs_hidden()


def test_linux_hidepid_makes_group_scan_incomplete(proc_root: Path):
    add_proc(proc_root, 30, stat_line(30, b"a", pgrp=30))
    write_mountinfo(proc_root, "rw,hidepid=2")
    scan = LinuxHost(proc_root).scan_group(30)
    assert scan.unreadable >= 1 and [m.pid for m in scan.members] == [30]


def test_linux_hidepid_missing_process_is_unknown_not_dead(proc_root: Path):
    write_mountinfo(proc_root, "rw,hidepid=2")
    res = check_identity(LinuxHost(proc_root), Identity("1234-abcd", 77, 5))
    assert res.state is Liveness.UNKNOWN and res.reason == "hidepid"


def test_linux_visible_missing_process_is_dead(proc_root: Path):
    res = check_identity(LinuxHost(proc_root), Identity("1234-abcd", 77, 5))
    assert res.state is Liveness.DEAD and res.reason == "gone"


def test_fake_hidden_missing_process_is_unknown_and_group_probe_incomplete():
    host = FakeHost(hidden=True)
    assert check_identity(host, Identity("boot-1", 5, 9)).state is Liveness.UNKNOWN
    assert not probe_group(host, 5).complete


def add_status(root: Path, pid: int, tgid: int) -> None:
    (root / str(pid) / "status").write_text(f"Name:\tx\nTgid:\t{tgid}\nPid:\t{pid}\n")


def test_linux_thread_group_id_reads_tgid(proc_root: Path):
    add_proc(proc_root, 20, stat_line(20, b"x"))
    add_status(proc_root, 20, 19)
    host = LinuxHost(proc_root)
    assert host.thread_group_id(20) == 19
    assert host.thread_group_id(21) is None


def test_linux_thread_group_id_without_tgid_is_a_parse_error(proc_root: Path):
    add_proc(proc_root, 20, stat_line(20, b"x"))
    (proc_root / "20" / "status").write_text("Name:\tx\n")
    with pytest.raises(ProcParseError):
        LinuxHost(proc_root).thread_group_id(20)


def test_identify_refuses_a_thread_id():
    host = FakeHost(procs={50: proc(50, start=321)}, tgids={50: 40})
    with pytest.raises(ProcParseError):
        identify(host, 50)


def test_identify_refuses_when_tgid_unreadable(proc_root: Path):
    add_proc(proc_root, 20, stat_line(20, b"x"))  # no status file content with Tgid
    (proc_root / "20" / "status").write_text("")
    with pytest.raises(ProcParseError):
        identify(LinuxHost(proc_root), 20)


# --- identify / check_identity (fake host) --------------------------------------------------


def test_identify_records_boot_pid_start():
    host = FakeHost(procs={50: proc(50, start=321)})
    assert identify(host, 50) == Identity("boot-1", 50, 321)


def test_identify_gone():
    with pytest.raises(ProcessGone):
        identify(FakeHost(), 50)


def test_identify_without_boot_id_raises():
    with pytest.raises(BootIdUnavailable):
        identify(FakeHost(boot=None, procs={50: proc(50)}), 50)


@pytest.mark.parametrize("pid", [0, 1, -1, 2**40, True])
def test_identity_rejects_unsafe_pids(pid: int):
    with pytest.raises(ValueError):
        Identity("b", pid, 1)


def test_identity_rejects_bad_boot_and_start():
    with pytest.raises(ValueError):
        Identity("", 5, 1)
    with pytest.raises(ValueError):
        Identity("b", 5, -1)


def test_alive():
    host = FakeHost(procs={5: proc(5, start=9)})
    res = check_identity(host, Identity("boot-1", 5, 9))
    assert res.state is Liveness.ALIVE and res.alive and res.info is not None


def test_reaped_pid_not_alive():
    res = check_identity(FakeHost(), Identity("boot-1", 5, 9))
    assert res.state is Liveness.DEAD and res.reason == "gone"


def test_reused_pid_not_alive():
    host = FakeHost(procs={5: proc(5, start=10)})
    res = check_identity(host, Identity("boot-1", 5, 9))
    assert res.state is Liveness.DEAD and res.reason == "reused"


def test_new_boot_not_alive_even_if_start_matches():
    host = FakeHost(boot="boot-2", procs={5: proc(5, start=9)})
    res = check_identity(host, Identity("boot-1", 5, 9))
    assert res.state is Liveness.DEAD and res.reason == "boot_changed"


def test_zombie_not_alive_unless_reserved_leader():
    host = FakeHost(procs={5: proc(5, start=9, zombie=True)})
    ident = Identity("boot-1", 5, 9)
    res = check_identity(host, ident)
    assert res.state is Liveness.DEAD and res.reason == "zombie"
    assert check_identity(host, ident, allow_zombie=True).state is Liveness.ALIVE


def test_zombie_with_other_start_is_still_reused_with_allow_zombie():
    host = FakeHost(procs={5: proc(5, start=10, zombie=True)})
    res = check_identity(host, Identity("boot-1", 5, 9), allow_zombie=True)
    assert res.state is Liveness.DEAD


@pytest.mark.parametrize(
    "exc", [PermissionError(errno.EACCES, "x"), ProcParseError("bad"), OSError(errno.EIO, "x")]
)
def test_unreadable_is_unknown_never_dead(exc: Exception):
    host = FakeHost(info_errors={5: exc})
    res = check_identity(host, Identity("boot-1", 5, 9))
    assert res.state is Liveness.UNKNOWN and not res.alive


def test_missing_boot_id_is_unknown():
    res = check_identity(FakeHost(boot=None, procs={5: proc(5, 9)}), Identity("b", 5, 9))
    assert res.state is Liveness.UNKNOWN


# --- macOS (fakes only; unverified on a real Mac) -------------------------------------------


class FakeLibproc:
    def __init__(self) -> None:
        self.info: dict[int, tuple[int, int, int, int, bool]] = {}  # pid -> ppid,pgid,sec,usec,Z
        self.errors: dict[int, int] = {}
        self.groups: dict[int, list[int]] = {}
        self.uuid = "AAAA-BBBB"

    def bsdinfo(self, pid: int) -> tuple[int, int, int, int, bool] | None:
        if pid in self.errors:
            raise OSError(self.errors[pid], "fake")
        return self.info.get(pid)

    def group_pids(self, pgid: int) -> list[int]:
        return self.groups.get(pgid, [])

    def boot_uuid(self) -> str:
        return self.uuid


def test_darwin_identity_start_is_seconds_and_microseconds():
    lib = FakeLibproc()
    lib.info[40] = (1, 40, 1_700_000_000, 123456, False)
    host = DarwinHost(lib)
    ident = identify(host, 40)
    assert ident == Identity("AAAA-BBBB", 40, 1_700_000_000 * 1_000_000 + 123456)
    assert check_identity(host, ident).alive


def test_darwin_microsecond_difference_is_a_different_process():
    lib = FakeLibproc()
    lib.info[40] = (1, 40, 5, 2, False)
    host = DarwinHost(lib)
    res = check_identity(host, Identity("AAAA-BBBB", 40, 5_000_001))
    assert res.reason == "reused"


def test_darwin_zombie_and_gone_and_denied():
    lib = FakeLibproc()
    lib.info[40] = (1, 40, 5, 2, True)
    lib.errors[41] = errno.EPERM
    host = DarwinHost(lib)
    ident = Identity("AAAA-BBBB", 40, 5_000_002)
    assert check_identity(host, ident).reason == "zombie"
    assert check_identity(host, ident, allow_zombie=True).alive
    assert check_identity(host, Identity("AAAA-BBBB", 42, 1)).reason == "gone"
    assert check_identity(host, Identity("AAAA-BBBB", 41, 1)).state is Liveness.UNKNOWN


def test_darwin_group_scan_probes_group_only():
    lib = FakeLibproc()
    lib.info.update({40: (1, 40, 5, 2, False), 41: (40, 40, 6, 0, True)})
    lib.groups[40] = [40, 41, 99]  # 99 vanished before the info read
    scan = DarwinHost(lib).scan_group(40)
    assert sorted(m.pid for m in scan.members) == [40, 41]
    assert [m.zombie for m in scan.members if m.pid == 41] == [True]


def test_darwin_boot_uuid_empty_is_unavailable():
    lib = FakeLibproc()
    lib.uuid = ""
    with pytest.raises(BootIdUnavailable):
        DarwinHost(lib).boot_id()


@pytest.mark.parametrize(
    "exc", [FileNotFoundError(2, "no sysctl"), subprocess.TimeoutExpired("s", 5)]
)
def test_darwin_sysctl_failure_is_boot_id_unavailable(
    exc: Exception, monkeypatch: pytest.MonkeyPatch
):
    argv: list[list[str]] = []

    def boom(a: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        argv.append(a)
        raise exc

    monkeypatch.setattr("jobsctl.gateway.run", boom)
    host = DarwinHost()
    with pytest.raises(BootIdUnavailable):
        host.boot_id()
    assert argv[0][0] == "/usr/sbin/sysctl"
    res = check_identity(host, Identity("AAAA", 40, 5))
    assert res.state is Liveness.UNKNOWN and res.reason == "boot_id_unavailable"


def test_darwin_sysctl_nonzero_exit_is_boot_id_unavailable(monkeypatch: pytest.MonkeyPatch):
    done = subprocess.CompletedProcess(["x"], 1, stdout="junk\n", stderr="")
    monkeypatch.setattr("jobsctl.gateway.run", lambda a, **kw: done)
    with pytest.raises(BootIdUnavailable):
        DarwinHost().boot_id()


def test_darwin_has_no_pidfds():
    assert DarwinHost(FakeLibproc()).pidfd_supported() is False
