"""The `resolve --note-file` reader (interface.md, `resolve`).

A note is untrusted text of at most 4096 bytes. A path is opened with `O_NONBLOCK` (so a FIFO
cannot hang the open), must be a regular file once open (a FIFO, device or directory is refused
without reading it; a symlink is followed to what it names) and at most 4097 bytes are read. `-`
reads stdin the same way: stdin is already open, so there is no open to hang, and a terminal is
read until end of file. Nothing here echoes the note's content in an error.
"""

from __future__ import annotations

import os
import stat
import sys
from typing import Final

from jobsctl.errors import usage_error

MAX_NOTE_BYTES: Final = 4096
_READ_LIMIT: Final = MAX_NOTE_BYTES + 1


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < _READ_LIMIT:
        chunk = os.read(fd, _READ_LIMIT - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _read_stdin() -> bytes:
    stream = sys.stdin
    if stream is None:
        raise usage_error("stdin is closed; it cannot supply the note")
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is not None:
        # a non-blocking stdin would give short reads (silent truncation) or None
        os.set_blocking(fd, True)
        return _read_fd(fd)
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        return bytes(binary.read(_READ_LIMIT))
    return stream.read(_READ_LIMIT).encode("utf-8", "surrogateescape")


def _read_path(path: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "invalid path"
        raise usage_error(f"cannot open the note file ({reason})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise usage_error("the note file is not a regular file")
        return _read_fd(fd)
    except OSError as exc:
        raise usage_error(f"cannot read the note file ({exc.strerror or 'I/O error'})") from None
    finally:
        os.close(fd)


def read_note(where: str) -> str:
    """The note from `where` (a path, or `-` for stdin); exit 2 when it cannot be read, is not
    UTF-8, or is over 4096 bytes."""
    try:
        data = _read_stdin() if where == "-" else _read_path(where)
    except OSError as exc:  # stdin: a directory, a closed descriptor, a read error
        raise usage_error(f"cannot read the note ({exc.strerror or 'I/O error'})") from None
    if len(data) > MAX_NOTE_BYTES:
        raise usage_error(f"the note is longer than {MAX_NOTE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise usage_error("the note is not valid UTF-8") from None
