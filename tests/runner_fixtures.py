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


class PassSpawns(list[dict[str, object]]):
    """The triggered passes a test caused (a start trigger spawns nothing in tests)."""


@pytest.fixture(autouse=True)
def trigger_spawns(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> PassSpawns:
    """No test starts a real detached pass by accident: `add` and the other start triggers record
    the spawn here instead. Tests of the real spawn patch `gateway.spawn_detached` themselves."""
    calls = PassSpawns()
    if request.node.get_closest_marker("real_trigger") is not None:
        return calls

    def fake(
        state_dir: Path, config: object, config_path: Path | None, *, retried: int = 0
    ) -> object:
        calls.append({"state_dir": state_dir, "config_path": config_path, "retried": retried})
        return _Done()

    monkeypatch.setattr("jobsctl.triggers.spawn_pass", fake)
    return calls


class _Done:
    pid = 4_000_000

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None
