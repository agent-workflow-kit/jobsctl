"""`jobsctl browse` as a real process on a pseudo-terminal: start, quit, signals, restoration.

The child is started on a pty of our own (so these tests run the same with no terminal, as in CI),
recorded in the `procs` fixture and signaled by its exact PID. Every wait is bounded.
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")  # the extra-less behavior is covered in test_browse_cli.py

from tests.manual_helpers import add_job  # noqa: E402
from tests.spike_helpers import Procs, linux_only  # noqa: E402

pytestmark = linux_only


@pytest.fixture
def procs(spike_procs: Procs) -> Procs:
    return spike_procs


ALT_SCREEN_ON = b"\x1b[?1049h"
ALT_SCREEN_OFF = b"\x1b[?1049l"


class Terminal:
    def __init__(self, procs: Procs, *args: str) -> None:
        master, slave = pty.openpty()
        termios.tcsetwinsize(master, (40, 140)) if hasattr(termios, "tcsetwinsize") else None
        import fcntl

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", COLUMNS="140", LINES="40")
        self.master = master
        self.proc = procs.popen(
            [sys.executable, "-I", "-m", "jobsctl", "browse", *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.seen = b""

    def read_until(self, needle: bytes, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self.seen:
            left = deadline - time.monotonic()
            assert left > 0, f"timed out waiting for {needle!r}; saw {self.seen[-300:]!r}"
            if select.select([self.master], [], [], min(left, 0.2))[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.seen += chunk

    def drain(self) -> None:
        while select.select([self.master], [], [], 0.2)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.seen += chunk

    def wait(self, timeout: float = 30.0) -> int:
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None:
            assert time.monotonic() < deadline, f"did not exit; saw {self.seen[-300:]!r}"
            self.drain()
        self.drain()
        os.close(self.master)
        return int(self.proc.returncode)


@pytest.fixture
def terminal(procs: Procs, capsys: pytest.CaptureFixture[str]) -> Terminal:
    add_job(capsys)
    return Terminal(procs)


def test_q_quits_with_exit_0_and_restores_the_screen(terminal: Terminal) -> None:
    terminal.read_until(ALT_SCREEN_ON)
    terminal.read_until(b"global:")
    os.write(terminal.master, b"q")
    assert terminal.wait() == 0
    assert ALT_SCREEN_OFF in terminal.seen and b"Traceback" not in terminal.seen


@pytest.mark.parametrize(
    ("number", "code"), [(signal.SIGTERM, 130), (signal.SIGHUP, 130), (signal.SIGINT, 130)]
)
def test_signals_exit_130_after_restoring_the_terminal(
    procs: Procs, capsys: pytest.CaptureFixture[str], number: signal.Signals, code: int
) -> None:
    add_job(capsys)
    terminal = Terminal(procs)
    terminal.read_until(b"global:")
    os.kill(terminal.proc.pid, number)
    assert terminal.wait() == code
    assert ALT_SCREEN_OFF in terminal.seen and b"Traceback" not in terminal.seen
    assert terminal.seen.rindex(ALT_SCREEN_OFF) > terminal.seen.rindex(ALT_SCREEN_ON)


def test_the_result_file_is_written_on_exit(
    procs: Procs, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job_id = add_job(capsys)
    result = tmp_path / "result.json"
    terminal = Terminal(procs, "--result-file", str(result))
    terminal.read_until(b"global:")
    assert not result.exists()
    os.write(terminal.master, b"o")
    assert terminal.wait() == 0
    assert json.loads(result.read_text()) == {"intent": "open", "selected": [job_id]}
