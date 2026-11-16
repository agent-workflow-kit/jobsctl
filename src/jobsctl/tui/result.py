"""The `--result-file` of `browse` (`schemas/cli/browse-result.json`)."""

from __future__ import annotations

import json
import os
import stat
from typing import Final, Literal

from jobsctl.errors import JobsctlError

Intent = Literal["back", "open"]
_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def write_result(path: str, intent: Intent, selected: list[str]) -> None:
    """Write the result document to `path`: a regular file, created 0600 when new, never
    through a symlink, never opening a FIFO or device (an open that would block is refused)."""
    document = json.dumps({"intent": intent, "selected": selected}) + "\n"
    try:
        fd = os.open(path, _FLAGS, 0o600)
    except (OSError, ValueError) as exc:
        raise _cannot(exc) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise JobsctlError("internal", "cannot write the result file (not a regular file)")
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
