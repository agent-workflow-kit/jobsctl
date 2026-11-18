"""Every test file that names a Linux-only facility is classified, and the marked ones are marked.

The scan is the "regenerated from a grep" list the macOS checklist points at: a new test that
reads ``/proc``, calls ``os.waitid`` or ``prctl`` fails here until its file is classified below.
Nothing here proves macOS behavior; it keeps the Linux-only tests from running unmarked.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests import capabilities, kill_guard
from tests.proc_fakes import FakeHost, proc

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FACILITY = re.compile(r"/proc\b|waitid|prctl|subreaper", re.IGNORECASE)

MARKED = "marked"  # real use of the facility, behind a capability skip in the file
FAKED = "faked"  # names it as data, a fake or documentation; runs anywhere
LIVE = "live-macos"  # macOS-only checks

# file -> (class, why). Add a file here, with its mark, when the scan below finds it.
CLASSIFIED: dict[str, tuple[str, str]] = {
    "proc_fakes.py": (FAKED, "fake host; no real read"),
    "proc_real.py": (FAKED, "helper child sets its name with prctl; used by marked tests only"),
    "runner_helpers.py": (FAKED, "fake shim lists /proc/self/fd only where /proc exists"),
    "shim_helpers.py": (FAKED, "docstring only"),
    "spike_helpers.py": (MARKED, "reads /proc/<pid>/stat; linux_only for its spike tests"),
    "test_cron_command.py": (MARKED, "/proc/locks, skipif where absent"),
    "test_darwin_paths.py": (FAKED, "fakes libproc; runs on any host"),
    "test_env_file.py": (MARKED, "prctl dumpable flag, capability skip"),
    "test_hook_run.py": (MARKED, "Linux-only until the live macOS run (waitid emulation)"),
    "test_leftovers.py": (MARKED, "real subreaper and os.waitid"),
    "test_macos_live.py": (LIVE, "macOS only, live"),
    "test_proc_identity.py": (FAKED, "parses /proc text from a fake root"),
    "test_proc_real.py": (MARKED, "module skip: Linux /proc and pidfd"),
    "test_runner.py": (MARKED, "/proc fd listing, os.waitid spy"),
    "test_shim.py": (MARKED, "/proc zombie state and SigIgn"),
    "test_shim_exit.py": (MARKED, "os.waitid"),
    "test_shim_output.py": (MARKED, "a LinuxHost subclass"),
    "test_shim_processes.py": (MARKED, "/proc reads, subreaper, dumpable"),
    "test_spike_processes.py": (MARKED, "module linux_only"),
    "test_spike_state.py": (MARKED, "linux_only on the /proc spike"),
    "test_wait_processes.py": (MARKED, "/proc/<pid>/fd polling"),
}
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
    assert CLASSIFIED.keys() - found == set(), "stale entries: no longer name a facility"


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
    monkeypatch.setattr(kill_guard, "_host", host)
    assert kill_guard._descendant_of_us(7001) is known
    assert kill_guard._permitted(7001) is known
