"""macOS code paths that run on Linux: fakes for libproc, and what a platform without
``os.waitid`` needs to import. None of this proves anything about a real Mac
(docs/macos-checklist.md)."""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

from jobsctl import cron, gateway_proc
from jobsctl import env_canonical as ec
from jobsctl.leftovers import count_leftovers
from jobsctl.proc_host import DarwinHost

from .test_proc_identity import FakeLibproc

# -- temporary directories on macOS ------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "/var/folders/zz/abc123/T/bin",
        "/private/var/folders/zz/abc123/T/bin",
        "/var/folders",
        "/private/tmp/x",
        "/private/tmp",
    ],
)
def test_macos_temp_dirs_are_temporary_for_cron_and_for_the_job_path(entry: str) -> None:
    kept, warnings = cron.filter_path([entry], home="/h")
    assert kept == [] and [w["reason"] for w in warnings] == ["temporary"]
    assert ec._drop_reason(entry, ec._temp_roots()) == "temporary"


@pytest.mark.parametrize("entry", ["/var/foldersX/bin", "/private/tmpfoo", "/var/folder/x"])
def test_macos_temp_roots_are_path_prefixes_not_string_prefixes(entry: str) -> None:
    assert not cron._is_temporary(entry, cron.TEMP_ROOTS)
    _, warnings = cron.filter_path([entry], home="/h")
    assert [w["reason"] for w in warnings] == ["missing"]
    assert ec._drop_reason(entry, ec._temp_roots()) == "missing"  # not under a root, no such dir


# -- scan_children (macOS: libproc children by ppid) ---------------------------------------------


def _lib() -> FakeLibproc:
    lib = FakeLibproc()
    lib.info.update(
        {
            100: (1, 100, 1, 0, False),  # the shim
            200: (100, 200, 2, 0, False),  # the job leader
            300: (100, 300, 3, 0, False),  # a child that left the group, parent still alive
            301: (100, 100, 4, 0, True),  # an exited child (zombie)
            400: (999, 400, 5, 0, False),  # listed, but its parent changed: not a child
        }
    )
    lib.children[100] = [200, 300, 301, 400, 555]  # 555 vanished before the read
    return lib


def test_darwin_scan_children_lists_live_and_zombie_children_only() -> None:
    scan = DarwinHost(_lib()).scan_children(100)
    assert sorted(m.pid for m in scan.members) == [200, 300, 301]
    assert scan.unreadable == 0 and not scan.hidden


def test_darwin_scan_children_unreadable_entry_makes_the_scan_incomplete() -> None:
    lib = _lib()
    lib.errors[300] = errno.EPERM
    scan = DarwinHost(lib).scan_children(100)
    assert sorted(m.pid for m in scan.members) == [200, 301] and scan.unreadable == 1


def test_darwin_scan_children_listing_failure_is_incomplete_never_empty_and_complete() -> None:
    lib = _lib()
    lib.list_errors.add(100)
    scan = DarwinHost(lib).scan_children(100)
    assert scan.members == () and scan.unreadable == 1


def test_darwin_leftovers_count_a_live_child_outside_the_group_and_reap_the_zombie() -> None:
    reaped: list[int] = []
    got = count_leftovers(
        DarwinHost(_lib()),
        leader_pid=200,
        pgid=200,
        own_pid=100,
        reap=lambda pid: reaped.append(pid) or True,
    )
    assert got.pids == (300,) and got.complete and reaped == [301]


# -- wait without WNOWAIT (libproc zombie state) -------------------------------------------------

ME = 4242


def _state(table: dict[int, tuple[int, bool, int]]) -> Callable[[int], Any]:
    return lambda pid: table.get(pid)


def _wait(table: dict[int, tuple[int, bool, int]], pid: int, **kw: Any) -> Any:
    return gateway_proc.emulated_waitid_nowait(
        _state(table), pid, parent=ME, sleep=kw.pop("sleep", lambda s: None), **kw
    )


def test_wait_status_decoding_matches_the_waitid_codes_the_shim_maps() -> None:
    from jobsctl import shim_exit

    assert (gateway_proc.CLD_EXITED, gateway_proc.CLD_KILLED, gateway_proc.CLD_DUMPED) == (
        shim_exit._EXITED,
        shim_exit._KILLED,
        shim_exit._DUMPED,
    )


@pytest.mark.parametrize(
    ("status", "record"),
    [
        (0, ("exited", 0, None, False)),
        (7 << 8, ("exited", 7, None, False)),
        (255 << 8, ("exited", 255, None, False)),
        (9, ("signaled", None, 9, False)),
        (11 | 0x80, ("signaled", None, 11, True)),
    ],
)
def test_a_zombie_child_is_reported_without_reaping(
    status: int, record: tuple[str, int | None, int | None, bool]
) -> None:
    from jobsctl.shim_exit import exit_record

    got = _wait({10: (ME, True, status)}, 10)
    assert got.si_pid == 10
    rec = exit_record(got.si_code, got.si_status)
    assert (rec.kind, rec.code, rec.signal, rec.core) == record


def test_nohang_returns_none_for_a_running_child() -> None:
    assert _wait({10: (ME, False, 0)}, 10, nohang=True) is None


def test_blocking_wait_polls_until_the_child_is_a_zombie_and_is_bounded_by_the_caller() -> None:
    table = {10: (ME, False, 0)}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            table[10] = (ME, True, 3 << 8)

    got = _wait(table, 10, sleep=sleep)
    assert got.si_status == 3 and len(sleeps) == 3 and all(0 < s <= 0.1 for s in sleeps)


@pytest.mark.parametrize("nohang", [True, False])
def test_a_gone_process_or_one_that_is_not_our_child_is_echild(nohang: bool) -> None:
    for table in ({}, {10: (ME + 1, True, 0)}, {10: (1, False, 0)}):
        with pytest.raises(ChildProcessError) as ei:
            _wait(table, 10, nohang=nohang)
        assert ei.value.errno == errno.ECHILD


def test_an_undecodable_status_is_an_error_not_a_guess() -> None:
    with pytest.raises(OSError) as ei:
        _wait({10: (ME, True, 0x7F | (19 << 8))}, 10)  # a "stopped" status on a zombie
    assert ei.value.errno == errno.EIO


def test_waitid_nowait_uses_libproc_only_where_os_has_no_waitid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lib:
        def wait_state(self, pid: int) -> tuple[int, bool, int]:
            return (os.getpid(), True, 5 << 8)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delattr(os, "waitid")
    monkeypatch.setattr(gateway_proc, "DarwinLibproc", Lib)
    assert gateway_proc.waitid_nowait(77).si_status == 5  # type: ignore[union-attr]
    monkeypatch.setattr(sys, "platform", "freebsd14")
    with pytest.raises(OSError) as ei:
        gateway_proc.waitid_nowait(77)
    assert ei.value.errno == errno.ENOSYS


# -- importing the program where os.waitid does not exist ----------------------------------------


def test_every_module_imports_without_os_waitid() -> None:
    """``os.waitid`` and ``os.waitid_result`` are missing on macOS CPython; a module that names
    them at import time would stop ``jobsctl`` from starting there."""
    import jobsctl

    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(jobsctl.__file__)))
    code = (
        "import os, sys, importlib, pkgutil\n"
        f"sys.path.insert(0, {package_parent!r})\n"
        "del os.waitid, os.waitid_result\n"
        "import jobsctl\n"
        f"assert os.path.dirname(os.path.dirname(jobsctl.__file__)) == {package_parent!r}\n"
        "for m in pkgutil.walk_packages(jobsctl.__path__, 'jobsctl.'):\n"
        "    if m.name.startswith('jobsctl.tui') or m.name.endswith('__main__'):\n"
        "        continue  # the TUI needs the optional extra; __main__ runs the CLI\n"
        "    importlib.import_module(m.name)\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        env={"HOME": os.environ["HOME"]},
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        cwd="/",
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-800:]


# -- DarwinLibproc against a fake libproc library (errno, sizes, buffers) ------------------------


class FakeLib:
    """Stands in for ``libproc.dylib`` (``proc_pidinfo``, ``proc_listpids``) as ctypes sees it."""

    def __init__(self) -> None:
        self.pidinfo_calls: list[int] = []  # the `arg` of every proc_pidinfo call
        self.zombie_needs_arg_1 = False
        self.pidinfo_errno = errno.ESRCH
        self.pids: list[int] = []
        self.list_errno_by_call: dict[int, int] = {}  # call number (1 = sizing) -> errno
        self.sizing_extra = 0  # bytes the sizing call under-reports by
        self.list_calls = 0
        self.buffer_sizes: list[int] = []

    def proc_pidinfo(self, pid: Any, flavor: Any, arg: Any, buf: Any, size: Any) -> int:
        self.pidinfo_calls.append(arg.value)
        if self.zombie_needs_arg_1 and arg.value != 1:
            ctypes.set_errno(self.pidinfo_errno)
            return 0
        info = buf._obj
        info.pbi_pid = pid.value
        info.pbi_status = gateway_proc.SZOMB
        info.pbi_xstatus = 3 << 8
        return int(size.value)

    def proc_listpids(self, kind: Any, arg: Any, buf: Any, size: Any) -> int:
        self.list_calls += 1
        if self.list_calls in self.list_errno_by_call:
            ctypes.set_errno(self.list_errno_by_call[self.list_calls])
            return -1
        if buf is None:
            return max(0, 4 * len(self.pids) - self.sizing_extra)
        self.buffer_sizes.append(ctypes.sizeof(buf))
        room = len(buf)
        for i, pid in enumerate(self.pids[:room]):
            buf[i] = pid
        return 4 * min(room, len(self.pids))


@pytest.fixture
def fake_lib(monkeypatch: pytest.MonkeyPatch) -> FakeLib:
    lib = FakeLib()
    monkeypatch.setattr(gateway_proc, "_get_libproc", lambda: lib)
    return lib


def test_a_zombie_that_answers_only_with_arg_1_is_read_by_the_retry(fake_lib: FakeLib) -> None:
    fake_lib.zombie_needs_arg_1 = True
    got = gateway_proc.DarwinLibproc().bsdinfo(55)
    assert got is not None and got[4] is True
    assert fake_lib.pidinfo_calls == [0, 1]
    assert gateway_proc.DarwinLibproc().wait_state(55) == (0, True, 3 << 8)


def test_a_process_that_is_gone_is_none_after_one_retry(fake_lib: FakeLib) -> None:
    fake_lib.zombie_needs_arg_1 = True
    fake_lib.pidinfo_calls.clear()
    # still ESRCH with arg 1 too: gone
    fake_lib.proc_pidinfo = lambda pid, flavor, arg, buf, size: (  # type: ignore[method-assign]
        fake_lib.pidinfo_calls.append(arg.value),
        ctypes.set_errno(errno.ESRCH),
        0,
    )[2]
    assert gateway_proc.DarwinLibproc().bsdinfo(55) is None
    assert fake_lib.pidinfo_calls == [0, 1]


def test_other_pidinfo_errors_are_not_retried_and_are_raised(fake_lib: FakeLib) -> None:
    fake_lib.zombie_needs_arg_1 = True
    fake_lib.pidinfo_errno = errno.EPERM
    with pytest.raises(OSError) as ei:
        gateway_proc.DarwinLibproc().bsdinfo(55)
    assert ei.value.errno == errno.EPERM and fake_lib.pidinfo_calls == [0]


def test_a_failed_second_listing_call_is_an_error_when_strict(fake_lib: FakeLib) -> None:
    fake_lib.pids = [5, 6]
    fake_lib.list_errno_by_call = {2: errno.EPERM}
    with pytest.raises(OSError) as ei:
        gateway_proc.DarwinLibproc().child_pids(1)
    assert ei.value.errno == errno.EPERM


def test_a_failed_second_listing_call_reads_as_empty_for_a_group(fake_lib: FakeLib) -> None:
    fake_lib.pids = [5, 6]
    fake_lib.list_errno_by_call = {2: errno.EPERM}
    assert gateway_proc.DarwinLibproc().group_pids(1) == []


def test_a_listing_that_fills_the_buffer_is_retried_with_a_larger_one(fake_lib: FakeLib) -> None:
    fake_lib.pids = list(range(1, 101))
    fake_lib.sizing_extra = 4 * 84  # sizing under-reports: 32 entries at first, then doubles
    got = gateway_proc.DarwinLibproc().child_pids(1)
    assert got == list(range(1, 101))
    assert fake_lib.buffer_sizes == sorted(fake_lib.buffer_sizes) and len(fake_lib.buffer_sizes) > 1


def test_a_listing_that_never_fits_is_incomplete_when_strict_and_truncated_otherwise(
    fake_lib: FakeLib,
) -> None:
    fake_lib.pids = list(range(1, 100_000))
    fake_lib.sizing_extra = 4 * 99_000
    with pytest.raises(OSError):
        gateway_proc.DarwinLibproc().child_pids(1)
    assert len(fake_lib.buffer_sizes) <= 5
    assert len(gateway_proc.DarwinLibproc().group_pids(1)) > 0


def test_a_listing_that_fits_makes_two_calls(fake_lib: FakeLib) -> None:
    fake_lib.pids = [7, 8, 9]
    assert gateway_proc.DarwinLibproc().child_pids(1) == [7, 8, 9]
    assert fake_lib.list_calls == 2


# -- boot_uuid failure paths (a fake gateway.run) ------------------------------------------------


def _boot(monkeypatch: pytest.MonkeyPatch, **result: Any) -> str:
    class Res:
        returncode = result.get("returncode", 0)
        stdout = result.get("stdout", "")

    def run(argv: list[str], **kw: Any) -> Any:
        if "raises" in result:
            raise result["raises"]
        assert argv == [gateway_proc.SYSCTL, "-n", "kern.bootsessionuuid"] and kw["timeout"] == 5
        return Res()

    monkeypatch.setattr(gateway_proc.gateway, "run", run)
    return gateway_proc.DarwinLibproc().boot_uuid()


def test_boot_uuid_is_stripped_and_a_nonzero_exit_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _boot(monkeypatch, stdout="AAAA-BBBB\n") == "AAAA-BBBB"
    assert _boot(monkeypatch, returncode=1, stdout="x") == ""


def test_boot_uuid_missing_binary_and_timeout_propagate_for_the_host_to_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(OSError):
        _boot(monkeypatch, raises=FileNotFoundError(errno.ENOENT, "sysctl"))
    with pytest.raises(subprocess.TimeoutExpired):
        _boot(monkeypatch, raises=subprocess.TimeoutExpired("sysctl", 5))


@pytest.mark.parametrize(
    "exc", [FileNotFoundError(errno.ENOENT, "x"), subprocess.TimeoutExpired("sysctl", 5)]
)
def test_the_darwin_host_reports_an_unavailable_boot_id_for_both(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    from jobsctl.proc_host import BootIdUnavailable

    def raising(argv: list[str], **kw: Any) -> Any:
        raise exc

    monkeypatch.setattr(gateway_proc.gateway, "run", raising)
    with pytest.raises(BootIdUnavailable):
        DarwinHost(gateway_proc.DarwinLibproc()).boot_id()
