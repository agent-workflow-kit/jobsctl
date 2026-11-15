"""The result file: atomic, private, never written through a planted name."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jobsctl.result_file import (
    ResultFileError,
    ResultRecord,
    ResultTail,
    delete_result,
    read_result,
    result_name,
    write_result,
)
from jobsctl.shim_exit import ExitRecord

JOB = "jb-aaaaaaaa"


def record(**over: object) -> ResultRecord:
    values: dict[str, object] = {
        "job_id": JOB,
        "attempt": 2,
        "claim_id": "c" * 32,
        "status": "failed",
        "reason": "exit",
        "hold_reason": None,
        "exit": ExitRecord("exited", code=3),
        "leftovers": 1,
        "tail": ResultTail("out \n", 7, True),
        "ended_at": "2026-01-01T00:00:00.000Z",
    }
    values.update(over)
    return ResultRecord(**values)  # type: ignore[arg-type]


@pytest.fixture
def shims(tmp_path: Path) -> Path:
    d = tmp_path / "shims"
    d.mkdir(mode=0o700)
    return d


def test_write_then_read_round_trips_and_is_private(shims: Path) -> None:
    name = write_result(shims, record())
    assert name == result_name(JOB, 2) == f"{JOB}.2.result"
    st = os.lstat(shims / name)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert read_result(shims, JOB, 2) == record()
    assert [p.name for p in shims.iterdir()] == [name]  # no temporary file is left


def test_a_loose_umask_does_not_loosen_the_mode(shims: Path) -> None:
    old = os.umask(0o000)
    try:
        write_result(shims, record())
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.lstat(shims / result_name(JOB, 2)).st_mode) == 0o600


def test_a_symlink_planted_at_the_final_name_is_replaced_not_followed(
    shims: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    os.symlink(victim, shims / result_name(JOB, 2))
    write_result(shims, record())
    assert victim.read_text() == "precious"
    assert not os.path.islink(shims / result_name(JOB, 2))
    assert read_result(shims, JOB, 2) == record()


def test_a_directory_at_the_final_name_is_an_error_and_leaves_no_temp(shims: Path) -> None:
    (shims / result_name(JOB, 2)).mkdir()
    with pytest.raises(ResultFileError):
        write_result(shims, record())
    assert [p.name for p in shims.iterdir()] == [result_name(JOB, 2)]


def test_a_symlinked_shims_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(ResultFileError):
        write_result(link, record())
    assert list(real.iterdir()) == []


def test_reading_refuses_a_symlink(shims: Path, tmp_path: Path) -> None:
    good = shims / "elsewhere"
    write_result(shims, record())
    os.rename(shims / result_name(JOB, 2), good)
    os.symlink(good, shims / result_name(JOB, 2))
    with pytest.raises(ResultFileError):
        read_result(shims, JOB, 2)


def test_reading_refuses_a_fifo_without_blocking(shims: Path) -> None:
    os.mkfifo(shims / result_name(JOB, 2), 0o600)
    with pytest.raises(ResultFileError):
        read_result(shims, JOB, 2)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666, 0o400])
def test_reading_refuses_a_loose_mode(shims: Path, mode: int) -> None:
    write_result(shims, record())
    os.chmod(shims / result_name(JOB, 2), mode)
    with pytest.raises(ResultFileError):
        read_result(shims, JOB, 2)


def test_reading_refuses_an_oversized_file(shims: Path) -> None:
    path = shims / result_name(JOB, 2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.ftruncate(fd, 3 * 1024 * 1024)
    os.close(fd)
    with pytest.raises(ResultFileError):
        read_result(shims, JOB, 2)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"[]",
        b"{}",
        b'{"version": 2}',
    ],
)
def test_reading_refuses_damaged_content(shims: Path, content: bytes) -> None:
    path = shims / result_name(JOB, 2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, content)
    os.close(fd)
    with pytest.raises(ResultFileError):
        read_result(shims, JOB, 2)


def test_reading_refuses_another_jobs_or_attempts_content(shims: Path) -> None:
    write_result(shims, record())
    os.rename(shims / result_name(JOB, 2), shims / result_name(JOB, 3))
    with pytest.raises(ResultFileError):
        read_result(shims, JOB, 3)


def test_a_missing_file_reads_as_none(shims: Path) -> None:
    assert read_result(shims, JOB, 1) is None


def test_delete_removes_the_name_and_ignores_a_missing_file(shims: Path, tmp_path: Path) -> None:
    write_result(shims, record())
    delete_result(shims, JOB, 2)
    delete_result(shims, JOB, 2)
    assert list(shims.iterdir()) == []


def test_delete_of_a_symlink_removes_the_link_only(shims: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    os.symlink(victim, shims / result_name(JOB, 2))
    delete_result(shims, JOB, 2)
    assert victim.read_text() == "precious"
    assert list(shims.iterdir()) == []
