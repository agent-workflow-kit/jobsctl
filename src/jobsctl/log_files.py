"""Opening and appending to ``log.jsonl`` and ``audit.jsonl`` (interface.md, Audit and log).

The files are append-only, mode 0600, regular files owned by the user. A symlink, a FIFO or
device, a directory or a foreign owner is refused (the same rules as ``state.db``); a file
with group or other permissions is tightened. Appends happen under an exclusive ``flock`` that
the caller holds from the sequence-number increment to the write (see ``log_writer``), so
records are in ``seq`` order and one writer's line never interleaves with another's.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jobsctl import gateway
from jobsctl.errors import JobsctlError

_REOPEN_TRIES = 3


def _unsafe(path: Path, why: str) -> JobsctlError:
    return JobsctlError("internal", f"state path {path} is unsafe: {why}")


def open_append(path: Path) -> int:
    """Open (creating, 0600) ``path`` for reading and appending; return the descriptor.

    ``O_NONBLOCK`` keeps a FIFO from blocking the open (``fstat`` then rejects it); it has no
    effect on a regular file.
    """
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _unsafe(path, f"cannot open ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _unsafe(path, "not a regular file")
        if st.st_uid != os.geteuid():
            raise _unsafe(path, "owned by another user")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)  # also adds back owner bits a restrictive umask removed
    except OSError as exc:
        os.close(fd)
        raise _unsafe(path, exc.strerror or "cannot check it") from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def _same_file(fd: int, path: Path) -> bool:
    """Is ``path`` still the file ``fd`` refers to (it may have been rotated away)?"""
    try:
        on_disk = os.lstat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (on_disk.st_ino, on_disk.st_dev) == (held.st_ino, held.st_dev)


def _open_locked(path: Path) -> int:
    """Open ``path`` and take its lock; reopen if the file was rotated away meanwhile."""
    fd = -1
    for _ in range(_REOPEN_TRIES):
        fd = open_append(path)
        try:
            gateway.flock_exclusive(fd)
            if _same_file(fd, path):
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
    # The file kept moving: lock whatever is at the path now rather than loop forever.
    fd = open_append(path)
    try:
        gateway.flock_exclusive(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def locked(path: Path) -> Iterator[int]:
    """Yield a descriptor of ``path`` held under an exclusive lock, released on exit.

    If the file was renamed or removed (external rotation) between the open and the lock, the
    new file at ``path`` is opened and locked instead, so the lock protects the file that the
    append reaches.
    """
    fd = _open_locked(path)
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            gateway.funlock(fd)
        os.close(fd)


def append_line(fd: int, data: bytes, *, durable: bool) -> None:
    """Append ``data`` (one newline-terminated record) to the locked file ``fd``.

    A last line without its newline (a writer crashed mid-append) is terminated first, so the
    new record starts on its own line; readers skip the torn fragment. A failed write cuts the
    file back to its earlier length, so no partial record stays behind.
    """
    size = os.fstat(fd).st_size
    prefix = b""
    if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
        prefix = b"\n"
    payload = prefix + data
    written = 0
    try:
        while written < len(payload):
            written += os.write(fd, payload[written:])
        if durable:
            os.fsync(fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, size)
        raise JobsctlError("internal", f"cannot append to the record file: {exc.strerror}") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, size)
        raise
