"""A real `jobsctl _deliver` process runs async hooks and redelivers (Pass step 5, Delivery)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsctl.config_files import load_config
from jobsctl.spawn import spawn_internal
from jobsctl.store_dir import resolve_state_dir
from tests.delivery_helpers import open_store, pending, ran, recorder
from tests.helpers import run_cli
from tests.manual_helpers import add_job, config_file, log_records, outbox


def test_a_spawned_deliver_runs_async_hooks_and_exits(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cfg = config_file(
        tmp_path,
        hooks={"post-held": [{"name": "w", "cmd": recorder(out), "async": True}]},
    )
    job_id = add_job(capsys, config=cfg)
    assert run_cli(capsys, "--config", cfg, "--json", "hold", job_id).code == 0
    assert ran(out) == []  # the command left the async event to `_deliver`
    with open_store() as store:
        assert len(pending(store, is_async=True)) == 1
    config = load_config(Path(cfg))
    proc = spawn_internal(resolve_state_dir(), config, Path(cfg), "_deliver")
    assert proc.wait(timeout=60) == 0
    (file,) = ran(out)
    assert json.loads(file.read_text())["event_id"] == outbox()[0]["event_id"]
    with open_store() as store:
        assert pending(store) == []
        assert store.query_one("SELECT 1 FROM leases WHERE name = 'deliver'") is None
    (record,) = log_records("hook_ran")
    assert record["outcome"] == "allow"
