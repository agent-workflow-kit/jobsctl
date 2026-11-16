"""Diagnostics files of internal processes (docs/interface.md, Internal processes).

An internal process is started with its stdout and stderr on ``/dev/null`` and replaces them
with a :class:`DiagFile` for its own file: ``shims/<id>.log`` for a shim, ``diag/<kind>-<pid>.log``
for the others. The file is created (0600, no symlink, no FIFO) on the first write only, and
holds at most 1 MiB; later writes are dropped after one marker line. Callers never write
environment values or ``env_file`` lines into it.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from jobsctl.log_files import open_append

MAX_BYTES = 1024 * 1024
_MARKER = b"[diagnostics truncated at 1 MiB]\n"


class DiagFile(io.TextIOBase):
    def __init__(self, path: Path, limit: int = MAX_BYTES) -> None:
        self._path = path
        self._limit = limit
        self._fd: int | None = None
        self._written = 0
        self._failed = False
        self._full = False

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def write(self, text: str) -> int:
        size = len(text)
        data = text.encode("utf-8", "replace")
        if not data or self._failed or self._full:
            return size
        if self._fd is None:
            try:
                self._fd = open_append(self._path)
                self._written = os.fstat(self._fd).st_size
                if self._ends_full():
                    self._full = True
                    return size
            except Exception:  # diagnostics are best-effort; never fail the process for them
                self._failed = True
                return size
        room = max(self._limit - len(_MARKER) - self._written, 0)
        if len(data) > room:
            # cut on a character boundary: a partial trailing sequence is dropped
            data = data[:room].decode("utf-8", "ignore").encode("utf-8") + _MARKER
            self._full = True
        try:
            os.write(self._fd, data)
        except OSError:
            self._failed = True
        self._written += len(data)
        return size

    def _ends_full(self) -> bool:
        """A reopened file that already holds the marker (or is at the cap) takes nothing more."""
        if self._fd is None or self._written == 0:
            return False
        if self._written >= self._limit:
            return True
        try:
            tail = os.pread(self._fd, len(_MARKER), max(self._written - len(_MARKER), 0))
        except OSError:
            return False
        return tail == _MARKER

    def flush(self) -> None:
        return None

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        super().close()


def install(path: Path, limit: int = MAX_BYTES) -> DiagFile:
    """Send this process's ``sys.stdout`` and ``sys.stderr`` to the diagnostics file `path`."""
    diag = DiagFile(path, limit)
    sys.stdout = diag
    sys.stderr = diag
    return diag
