"""The opt-in full output log of an attempt (interface.md, Full output log; D55).

`<state dir>/output/<job id>.<n>.log` holds the masked, sanitized combined output of attempt `n`
(`output_stream.StreamMasker`: the pipeline of the tail, applied to the whole stream). It is a
secret-bearing file, so it is created with care:

- `output/` is opened by descriptor without following a symlink, must be owned by us and on the
  state directory's device, and is tightened to 0700 (created 0700 whatever the umask);
- the file is created relative to that descriptor with `O_CREAT | O_EXCL | O_NOFOLLOW` and mode
  0600 (`fchmod` afterwards, as a restrictive umask can remove owner bits), so a planted symlink
  or file is never written through: a leftover name is unlinked (never followed) and created
  again once, else the log is not written; the descriptor must be a regular file of ours with a
  single link;
- its name is built from the job id and the attempt number only, never from job data.

The log never fails the job: every error (cannot open, disk full, over the cap, nothing safe to
write) stops the log and is reported once through `on_stop`; the job's output is still read.
At the cap (`max_bytes` of written text) it writes a marker line, stops, and renames the file to
`<id>.<n>.log.truncated` so a reader (`show`) can tell without reading it.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from jobsctl.env_mask import MaskingSet
from jobsctl.output_stream import StreamMasker, StreamOverflow
from jobsctl.store_dir import StatePaths

DEFAULT_MAX_BYTES: Final = 16 * 1024 * 1024
MAX_MAX_BYTES: Final = 1 << 30
TRUNCATED_SUFFIX: Final = ".truncated"
# The file names of one job's logs: `<id>.<n>.log`, or `.log.truncated` once the cap was hit.
LOG_FILE_RE: Final = re.compile(r"(jb-[a-z2-7]{8})\.[0-9]+\.log(?:\.truncated)?")
_ID_RE: Final = re.compile(r"jb-[a-z2-7]{8}")
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _os_write(fd: int, data: memoryview) -> int:
    return os.write(fd, data)


def marker_line(max_bytes: int) -> str:
    return f"[jobsctl: full output log truncated at {max_bytes} bytes (full_output_max_bytes)]\n"


def log_name(job_id: str, attempt: int, *, truncated: bool = False) -> str:
    """The file name for a job id and attempt number; both are checked, so no job data can
    steer the path."""
    if not _ID_RE.fullmatch(job_id) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("not a job id and attempt number")
    return f"{job_id}.{attempt}.log" + (TRUNCATED_SUFFIX if truncated else "")


def _open_output_dir(paths: StatePaths, *, create: bool) -> int:
    """A descriptor of `output/`, created when `create`; OSError when it is not safe."""
    root = os.open(paths.root, _DIR_FLAGS)
    try:
        root_st = os.fstat(root)
        if root_st.st_uid != os.geteuid():
            raise PermissionError(errno.EPERM, "the state directory is not ours")
        if create:
            try:
                os.mkdir(paths.output.name, 0o700, dir_fd=root)
            except FileExistsError:
                pass
        fd = os.open(paths.output.name, _DIR_FLAGS, dir_fd=root)
    finally:
        os.close(root)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or st.st_dev != root_st.st_dev:
            raise PermissionError(errno.EPERM, "output/ is not ours or on another device")
        if create and stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)  # the umask, or a loose directory of ours
    except BaseException:
        os.close(fd)
        raise
    return fd


def _create_file(dir_fd: int, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except FileExistsError:
        os.unlink(name, dir_fd=dir_fd)  # a symlink is removed itself; a directory is refused
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            raise PermissionError(errno.EPERM, "not a private regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass(frozen=True)
class LogPointer:
    path: str
    bytes: int
    truncated: bool


def find_log(paths: StatePaths, job_id: str, attempt: int) -> LogPointer | None:
    """The log of an attempt as `show` reports it, or None when there is none (a symlink, a
    foreign or non-regular file, a missing or unsafe directory all read as none). Read-only."""
    try:
        dir_fd = _open_output_dir(paths, create=False)
    except OSError:
        return None
    try:
        for truncated in (True, False):
            name = log_name(job_id, attempt, truncated=truncated)
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
                return LogPointer(str(paths.output / name), st.st_size, truncated)
        return None
    finally:
        os.close(dir_fd)


StopHook = Callable[[str, int], None]


class FullOutputLog:
    """Writes the masked stream of one attempt to its file. `feed` and `finish` never raise."""

    def __init__(
        self,
        paths: StatePaths,
        job_id: str,
        attempt: int,
        masking: MaskingSet,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        on_stop: StopHook = lambda reason, written: None,
        max_pending: int | None = None,
    ) -> None:
        self._paths = paths
        self._name = log_name(job_id, attempt)
        self._masker = (
            StreamMasker(masking)
            if max_pending is None
            else StreamMasker(masking, max_pending=max_pending)
        )
        self._max = max_bytes
        self._on_stop = on_stop
        self._dir_fd: int | None = None
        self._fd: int | None = None
        self._opened = False
        self._stopped = False
        self._written = 0
        self._ends_with_newline = True

    @property
    def path(self) -> Path:
        return self._paths.output / self._name

    @property
    def active(self) -> bool:
        return not self._stopped

    def feed(self, data: bytes) -> None:
        if self._stopped:
            return
        try:
            self._write(self._masker.feed(data))
        except StreamOverflow:
            self._stop("overflow")
        except OSError as exc:
            self._stop(
                "disk_full"
                if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG)
                else "write_failed"
            )
        except Exception:  # the log must never fail the job
            self._stop("internal")

    def finish(self) -> None:
        """Write the rest and close. Safe to call twice."""
        if not self._stopped:
            try:
                self._write(self._masker.finish())
            except OSError as exc:
                self._stop(
                    "disk_full"
                    if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG)
                    else "write_failed"
                )
            except Exception:
                self._stop("internal")
        self._close()

    # -- internals ---------------------------------------------------------------------------

    def _write(self, text: str) -> None:
        if not text or self._stopped:
            return
        data = text.encode("utf-8")
        room = self._max - self._written
        if len(data) > room:
            data = data[: max(room, 0)].decode("utf-8", "ignore").encode("utf-8")
            self._put(data)
            self._truncate()
            return
        self._put(data)

    def _put(self, data: bytes) -> None:
        if not data:
            return
        if not self._opened:
            self._open()
        assert self._fd is not None
        view = memoryview(data)
        while view:
            n = _os_write(self._fd, view)
            view = view[n:]
        self._written += len(data)
        self._ends_with_newline = data.endswith(b"\n")

    def _open(self) -> None:
        self._opened = True
        try:
            self._dir_fd = _open_output_dir(self._paths, create=True)
            self._fd = _create_file(self._dir_fd, self._name)
        except OSError as exc:
            self._close()
            self._stopped = True
            self._on_stop(
                "disk_full" if exc.errno in (errno.ENOSPC, errno.EDQUOT) else "open_failed", 0
            )
            raise _Stopped from exc

    def _truncate(self) -> None:
        """The cap is reached: the marker, then no more writing; the name says so."""
        marker = marker_line(self._max)
        tail = marker if self._ends_with_newline else "\n" + marker
        try:
            self._put(tail.encode("utf-8"))
            if self._dir_fd is not None:
                os.rename(
                    self._name,
                    self._name + TRUNCATED_SUFFIX,
                    src_dir_fd=self._dir_fd,
                    dst_dir_fd=self._dir_fd,
                )
        finally:
            self._stop("cap_reached")

    def _stop(self, reason: str) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._close()
        self._on_stop(reason, self._written)

    def _close(self) -> None:
        for attr in ("_fd", "_dir_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                setattr(self, attr, None)
                try:
                    os.close(fd)
                except OSError:
                    pass


class _Stopped(Exception):
    """`_open` already stopped the log and reported it."""
