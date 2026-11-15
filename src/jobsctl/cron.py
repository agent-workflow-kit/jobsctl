"""The managed crontab block of ``cron install`` / ``cron uninstall`` (interface.md, cron install).

Pure functions build the entry line and splice the block into a crontab held as bytes; ``apply``
reads the user's crontab, projects the change and writes it back through ``gateway``. Everything
outside the block is kept byte for byte (comments, environment lines, CRLF, bytes that are not
UTF-8). The one exception is a missing final newline, which is added when the block follows it.

The block is two lines: the header ``# jobsctl managed`` and the entry. A crontab we cannot read
unambiguously (two headers, a header not followed by an entry, a NUL byte, an oversized file) is
refused rather than guessed at.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsctl import gateway
from jobsctl.config_files import absolute
from jobsctl.errors import JobsctlError
from jobsctl.store_dir import prepare_state_dir, resolve_state_dir
from jobsctl.textsafe import sanitize

HEADER = b"# jobsctl managed"
ENTRY_PREFIX = b"* * * * * "
ENTRY_SUFFIX = b" run --quiet"
MAX_CRONTAB_BYTES = 1024 * 1024
TEMP_ROOTS = ("/tmp", "/run/user")  # PATH entries under these are per-session directories
XDG_VARIABLES = ("XDG_CONFIG_HOME", "XDG_STATE_HOME")

Warning = dict[str, Any]


def _fail(message: str) -> JobsctlError:
    return JobsctlError("internal", message)


# -- quoting ----------------------------------------------------------------------------------


def quote(value: str) -> str:
    """``value`` as one single-quoted shell word with ``%`` escaped for cron.

    ``'`` becomes ``'\\''``. Cron turns a bare ``%`` into a newline even inside quotes and turns
    ``\\%`` back into ``%`` before the shell runs, so that is the only escape needed; the value
    is otherwise literal to the shell (no ``$``, backtick or backslash expansion in single quotes).
    """
    return "'" + value.replace("'", "'\\''").replace("%", "\\%") + "'"


def has_control(text: str) -> bool:
    """A control character (or line/paragraph separator) that cannot be written to a crontab."""
    return any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in text)


# -- PATH and XDG values ----------------------------------------------------------------------


def _warning(*, reason: str, path: str | None = None, variable: str | None = None) -> Warning:
    what = f"{variable} is left out" if variable else "PATH entry dropped"
    if path:
        what = f"PATH entry dropped: {sanitize(path, inline=True)}"
    out: Warning = {"code": "path_entry_dropped", "message": f"{what} ({reason})", "reason": reason}
    if variable is not None:
        out["variable"] = variable
    elif path:
        out["path"] = path
    return out


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _is_temporary(path: str, temp_roots: Sequence[str]) -> bool:
    candidates = {os.path.normpath(path), os.path.realpath(path)}
    for root in temp_roots:
        roots = {os.path.normpath(root), os.path.realpath(root)}
        if any(_under(c, r) for c in candidates for r in roots):
            return True
    return False


def filter_path(
    entries: Sequence[str],
    *,
    home: str,
    temp_roots: Sequence[str] | None = None,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> tuple[list[str], list[Warning]]:
    """The usable ``PATH`` entries in order (duplicates dropped silently) and a warning for each
    entry dropped: empty, relative, not an existing directory, temporary or with a control
    character. A leading ``~`` or ``~/`` is expanded to ``home``."""
    roots = TEMP_ROOTS if temp_roots is None else temp_roots
    kept: list[str] = []
    warnings: list[Warning] = []
    for raw in entries:
        entry = raw
        if entry == "~" or entry.startswith("~/"):
            entry = home.rstrip("/") + entry[1:]
        if entry == "":
            warnings.append(_warning(reason="empty"))
        elif has_control(entry):
            warnings.append(_warning(reason="control", path=raw))
        elif not entry.startswith("/"):
            warnings.append(_warning(reason="relative", path=raw))
        elif _is_temporary(entry, roots):
            warnings.append(_warning(reason="temporary", path=raw))
        elif not is_dir(entry):
            warnings.append(_warning(reason="missing", path=raw))
        elif entry not in kept:
            kept.append(entry)
    return kept, warnings


def filter_xdg(environ: Mapping[str, str]) -> tuple[dict[str, str], list[Warning]]:
    """The ``XDG_*`` values to write: only those set (non-empty) at install. A relative value
    (jobsctl ignores it) or one with a control character is left out with a warning that names
    the variable and never its value."""
    kept: dict[str, str] = {}
    warnings: list[Warning] = []
    for name in XDG_VARIABLES:
        value = environ.get(name, "")
        if value == "":
            continue
        if has_control(value):
            warnings.append(_warning(reason="control", variable=name))
        elif not value.startswith("/"):
            warnings.append(_warning(reason="relative", variable=name))
        else:
            kept[name] = value
    return kept, warnings


def build_entry(*, jobsctl: str, path: Sequence[str], xdg: Mapping[str, str]) -> str:
    parts = ["* * * * *", f"PATH={quote(':'.join(path))}"]
    parts += [f"{name}={quote(value)}" for name, value in xdg.items()]
    parts += [quote(jobsctl), "run", "--quiet"]
    return " ".join(parts)


def locate_jobsctl(argv0: str, environ: Mapping[str, str]) -> str:
    """The absolute path of the ``jobsctl`` executable: ``argv[0]`` when it is the installed
    script, else the first ``jobsctl`` on ``PATH`` (a ``python -m jobsctl`` run). Symlinks are kept,
    so an upgrade that re-points them keeps working."""
    found: str | None = None
    if os.path.basename(argv0) == "jobsctl":
        found = (
            str(absolute(argv0)) if "/" in argv0 else shutil.which(argv0, path=environ.get("PATH"))
        )
    if found is None:
        found = shutil.which("jobsctl", path=environ.get("PATH"))
    if found is None:
        raise _fail("cannot find the jobsctl executable; install it on PATH first")
    found = str(absolute(found))
    if has_control(found):
        raise _fail("the path of jobsctl has a control character; it cannot go in a crontab")
    return found


# -- splicing the block -----------------------------------------------------------------------


def is_entry(line: bytes) -> bool:
    return line.startswith(ENTRY_PREFIX) and line.endswith(ENTRY_SUFFIX)


@dataclass
class _Parsed:
    lines: list[bytes]
    start: int | None  # index of the header line, if there is a block


def _parse(data: bytes | None) -> _Parsed:
    raw = data or b""
    if len(raw) > MAX_CRONTAB_BYTES:
        raise _fail("the existing crontab is larger than 1 MiB; not touching it")
    if b"\0" in raw:
        raise _fail("the existing crontab contains a NUL byte; not touching it")
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    headers = [i for i, line in enumerate(lines) if line == HEADER]
    if len(headers) > 1:
        raise _fail("the crontab has more than one '# jobsctl managed' block; fix it by hand")
    if not headers:
        return _Parsed(lines, None)
    i = headers[0]
    if i + 1 >= len(lines) or not is_entry(lines[i + 1]):
        raise _fail(
            "the '# jobsctl managed' line is not followed by a jobsctl entry; fix it by hand"
        )
    return _Parsed(lines, i)


def _join(lines: list[bytes]) -> bytes:
    return b"\n".join(lines) + b"\n" if lines else b""


def with_block(data: bytes | None, entry: bytes) -> bytes:
    """``data`` with the managed block holding ``entry``, replacing an existing block in place or
    appended. A block already identical leaves the bytes untouched."""
    parsed = _parse(data)
    block = [HEADER, entry]
    if parsed.start is None:
        return _join(parsed.lines + block)
    if parsed.lines[parsed.start : parsed.start + 2] == block:
        return data or b""
    lines = parsed.lines[: parsed.start] + block + parsed.lines[parsed.start + 2 :]
    return _join(lines)


def without_block(data: bytes | None) -> bytes | None:
    """``data`` without the managed block (unchanged bytes when there is none)."""
    parsed = _parse(data)
    if parsed.start is None:
        return data
    return _join(parsed.lines[: parsed.start] + parsed.lines[parsed.start + 2 :])


def block_of(data: bytes | None) -> bytes | None:
    """The two block lines (without a final newline), or ``None``."""
    parsed = _parse(data)
    if parsed.start is None:
        return None
    return b"\n".join(parsed.lines[parsed.start : parsed.start + 2])


# -- applying ---------------------------------------------------------------------------------


@dataclass
class CronResult:
    changed: bool
    dry_run: bool
    block: str | None
    warnings: list[Warning] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "dry_run": self.dry_run,
            "block": self.block,
            "warnings": self.warnings,
        }


def _decode(block: bytes) -> str:
    return block.decode("utf-8", errors="surrogateescape")


def _read() -> bytes | None:
    try:
        return gateway.crontab_read()
    except gateway.CrontabError as exc:
        raise _fail(f"cannot read the crontab: {exc}") from exc


def _same(a: bytes | None, b: bytes | None) -> bool:
    return (a or b"") == (b or b"")


def _write(data: bytes, state_dir: Path) -> None:
    """Install ``data`` with the crontab command from a private temp file (never stdin), or remove
    the crontab when ``data`` is empty."""
    try:
        if data == b"":
            gateway.crontab_remove()
            return
        fd, name = tempfile.mkstemp(prefix="crontab-", dir=state_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            gateway.crontab_install(name)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
    except gateway.CrontabError as exc:
        raise _fail(f"the crontab command failed; your crontab is unchanged: {exc}") from exc
    except OSError as exc:
        raise _fail(f"cannot stage the crontab: {exc.strerror}") from exc


def _locked_state_dir() -> tuple[Path, int]:
    paths = prepare_state_dir(resolve_state_dir(), create=True)
    lock = paths.root / "cron.lock"
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise _fail(f"cannot open {lock}: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise _fail(f"{lock} is not a regular file we own")
        gateway.flock_exclusive(fd)
    except BaseException:
        os.close(fd)
        raise
    return paths.root, fd


def _commit(compute: Callable[[bytes | None], bytes]) -> tuple[bool, bytes | None]:
    """Under the cron lock: read, compute the new crontab, re-read to catch a concurrent editor,
    write, read back and check. Returns whether it wrote and the final crontab."""
    state_dir, lock_fd = _locked_state_dir()
    try:
        for _attempt in range(3):
            before = _read()
            after = compute(before)
            if _same(before, after):
                return False, before
            if not _same(_read(), before):
                continue  # someone else edited the crontab meanwhile: recompute
            _write(after, state_dir)
            seen = _read()
            if not _same(seen, after):
                raise _fail(
                    "the crontab does not read back as written (edited meanwhile?); "
                    "run the command again"
                )
            return True, seen
        raise _fail("the crontab keeps changing under us; run the command again")
    finally:
        os.close(lock_fd)


def install(*, dry_run: bool, environ: Mapping[str, str], argv0: str, home: str) -> CronResult:
    jobsctl = locate_jobsctl(argv0, environ)
    entries = environ.get("PATH", "").split(":") if environ.get("PATH") else []
    local_bin = home.rstrip("/") + "/.local/bin"
    path, warnings = filter_path(entries, home=home)
    if local_bin not in path and os.path.isdir(local_bin):
        path.append(local_bin)
    if not path:
        raise _fail("no usable PATH entry is left to write; set PATH and try again")
    xdg, xdg_warnings = filter_xdg(environ)
    warnings += xdg_warnings
    entry = os.fsencode(build_entry(jobsctl=jobsctl, path=path, xdg=xdg))
    block = _decode(HEADER + b"\n" + entry)
    if dry_run:
        current = _read()
        projected = with_block(current, entry)
        return CronResult(not _same(current, projected), True, block, warnings)
    changed, _ = _commit(lambda current: with_block(current, entry))
    return CronResult(changed, False, block, warnings)


def uninstall(*, dry_run: bool = False) -> CronResult:
    if dry_run:
        current = _read()
        return CronResult(not _same(current, without_block(current)), True, None)
    changed, _ = _commit(lambda current: without_block(current) or b"")
    return CronResult(changed, False, None)


def main_argv0() -> str:
    return sys.argv[0] if sys.argv else "jobsctl"
