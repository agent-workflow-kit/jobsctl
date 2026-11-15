"""A real cron daemon runs the installed entry (spike A2). Run by hand, never in CI:

    JOBSCTL_LIVE_CRON=1 uv run pytest -m live tests/test_cron_live.py

Cron reads the user's real crontab, so this test cannot use a private one. It is therefore opt-in
(``JOBSCTL_LIVE_CRON=1``), refuses to run when the crontab already holds a jobsctl block, saves the
crontab's exact bytes to a file before touching it and writes them back in a ``finally`` (or
removes the crontab when there was none), then checks the restore byte for byte. Everything else
is private: the state and config directories (written into the cron entry as ``XDG_*``), the job
and its marker files live under pytest's temporary directory, and ``auto_run: false`` keeps any
trigger but cron from starting the job. The test starts no process it does not wait for.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobsctl import gateway

pytestmark = pytest.mark.live

WAIT_FOR_CRON = 180.0


def _jobsctl_script() -> Path:
    script = Path(sys.executable).parent / "jobsctl"
    if not script.exists():
        pytest.skip(f"no jobsctl console script next to {sys.executable}")
    return script


def _restore(original: bytes | None) -> None:
    if original is None:
        gateway.crontab_remove()
    else:
        gateway.crontab_install(original)


@pytest.mark.skipif(os.environ.get("JOBSCTL_LIVE_CRON") != "1", reason="set JOBSCTL_LIVE_CRON=1")
def test_a_real_cron_pass_starts_a_queued_job_and_its_shim_outlives_the_pass(
    tmp_path: Path,
) -> None:
    script = _jobsctl_script()
    state, config = tmp_path / "state", tmp_path / "config"
    (config / "jobsctl").mkdir(parents=True)
    (config / "jobsctl" / "config.yaml").write_text("auto_run: false\n")
    env = {
        **os.environ,
        "PATH": f"{script.parent}:/usr/local/bin:/usr/bin:/bin",
        "XDG_STATE_HOME": str(state),
        "XDG_CONFIG_HOME": str(config),
    }

    def jobsctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *args], env=env, capture_output=True, text=True, timeout=60, check=False
        )

    original = gateway.crontab_read()
    assert original is None or b"# jobsctl managed" not in original, (
        "the crontab already has a jobsctl block; uninstall it first"
    )
    backup = tmp_path / "crontab.backup"
    if original is not None:
        backup.write_bytes(original)  # kept on disk even if this test is killed

    marker, pid_file = tmp_path / "done", tmp_path / "job.pid"
    try:
        installed = jobsctl("--json", "cron", "install")
        assert installed.returncode == 0, installed.stderr
        assert json.loads(installed.stdout)["changed"] is True

        added = jobsctl(
            "--json", "add", "--", "sh", "-c", f"echo $$ > {pid_file}; sleep 8; touch {marker}"
        )
        assert added.returncode == 0, added.stderr
        job_id = json.loads(added.stdout)["job"]["id"]
        assert not pid_file.exists()  # nothing but cron may start it

        deadline = time.monotonic() + WAIT_FOR_CRON
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(1)
        # The pass that started the job exited long before the 8 s job ended, so the marker
        # proves the shim (not the pass) saw the job through.
        assert marker.exists(), "cron did not start the job in time"

        # `show` is not implemented yet: read the status from the private database.
        db = sqlite3.connect(f"file:{state / 'jobsctl' / 'state.db'}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT status FROM jobs WHERE id = ?", [job_id]).fetchone()
        finally:
            db.close()
        assert row == ("done",)
    finally:
        cleanup = jobsctl("cron", "uninstall")
        try:
            _restore(original)
        finally:
            assert cleanup.returncode == 0, cleanup.stderr
    assert gateway.crontab_read() == original
