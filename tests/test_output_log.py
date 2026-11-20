"""The full output log file: safe creation, the cap, failures that never reach the job."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl import output_log
from jobsctl.env_mask import masking_set_of
from jobsctl.output_log import FullOutputLog, find_log, log_name, marker_line
from jobsctl.store_dir import StatePaths

SECRET = "s3cr3t-token-VALUE"
MASKING = masking_set_of([SECRET])
JOB = "jb-aaaaaaaa"


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[StatePaths]:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    old = os.umask(0)  # the worst case: nothing is removed from a requested mode
    try:
        yield StatePaths(root)
    finally:
        os.umask(old)


def make(
    paths: StatePaths, stops: list[tuple[str, int]] | None = None, **kw: object
) -> FullOutputLog:
    sink = stops if stops is not None else []
    return FullOutputLog(
        paths,
        JOB,
        1,
        MASKING,
        on_stop=lambda r, w: sink.append((r, w)),
        **kw,  # type: ignore[arg-type]
    )


def mode(path: Path | str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_the_file_and_directory_are_private_even_under_umask_zero(paths: StatePaths) -> None:
    log = make(paths)
    log.feed(f"token {SECRET}\nmore\n".encode())
    log.finish()
    assert mode(paths.output) == 0o700
    assert mode(log.path) == 0o600
    assert log.path.read_text() == "token ***\nmore\n"


def test_a_restrictive_umask_does_not_remove_owner_bits(paths: StatePaths) -> None:
    os.umask(0o277)
    log = make(paths)
    log.feed(b"hello\n")
    log.finish()
    assert mode(paths.output) == 0o700 and mode(log.path) == 0o600
    assert log.path.read_text() == "hello\n"


def test_a_loose_existing_output_directory_is_tightened(paths: StatePaths) -> None:
    paths.output.mkdir(mode=0o755)
    os.chmod(paths.output, 0o755)
    log = make(paths)
    log.feed(b"x\n")
    log.finish()
    assert mode(paths.output) == 0o700


def test_no_file_is_created_for_a_job_without_output(paths: StatePaths) -> None:
    log = make(paths)
    log.finish()
    assert not paths.output.exists()


def test_the_name_is_built_from_the_id_and_attempt_only() -> None:
    assert log_name(JOB, 3) == f"{JOB}.3.log"
    assert log_name(JOB, 3, truncated=True) == f"{JOB}.3.log.truncated"
    for bad in ("../etc/passwd", "jb-AAAAAAAA", "jb-aaaaaaa", "", "jb-aaaaaaaa/x", "jb-aaaaaaaa\0"):
        with pytest.raises(ValueError):
            log_name(bad, 1)
    with pytest.raises(ValueError):
        log_name(JOB, 0)


def test_a_planted_symlink_at_the_name_is_replaced_never_followed(
    paths: StatePaths, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    paths.output.mkdir(mode=0o700)
    os.symlink(victim, paths.output / f"{JOB}.1.log")
    log = make(paths)
    log.feed(b"data\n")
    log.finish()
    assert victim.read_text() == "keep"
    assert not os.path.islink(log.path) and log.path.read_text() == "data\n"
    assert mode(log.path) == 0o600


def test_a_dangling_planted_symlink_is_not_followed_either(
    paths: StatePaths, tmp_path: Path
) -> None:
    paths.output.mkdir(mode=0o700)
    os.symlink(tmp_path / "does-not-exist", paths.output / f"{JOB}.1.log")
    log = make(paths)
    log.feed(b"data\n")
    log.finish()
    assert not (tmp_path / "does-not-exist").exists()
    assert log.path.read_text() == "data\n"


def test_a_planted_world_readable_file_is_replaced_by_a_private_one(paths: StatePaths) -> None:
    paths.output.mkdir(mode=0o700)
    planted = paths.output / f"{JOB}.1.log"
    planted.write_text("old")
    os.chmod(planted, 0o666)
    log = make(paths)
    log.feed(b"new\n")
    log.finish()
    assert log.path.read_text() == "new\n" and mode(log.path) == 0o600


def test_a_hard_linked_plant_is_not_written_through(paths: StatePaths, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    paths.output.mkdir(mode=0o700)
    os.link(victim, paths.output / f"{JOB}.1.log")
    log = make(paths)
    log.feed(b"data\n")
    log.finish()
    assert victim.read_text() == "keep"  # unlinked and re-created, not opened


def test_a_directory_at_the_name_stops_the_log_without_raising(paths: StatePaths) -> None:
    paths.output.mkdir(mode=0o700)
    (paths.output / f"{JOB}.1.log").mkdir()
    stops: list[tuple[str, int]] = []
    log = make(paths, stops)
    log.feed(b"data\n")
    log.feed(b"more\n")
    log.finish()
    assert stops == [("open_failed", 0)]


def test_an_output_directory_that_is_a_symlink_is_refused(
    paths: StatePaths, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, paths.output)
    stops: list[tuple[str, int]] = []
    log = make(paths, stops)
    log.feed(b"data\n")
    log.finish()
    assert os.listdir(outside) == [] and stops == [("open_failed", 0)]
    assert find_log(paths, JOB, 1) is None


def test_the_cap_writes_a_marker_stops_and_renames_the_file(paths: StatePaths) -> None:
    stops: list[tuple[str, int]] = []
    log = make(paths, stops, max_bytes=50)
    for _ in range(20):
        log.feed(b"0123456789\n")
    log.finish()
    assert not log.active
    final = paths.output / f"{JOB}.1.log.truncated"
    assert not (paths.output / f"{JOB}.1.log").exists()
    text = final.read_text()
    assert text.startswith("0123456789\n" * 4)
    assert text.endswith(marker_line(50))
    body = text[: -len(marker_line(50)) - 1]  # the cap is on the text; the marker follows
    assert len(body.encode()) == 50
    assert stops[0][0] == "cap_reached"
    assert mode(final) == 0o600


def test_the_cap_never_splits_a_character(paths: StatePaths) -> None:
    log = make(paths, max_bytes=5)
    log.feed("é\n".encode() * 10)
    log.finish()
    text = (paths.output / f"{JOB}.1.log.truncated").read_text()  # valid UTF-8
    assert text.startswith("é\né\n")


def test_what_is_found_by_show_says_whether_the_cap_was_hit(paths: StatePaths) -> None:
    ok = make(paths)
    ok.feed(b"abc\n")
    ok.finish()
    found = find_log(paths, JOB, 1)
    assert found is not None and found.truncated is False and found.bytes == 4
    assert found.path == str(paths.output / f"{JOB}.1.log")
    capped = FullOutputLog(paths, JOB, 2, MASKING, max_bytes=3)
    capped.feed(b"abcdefgh\n")
    capped.finish()
    found = find_log(paths, JOB, 2)
    assert found is not None and found.truncated is True


def test_find_log_ignores_a_symlink_and_a_foreign_kind_of_file(
    paths: StatePaths, tmp_path: Path
) -> None:
    paths.output.mkdir(mode=0o700)
    target = tmp_path / "t"
    target.write_text("x")
    os.symlink(target, paths.output / f"{JOB}.1.log")
    (paths.output / f"{JOB}.2.log").mkdir()
    assert find_log(paths, JOB, 1) is None and find_log(paths, JOB, 2) is None
    assert find_log(paths, JOB, 3) is None


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
def test_a_write_error_stops_the_log_and_never_raises(
    paths: StatePaths, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    stops: list[tuple[str, int]] = []
    log = make(paths, stops)
    log.feed(b"first line\n" * 5)

    def failing(fd: int, data: object) -> int:
        raise OSError(code, "no")

    monkeypatch.setattr(output_log, "_os_write", failing)
    log.feed(b"second line\n" * 5)
    log.feed(b"third line\n")
    log.finish()
    expected = "write_failed" if code == errno.EIO else "disk_full"
    assert [r for r, _ in stops] == [expected]  # reported once
    assert mode(log.path) == 0o600  # what was written stays, private


def test_a_disk_full_at_the_first_write_is_reported_once(
    paths: StatePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(fd: int, data: object) -> int:
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(output_log, "_os_write", failing)
    stops: list[tuple[str, int]] = []
    log = make(paths, stops)
    log.feed(b"x\n" * 100)
    log.finish()
    assert [r for r, _ in stops] == ["disk_full"]


def test_a_killed_writer_leaves_a_private_partial_file(paths: StatePaths) -> None:
    log = make(paths)
    log.feed(("line of text\n" * 10).encode())  # no finish(): the shim was killed
    assert mode(log.path) == 0o600
    assert log.path.read_text().startswith("line of text\n")


def test_binary_and_invalid_utf8_output_is_written_as_escaped_text(paths: StatePaths) -> None:
    log = make(paths)
    log.feed(b"\x00\x01\xff\xfe binary \x1b[31mred\x1b[0m\n\x80\x81")
    log.finish()
    text = log.path.read_bytes().decode("utf-8")  # always valid UTF-8
    assert "\\xff\\xfe" in text and "\x1b" not in text and "\x00" not in text
    assert "red" in text


def test_a_secret_in_a_flood_is_masked_in_the_file(paths: StatePaths) -> None:
    log = make(paths)
    data = (f"line {SECRET} and \x1b{SECRET}\n" * 5000).encode()
    for i in range(0, len(data), 997):  # chunk edges everywhere
        log.feed(data[i : i + 997])
    log.finish()
    text = log.path.read_text()
    assert SECRET not in text and "3cr3t" not in text
    assert text.count("***") >= 10000


def test_an_overflow_stops_the_log_instead_of_writing_unsafe_text(paths: StatePaths) -> None:
    masking = masking_set_of(["abcdefgh", "efghijkl", "ijklmnop", "mnopabcd"])
    stops: list[tuple[str, int]] = []
    log = FullOutputLog(
        paths, JOB, 1, masking, on_stop=lambda r, w: stops.append((r, w)), max_pending=40
    )
    for _ in range(100):
        log.feed(b"abcdefghijklmnop" * 4)
    log.finish()
    assert [r for r, _ in stops] == ["overflow"]
    if log.path.exists():
        assert "abcdefgh" not in log.path.read_text()
