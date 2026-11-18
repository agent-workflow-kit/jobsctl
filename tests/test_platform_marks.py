"""Every test file that names a Linux-only facility is classified, and the marked ones carry a skip.

The scan is the "regenerated from a grep" list the macOS checklist points at: a new test file that
names ``/proc``, ``os.waitid``, ``prctl``, a subreaper, a pidfd, ``fdinfo`` or ``LinuxHost`` fails
here until it is classified below. The check is per file: a marked file must contain some
capability skip, not that every test using the facility carries one; marking each test is by
review. Nothing here proves macOS behavior.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from jobsctl.proc_host import ProcInfo, ProcParseError
from tests import capabilities, kill_guard, spike_helpers, test_shim
from tests.proc_fakes import FakeHost, proc

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FACILITY = re.compile(r"/proc\b|waitid|prctl|subreaper|pidfd|fdinfo|LinuxHost", re.IGNORECASE)

MARKED = "marked"  # real use of the facility, behind a capability skip in the file
FAKED = "faked"  # names it as data, a fake or documentation; runs anywhere
LIVE = "live-macos"  # macOS-only checks
HELPER = "linux-helper"  # real Linux facility, imported only by files that are marked

# file -> (class, why). Add a file here, with its mark, when the scan below finds it.
CLASSIFIED: dict[str, tuple[str, str]] = {
    "hook_real.py": (HELPER, "real pidfd and LinuxHost; imported only by the marked hook tests"),
    "proc_fakes.py": (FAKED, "fake host; no real read"),
    "proc_real.py": (FAKED, "helper child sets its name with prctl; used by marked tests only"),
    "runner_helpers.py": (FAKED, "fake shim lists /proc/self/fd only where /proc exists"),
    "shim_helpers.py": (FAKED, "docstring only"),
    "spike_helpers.py": (
        MARKED,
        "stat_fields reads /proc; its spike tests are linux_only; the rest is portable",
    ),
    "test_cron_command.py": (MARKED, "/proc/locks, skipif where absent"),
    "test_darwin_paths.py": (FAKED, "fakes libproc; runs on any host"),
    "test_env_file.py": (MARKED, "prctl dumpable flag, capability skip"),
    "test_hook_interrupt.py": (MARKED, "Linux-only until the live macOS run; module skipif"),
    "test_hook_run.py": (MARKED, "Linux-only until the live macOS run (waitid emulation)"),
    "test_kill.py": (FAKED, "pidfd_ok=False on a fake host"),
    "test_kill_guard.py": (FAKED, "a raw syscall number on a pipe; no real pidfd"),
    "test_leftovers.py": (MARKED, "real subreaper and os.waitid"),
    "test_macos_live.py": (LIVE, "macOS only, live"),
    "test_proc_identity.py": (FAKED, "parses /proc text from a fake root"),
    "test_proc_real.py": (MARKED, "module skip: Linux /proc and pidfd"),
    "test_proc_signal.py": (FAKED, "fake host pidfd answers"),
    "test_reconcile.py": (FAKED, "pidfd_ok=False on a fake host"),
    "test_runner.py": (MARKED, "/proc fd listing, os.waitid spy"),
    "test_shim.py": (MARKED, "/proc SigIgn read"),
    "test_shim_exit.py": (MARKED, "os.waitid"),
    "test_shim_output.py": (MARKED, "a LinuxHost subclass"),
    "test_shim_processes.py": (MARKED, "/proc reads, subreaper, dumpable"),
    "test_shim_signals.py": (FAKED, "pidfd_ok=False on a fake host"),
    "test_shim_signals_unit.py": (FAKED, "fake host pidfd answers"),
    "test_spike_processes.py": (MARKED, "module linux_only"),
    "test_spike_state.py": (MARKED, "linux_only on the /proc spike"),
    "test_wait_processes.py": (MARKED, "/proc/<pid>/fd polling"),
}
# Classified by review: the file names none of the facilities above, but is Linux-only per #80
# (a module-level skipif on the platform). Exempt from the stale-entry check.
BY_REVIEW = {"test_hook_interrupt.py"}
MARKS = ("needs_proc", "needs_waitid", "needs_prctl", "HAS_PROC", "linux_only", "skipif")


def _scan() -> set[str]:
    me = Path(__file__).name
    return {
        path.name
        for path in TESTS.glob("*.py")
        if path.name not in {me, "capabilities.py", "kill_guard.py"}
        and FACILITY.search(path.read_text(encoding="utf-8"))
    }


def test_every_file_naming_a_linux_only_facility_is_classified() -> None:
    found = _scan()
    assert found - CLASSIFIED.keys() == set(), "classify (and mark) these files"
    assert CLASSIFIED.keys() - found - BY_REVIEW == set(), (
        "stale entries: no longer name a facility"
    )


def test_every_marked_file_carries_a_capability_skip() -> None:
    for name, (kind, _why) in CLASSIFIED.items():
        if kind != MARKED:
            continue
        text = (TESTS / name).read_text(encoding="utf-8")
        assert any(re.search(rf"\b{mark}\b", text) for mark in MARKS), f"{name}: no skip mark"


def test_a_capability_mark_skips_exactly_where_its_facility_is_missing() -> None:
    for mark, flag in (
        (capabilities.needs_proc, capabilities.HAS_PROC),
        (capabilities.needs_waitid, capabilities.HAS_WAITID),
        (capabilities.needs_prctl, capabilities.HAS_PRCTL),
    ):
        assert mark.name == "skipif"
        assert mark.args == (not flag,)
        assert mark.kwargs["reason"].startswith("needs ")


def test_unknown_markers_fail_the_run() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "--strict-markers" in config["tool"]["pytest"]["ini_options"]["addopts"]


def test_the_checklist_points_at_this_classification() -> None:
    assert "tests/test_platform_marks.py" in (ROOT / "docs/macos-checklist.md").read_text()


@pytest.mark.parametrize("known", [True, False])
def test_the_kill_guard_finds_a_descendant_through_the_host(
    monkeypatch: pytest.MonkeyPatch, known: bool
) -> None:
    """The parent chain comes from the platform host (libproc on macOS), not from ``/proc``."""
    me = kill_guard.os.getpid()
    host = FakeHost(procs={7001: proc(7001, ppid=7000), 7000: proc(7000, ppid=me)})
    if not known:
        del host.procs[7000]  # the chain breaks: unknown means not a descendant
    monkeypatch.setattr(kill_guard, "_host", lambda: host)
    assert kill_guard._descendant_of_us(7001) is known
    assert kill_guard._permitted(7001) is known


def test_test_shim_exit_collects_without_os_waitid_and_the_cld_constants() -> None:
    """A module-level ``os.CLD_*`` read fails collection (aborting the whole session), not one
    test; the module must import where ``os.waitid`` and the constants are absent (macOS)."""
    code = (
        "import os, sys, importlib\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "for name in ('waitid', 'waitid_result', 'CLD_STOPPED', 'CLD_CONTINUED', 'CLD_DUMPED',\n"
        "             'CLD_EXITED', 'CLD_KILLED', 'CLD_TRAPPED'):\n"
        "    if hasattr(os, name):\n"
        "        delattr(os, name)\n"
        "importlib.import_module('tests.test_shim_exit')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        env={"HOME": os.environ.get("HOME", "/")},
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        cwd="/",
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-800:]


def test_the_kill_guard_builds_its_host_lazily_and_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the guard builds no host (a libproc load failure cannot break every test module),
    and a host that cannot be built, or whose read raises, reads as "not a descendant"."""
    assert "default_host()" not in (TESTS / "kill_guard.py").read_text().split("def _host")[0]

    def broken() -> object:
        raise RuntimeError("libproc failed to load")

    monkeypatch.setattr(kill_guard, "_host", broken)
    assert kill_guard._parent_of(os.getpid()) is None
    assert kill_guard._descendant_of_us(os.getpid() + 1_000_000) is False

    class RaisingHost(FakeHost):
        def process_info(self, pid: int) -> ProcInfo | None:
            raise RuntimeError("unreadable")

    monkeypatch.setattr(kill_guard, "_host", lambda: RaisingHost())
    assert kill_guard._parent_of(os.getpid()) is None


@pytest.mark.parametrize("error", [PermissionError("hidden"), ProcParseError("garbled")])
def test_the_probe_helpers_read_an_unreadable_process_as_gone(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """``start_time``, ``alive`` and the shim's zombie probe keep the old ``/proc`` read's meaning:
    a process that cannot be read is gone, not an error."""

    class UnreadableHost(FakeHost):
        def process_info(self, pid: int) -> ProcInfo | None:
            raise error

    monkeypatch.setattr(spike_helpers, "default_host", lambda: UnreadableHost())
    monkeypatch.setattr(test_shim, "default_host", lambda: UnreadableHost())
    assert spike_helpers.start_time(1) is None
    assert spike_helpers.alive(1) is False
    assert test_shim._zombie_state(1) is None
