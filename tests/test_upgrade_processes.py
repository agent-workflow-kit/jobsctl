"""`run --loop` re-exec with a real process (interface.md, Upgrades).

The loop is started with Popen (so the kill guard knows its PID); the test stops it with SIGTERM and
falls back to killing that exact handle. Nothing else is signaled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl.leases import identity_text
from jobsctl.proc_host import default_host
from jobsctl.store_db import OpenMode, Store
from tests.manual_helpers import config_file

WORKER = Path(__file__).with_name("loop_worker.py")
WAIT = 60.0


class Loop:
    def __init__(self, tmp_path: Path) -> None:
        self.version_file = tmp_path / "version"
        self.version_file.write_text("1.0.0", encoding="utf-8")
        self.log = tmp_path / "starts.log"
        self.log.write_text("", encoding="utf-8")
        self.out = tmp_path / "loop.out"
        self.err = tmp_path / "loop.err"
        cfg = config_file(tmp_path, run_interval="1s", auto_run=False)
        env = dict(os.environ, LOOP_VERSION_FILE=str(self.version_file), LOOP_LOG=str(self.log))
        with open(self.out, "wb") as out, open(self.err, "wb") as err:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", str(WORKER), "--config", cfg, "--json", "run", "--loop"],
                env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                cwd=str(tmp_path), start_new_session=True,
            )  # fmt: skip

    def starts(self) -> list[list[str]]:
        return [line.split() for line in self.log.read_text(encoding="utf-8").splitlines()]

    def wait_starts(self, count: int) -> list[list[str]]:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            starts = self.starts()
            if len(starts) >= count:
                return starts
            assert self.proc.poll() is None, self.err.read_text(encoding="utf-8")
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for start {count}: {self.err.read_text(encoding='utf-8')}")

    def passes(self) -> int:
        return len(self.out.read_text(encoding="utf-8").splitlines())

    def wait_passes(self, count: int) -> None:
        deadline = time.monotonic() + WAIT
        while self.passes() < count:
            assert time.monotonic() < deadline, "timed out waiting for passes"
            time.sleep(0.05)

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=WAIT)
        except subprocess.TimeoutExpired:
            self.proc.kill()  # this exact handle
            self.proc.wait(timeout=WAIT)
            raise


@pytest.fixture
def loop(tmp_path: Path) -> Iterator[Loop]:
    started = Loop(tmp_path)
    try:
        yield started
    finally:
        if started.proc.poll() is None:
            started.proc.kill()  # the test failed before stopping it: exactly this handle
            started.proc.wait(timeout=WAIT)


def test_an_installed_version_change_re_execs_the_loop_in_place(loop: Loop) -> None:
    (first,) = loop.wait_starts(1)
    pid = loop.proc.pid
    assert first[0] == str(pid)
    identity = identity_text(default_host(), pid)
    loop.wait_passes(1)
    loop.version_file.write_text("2.0.0", encoding="utf-8")
    first, second = loop.wait_starts(2)[:2]
    assert second[0] == first[0] == str(pid)  # same PID: replaced, not duplicated
    assert second[1] == first[1]  # same parent: no fork, no second loop
    assert second[2] == "2.0.0"
    assert identity_text(default_host(), pid) == identity  # the lease identity is unchanged
    before = loop.passes()
    loop.wait_passes(before + 2)  # the new image keeps passing
    assert loop.proc.poll() is None
    assert loop.stop() == 0
    assert len(loop.starts()) == 2  # no flapping


def test_a_newer_schema_re_execs_once_and_does_not_flap(loop: Loop) -> None:
    loop.wait_starts(1)
    loop.wait_passes(1)
    with Store.open(mode=OpenMode.CLI) as store:
        with store.write() as tx:
            tx.execute(f"PRAGMA user_version = {store.known_version + 1}")
    first, second = loop.wait_starts(2)[:2]
    assert second[0] == first[0] == str(loop.proc.pid)
    before = loop.passes()
    loop.wait_passes(before + 3)  # the new image is no newer, so it runs on instead of re-exec'ing
    time.sleep(2.5)
    assert len(loop.starts()) == 2
    assert loop.stop() == 0
    assert "re-executing after an upgrade" in loop.err.read_text(encoding="utf-8")
    assert loop.out.read_text(encoding="utf-8").count("already_running") == loop.passes()


def test_sigterm_still_stops_the_re_exec_d_image(loop: Loop) -> None:
    loop.wait_starts(1)
    loop.wait_passes(1)  # the loop has read its starting version
    loop.version_file.write_text("3.0.0", encoding="utf-8")
    loop.wait_starts(2)
    loop.wait_passes(2)
    assert loop.stop() == 0
