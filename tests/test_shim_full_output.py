"""The full output log through the shim (in process) and `show`'s pointer to it."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from jobsctl import output_log
from jobsctl import shim as shim_module
from jobsctl.config import Config
from jobsctl.env_file import load_env_file
from jobsctl.store_db import OpenMode, Store
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against
from tests.helpers import run_cli
from tests.shim_helpers import JOB, job, log_records, make_shim, seed_claimed, tail_of

SECRET = "supersecretvalue123"
OUT = JOB + ".1.log"


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def secret_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An `env_file` defining TOKEN (the masked value), accepted by the shim's safety checks."""
    d = tmp_path / "secrets"
    d.mkdir(mode=0o700)
    f = d / "env"
    f.write_text(f"TOKEN={SECRET}\n")
    f.chmod(0o600)
    monkeypatch.setattr(
        shim_module, "load_env_file", partial(load_env_file, check_ancestors_from=tmp_path)
    )
    ENV_FILE[:] = [str(f)]
    return str(f)


@pytest.fixture(autouse=True)
def _validated(validated_records: None) -> None:
    return None


def out_dir() -> Path:
    return resolve_state_dir() / "output"


ENV_FILE: list[str] = []


def run_job(store: Store, script: str, config: dict[str, Any] | None = None) -> None:
    seed_claimed(store, ["sh", "-c", script], queue="q", pass_env=["TOKEN"])
    cfg = Config.model_validate({"env_file": ENV_FILE[0], **(config or {})})
    make_shim(store, cfg).run()
    assert job(store)["status"] == "done"


def show_json(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    res = run_cli(capsys, "--json", "show", JOB, *args)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/show.json", doc)
    return doc


def test_off_by_default_nothing_is_written_and_show_has_no_log_field(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_job(store, "echo hello")
    assert not out_dir().exists()
    assert "log" not in show_json(capsys)["output"]


def test_on_the_file_holds_the_masked_output_and_show_points_to_it(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_job(
        store,
        'echo "start $TOKEN"; printf "\\033[31mred\\033[0m\\n"; echo "end"',
        {"full_output_log": True},
    )
    file = out_dir() / OUT
    assert file.read_text() == "start ***\nred\nend\n"
    assert stat.S_IMODE(os.lstat(file).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(out_dir()).st_mode) == 0o700
    log = show_json(capsys)["output"]["log"]
    assert log == {"path": str(file), "bytes": file.stat().st_size, "truncated": False}
    assert tail_of(store) == "start ***\nred\nend\n"  # the tail is unchanged


def test_show_human_output_names_the_file(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    run_job(store, "echo hello", {"full_output_log": True})
    res = run_cli(capsys, "show", JOB)
    assert f"full output log: {out_dir() / OUT} (6 bytes)" in res.out


def test_per_queue_opt_in_overrides_the_global_off(store: Store) -> None:
    run_job(store, "echo hi", {"queues": {"q": {"full_output_log": True}}})
    assert (out_dir() / OUT).read_text() == "hi\n"


def test_per_queue_opt_out_overrides_the_global_on(store: Store) -> None:
    run_job(
        store,
        "echo hi",
        {"full_output_log": True, "queues": {"q": {"full_output_log": False}}},
    )
    assert not out_dir().exists()


def test_the_secret_is_never_in_the_file_under_umask_zero(store: Store) -> None:
    old = os.umask(0)
    try:
        run_job(
            store,
            'i=0; while [ $i -lt 2000 ]; do printf "x$TOKEN\\033$TOKEN y\\n"; i=$((i+1)); done',
            {"full_output_log": True},
        )
    finally:
        os.umask(old)
    file = out_dir() / OUT
    text = file.read_text()
    assert SECRET not in text and "3cr3t" not in text
    assert stat.S_IMODE(os.lstat(file).st_mode) == 0o600


def test_the_cap_stops_the_log_records_it_and_the_job_still_succeeds(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_job(
        store,
        "yes 0123456789abcdef | head -c 200000",
        {"full_output_log": True, "full_output_max_bytes": 1000, "tail_bytes": 2048},
    )
    capped = out_dir() / (OUT + ".truncated")
    assert capped.exists() and not (out_dir() / OUT).exists()
    assert capped.read_text().endswith(output_log.marker_line(1000))
    [rec] = log_records("full_output_log_stopped")
    assert rec["level"] == "warn" and rec["details"]["reason"] == "cap_reached"
    log = show_json(capsys)["output"]["log"]
    assert log["truncated"] is True and log["path"] == str(capped)
    assert tail_of(store) is not None and "0123456789abcdef" in (tail_of(store) or "")


def test_a_disk_full_error_never_fails_the_job_and_is_logged(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(fd: int, data: object) -> int:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(output_log, "_os_write", failing)
    run_job(store, "echo one; echo two", {"full_output_log": True})
    assert tail_of(store) == "one\ntwo\n"
    [rec] = log_records("full_output_log_stopped")
    assert rec["details"]["reason"] == "disk_full"


def test_a_planted_symlink_for_the_log_is_not_followed(store: Store, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    out_dir().mkdir(mode=0o700)
    os.symlink(victim, out_dir() / OUT)
    run_job(store, "echo hi", {"full_output_log": True})
    assert victim.read_text() == "keep"
    assert (out_dir() / OUT).read_text() == "hi\n" and not os.path.islink(out_dir() / OUT)


def test_an_unusable_output_directory_does_not_fail_the_job(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, out_dir())
    run_job(store, "echo hi", {"full_output_log": True})
    assert os.listdir(outside) == []
    [rec] = log_records("full_output_log_stopped")
    assert rec["details"]["reason"] == "open_failed"


def test_a_job_with_no_output_leaves_no_file(store: Store) -> None:
    run_job(store, "true", {"full_output_log": True})
    assert not out_dir().exists()


def test_a_binary_flood_is_valid_utf8_text_in_the_file(store: Store) -> None:
    run_job(store, "head -c 100000 /dev/urandom", {"full_output_log": True})
    (out_dir() / OUT).read_bytes().decode("utf-8")


def test_invalid_values_of_the_new_keys_are_refused() -> None:
    for bad in ({"full_output_max_bytes": 0}, {"full_output_max_bytes": 1 << 31},
                {"full_output_log": "yes"}, {"queues": {"q": {"full_output_log": None}}}):  # fmt: skip
        with pytest.raises(Exception):
            Config.model_validate(bad)


def test_defaults() -> None:
    cfg = Config()
    settings = cfg.queue("anything")
    assert settings.full_output_log is False and settings.full_output_max_bytes == 16 * 1024 * 1024
