"""macOS code paths that run on Linux: fakes for libproc, and what a platform without
``os.waitid`` needs to import. None of this proves anything about a real Mac
(docs/macos-checklist.md)."""

from __future__ import annotations

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
    assert ec._drop_reason(entry, ec._temp_roots()) != "temporary"


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
    code = (
        "import os, importlib, pkgutil\n"
        "del os.waitid, os.waitid_result\n"
        "import jobsctl\n"
        "for m in pkgutil.walk_packages(jobsctl.__path__, 'jobsctl.'):\n"
        "    if m.name.startswith('jobsctl.tui') or m.name.endswith('__main__'):\n"
        "        continue  # the TUI needs the optional extra; __main__ runs the CLI\n"
        "    importlib.import_module(m.name)\n"
    )
    res = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env={"HOME": os.environ["HOME"], "PYTHONPATH": ":".join(sys.path)},
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-800:]
