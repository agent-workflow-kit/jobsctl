"""Skip conditions for tests that need a facility only some platforms have.

Each mark names the capability a test needs, not the platform it happens to be missing on: a
test that reads ``/proc`` is skipped where ``/proc`` is absent, not "on darwin". Nothing here
has been run on macOS (docs/macos-checklist.md); the marks only record what the code reads.

``tests/test_platform_marks.py`` checks that every test file naming one of these facilities is
classified, so a new Linux-only test cannot be added unmarked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ``/proc/<pid>/{stat,status,environ,fd}`` readable for a process (Linux; absent on macOS).
HAS_PROC = Path("/proc/self/stat").is_file()
# ``os.waitid`` (``WEXITED | WNOWAIT``): CPython does not provide it on macOS.
HAS_WAITID = hasattr(os, "waitid")
# ``prctl`` (``PR_SET_CHILD_SUBREAPER``, ``PR_SET_DUMPABLE``): jobsctl itself gates these on the
# platform (``proc_linux``), so the test condition is the same one.
HAS_PRCTL = sys.platform == "linux"

needs_proc = pytest.mark.skipif(not HAS_PROC, reason="needs /proc (Linux)")
needs_waitid = pytest.mark.skipif(not HAS_WAITID, reason="needs os.waitid (absent on macOS)")
needs_prctl = pytest.mark.skipif(
    not HAS_PRCTL, reason="needs prctl: child subreaper and dumpable flag (Linux)"
)
