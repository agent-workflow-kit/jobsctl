"""The `--result-file` of `browse` (`schemas/cli/browse-result.json`)."""

from __future__ import annotations

import json
import os
import stat
from typing import Final, Literal

from jobsctl.errors import JobsctlError

Intent = Literal["back", "open"]
_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_regular(path: str, *, flags: int = _FLAGS) -> int:
    """Open `path` for writing without truncating: created 0600 when new (with `_FLAGS`), never
    through a symlink, never a FIFO, device or directory."""
    try:
        fd = os.open(path, flags, 0o600)
    except (OSError, ValueError) as exc:
        raise _cannot(exc) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise JobsctlError("internal", "cannot write the result file (not a regular file)")
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_result_path(path: str) -> None:
    """The checks of `write_result`, run before the session and without creating or changing
    anything: an existing path must be a regular file opened for writing, a missing one must
    have a writable directory."""
    try:
        fd = _open_regular(path, flags=_FLAGS & ~os.O_CREAT)
    except JobsctlError:
        if os.path.lexists(path):
            raise
        parent = os.path.dirname(path) or "."
        if not (os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK)):
            raise
        return
    os.close(fd)


def write_result(path: str, intent: Intent, selected: list[str]) -> None:
    """Write the result document to `path` (see `_open_regular` for what is accepted)."""
    document = json.dumps({"intent": intent, "selected": selected}) + "\n"
    fd = _open_regular(path)
    try:
        try:
            os.fchmod(fd, 0o600)
        except PermissionError:
            pass  # writable via group/ACL but not owned by us: write without tightening
        os.ftruncate(fd, 0)
        data = document.encode("ascii")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
    except OSError as exc:
        raise _cannot(exc) from None
    finally:
        os.close(fd)


def _cannot(exc: Exception) -> JobsctlError:
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "invalid path"
    return JobsctlError("internal", f"cannot write the result file ({reason})")
