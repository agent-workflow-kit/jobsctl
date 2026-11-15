"""Exit mapping (interface.md, Exit record; spike A6)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from jobsctl.shim_exit import (
    ExitRecord,
    Outcome,
    UnknownWaitStatus,
    exit_record,
    forced_outcome,
    natural_outcome,
    post_hook,
)


def _wait(code: str) -> os.waitid_result:
    proc = subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.DEVNULL)
    result = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
    assert result is not None
    proc.wait()
    return result


@pytest.mark.parametrize("code", [0, 1, 13, 143, 255])
def test_exit_codes_pass_through(code: int) -> None:
    got = _wait(f"import sys; sys.exit({code})")
    assert exit_record(got.si_code, got.si_status) == ExitRecord("exited", code=code)


def test_death_by_signal_is_signaled_with_the_signal_number() -> None:
    got = _wait("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    assert exit_record(got.si_code, got.si_status) == ExitRecord("signaled", signal=9)


def test_a_shell_reporting_a_killed_child_is_an_ordinary_exit_143() -> None:
    got = _wait(
        "import subprocess; raise SystemExit(subprocess.call(['/bin/sh', '-c', 'exit 143']))"
    )
    assert exit_record(got.si_code, got.si_status) == ExitRecord("exited", code=143)


def test_a_core_dump_sets_core() -> None:
    assert exit_record(os.CLD_DUMPED, 11) == ExitRecord("signaled", signal=11, core=True)


@pytest.mark.parametrize("si_code", [0, os.CLD_STOPPED, os.CLD_CONTINUED, 99])
def test_other_wait_results_are_refused(si_code: int) -> None:
    with pytest.raises(UnknownWaitStatus):
        exit_record(si_code, 0)


@pytest.mark.parametrize(
    ("record", "outcome"),
    [
        (ExitRecord("exited", code=0), Outcome("done", "exit")),
        (ExitRecord("exited", code=13), Outcome("held", None, "exit_13")),
        (ExitRecord("exited", code=1), Outcome("failed", "exit")),
        (ExitRecord("exited", code=255), Outcome("failed", "exit")),
        (ExitRecord("exited", code=143), Outcome("failed", "exit")),
        (ExitRecord("signaled", signal=15), Outcome("failed", "signaled")),
        (ExitRecord("signaled", signal=13, core=True), Outcome("failed", "signaled")),
    ],
)
def test_natural_outcome(record: ExitRecord, outcome: Outcome) -> None:
    assert natural_outcome(record) == outcome


def test_force_request_overrides_whatever_the_exit() -> None:
    assert forced_outcome() == Outcome("canceled", "forced")


def test_post_hook_names() -> None:
    assert post_hook(Outcome("done", "exit")) == "post-finish"
    assert post_hook(Outcome("failed", "exit")) == "post-fail"
    assert post_hook(Outcome("held", None, "exit_13")) == "post-held"
    assert post_hook(forced_outcome()) == "post-cancel"


def test_exit_record_json_round_trip() -> None:
    rec = ExitRecord("signaled", signal=9, core=True)
    assert ExitRecord.from_json(rec.as_json()) == rec
    assert rec.as_json() == {"kind": "signaled", "code": None, "signal": 9, "core": True}


def test_an_unobserved_exit_is_lost() -> None:
    assert natural_outcome(ExitRecord("unknown")) == Outcome("failed", "lost")
