"""Fixtures of the runner tests (registered through ``pytest_plugins`` in ``conftest.py``)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl.leases import identity_text
from jobsctl.proc_host import default_host
from jobsctl.runner import Runner
from tests.runner_helpers import ShimKit, kill_recorded_shims, make_kit


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[ShimKit]:
    k = make_kit(tmp_path)
    try:
        yield k
    finally:
        k.stop.touch()
        kill_recorded_shims()


@pytest.fixture
def runners() -> Iterator[list[Runner]]:
    """Runners a test creates; teardown kills and reaps exactly the processes they spawned
    (handles held unreaped, so no PID can have been reused)."""
    made: list[Runner] = []
    try:
        yield made
    finally:
        for runner in made:
            for child in runner.children:
                if child.popen.poll() is None:
                    child.popen.kill()
                child.popen.wait(timeout=10)


@pytest.fixture
def sleeper() -> Iterator[str]:
    """The identity of a live process that is not this one (a child, killed by its handle)."""
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield identity_text(default_host(), proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=10)
