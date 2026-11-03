"""`env_file` loading: the secrets file, its format and its availability checks.

`env_file` is a trust anchor (docs/interface.md, Canonical environment): it is **available** only
if it opens without following symlinks as a regular file of at most 1 MiB owned by the user with
no group or other read or write access, and every ancestor directory, opened from `/` without
following symlinks, is owned by the user or root, not writable by group or others and not a
symlink. Anything else is **unavailable**; `env_file: null` is **not configured**, which is not
unavailable.

Nothing here puts a value into a message, a warning, a repr or an exception: warnings carry codes,
line numbers and enumerated reasons only. Callers log a warning once per change of state by
comparing `EnvFile.state_key`.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

MAX_BYTES: Final = 1024 * 1024
_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PR_SET_DUMPABLE: Final = 4

# Names an env_file line may not set in a hook environment (still passable with --pass-env).
_IGNORED_EXACT: Final = frozenset(
    {
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "IFS",
    }
)
_IGNORED_PREFIXES: Final = ("JOBSCTL_", "LD_", "DYLD_", "PYTHON")

Status = Literal["not_configured", "available", "unavailable"]


@dataclass(frozen=True)
class EnvWarning:
    """A state warning for `log.jsonl`. Never holds file content.

    `reason` is one of a fixed set of codes (only for `env_file_unsafe`); `line` is the 1-based
    line number (line warnings); `entry`/`variable` are only used by other env modules."""

    code: str
    line: int | None = None
    reason: str | None = None
    variable: str | None = None
    entry: str | None = None


def _euid() -> int:
    return os.geteuid()


def disable_dumpable() -> None:
    """`prctl(PR_SET_DUMPABLE, 0)` on Linux, so other same-user processes cannot read this
    process's memory or environment (reset by exec, so jobs and hooks are unaffected). Called
    before any `env_file` value is loaded; a failure raises rather than carry on exposed."""
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "prctl(PR_SET_DUMPABLE) failed")


def _dir_trusted(mode: int, uid: int, euid: int) -> bool:
    return stat.S_ISDIR(mode) and uid in (euid, 0) and mode & 0o022 == 0


def _file_trusted(mode: int, uid: int, size: int, euid: int) -> bool:
    return stat.S_ISREG(mode) and uid == euid and mode & 0o066 == 0 and size <= MAX_BYTES


@dataclass(frozen=True)
class EnvFile:
    """The result of reading `env_file` at one moment. `values` is empty unless available; it is
    excluded from the repr so a stray log of this object cannot leak it."""

    status: Status
    values: Mapping[str, str] = field(default_factory=dict, repr=False)
    warnings: tuple[EnvWarning, ...] = ()
    lines: Mapping[str, int] = field(default_factory=dict, repr=False)  # name -> winning line

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def state_key(self) -> tuple[object, ...]:
        """Equal for equal warnings: log a warning only when this changes."""
        return tuple(self.warnings)

    def hook_values(self) -> tuple[dict[str, str], tuple[EnvWarning, ...]]:
        """The values a hook environment gets: everything but the ignored names (PATH, HOME, ...),
        each ignored name reported as `env_file_line_ignored` with its winning line number."""
        values: dict[str, str] = {}
        warnings: list[EnvWarning] = []
        for name, value in self.values.items():
            if name in _IGNORED_EXACT or name.startswith(_IGNORED_PREFIXES):
                warnings.append(EnvWarning("env_file_line_ignored", line=self.lines[name]))
            else:
                values[name] = value
        warnings.sort(key=lambda w: w.line or 0)
        return values, tuple(warnings)


def _unavailable(reason: str) -> EnvFile:
    return EnvFile("unavailable", warnings=(EnvWarning("env_file_unsafe", reason=reason),))


def _open_trusted(path: str, trust_from: str | None) -> tuple[int | None, str]:
    """Open `path` through checked ancestors; returns (fd, "") or (None, reason)."""
    if not path.startswith("/") or "\x00" in path:
        return None, "path"
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None, "path"
    exempt = 0
    if trust_from is not None:
        base = [p for p in trust_from.split("/") if p not in ("", ".")]
        if parts[: len(base)] == base:
            exempt = len(base)
    euid = _euid()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    open_fds: list[int] = []
    try:
        cur = os.open("/", flags | os.O_DIRECTORY)
        open_fds.append(cur)
        if exempt == 0:
            st = os.fstat(cur)
            if not _dir_trusted(st.st_mode, st.st_uid, euid):
                return None, "ancestor"
        for index, part in enumerate(parts[:-1], start=1):
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=cur)
            open_fds.append(nxt)
            if index > exempt:
                st = os.fstat(nxt)
                if not _dir_trusted(st.st_mode, st.st_uid, euid):
                    return None, "ancestor"
            cur = nxt
        fd = os.open(parts[-1], flags | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=cur)
        return fd, ""
    except OSError:
        return None, "open"
    finally:
        for fd in open_fds:
            os.close(fd)


def _read_trusted(fd: int) -> tuple[bytes | None, str]:
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_regular"
        if st.st_uid != _euid():
            return None, "owner"
        if st.st_mode & 0o066:
            return None, "mode"
        if st.st_size > MAX_BYTES:
            return None, "size"
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_BYTES:  # the file may have grown since fstat
            chunk = os.read(fd, MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_BYTES:
            return None, "size"
        return b"".join(chunks), ""
    except OSError:
        return None, "read"
    finally:
        os.close(fd)


def _parse(data: bytes) -> tuple[dict[str, str], dict[str, int], list[EnvWarning]]:
    values: dict[str, str] = {}
    lines: dict[str, int] = {}
    warnings: list[EnvWarning] = []
    raw_lines = data.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()  # the terminating newline does not start another line
    for number, raw in enumerate(raw_lines, start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if raw == b"" or raw.startswith(b"#"):
            continue
        name, sep, value = raw.partition(b"=")
        try:
            if b"\x00" in raw or not sep:
                raise ValueError
            text_name = name.decode("utf-8")
            text_value = value.decode("utf-8")
            if _NAME_RE.match(text_name) is None:
                raise ValueError
        except ValueError:  # includes UnicodeDecodeError
            warnings.append(EnvWarning("env_file_line_invalid", line=number))
            continue
        values[text_name] = text_value
        lines[text_name] = number
    return values, lines, warnings


def load_env_file(
    path: str | None, *, check_ancestors_from: str | os.PathLike[str] | None = None
) -> EnvFile:
    """Read `path` (the expanded config `env_file`) now.

    `check_ancestors_from` is a seam for tests only: ancestors up to and including that directory
    are opened but not checked (a pytest tmp dir sits under a world-writable `/tmp`). Production
    callers never pass it."""
    if path is None:
        return EnvFile("not_configured")
    disable_dumpable()
    trust = None if check_ancestors_from is None else os.fspath(check_ancestors_from)
    fd, reason = _open_trusted(path, trust)
    if fd is None:
        return _unavailable(reason)
    data, reason = _read_trusted(fd)
    if data is None:
        return _unavailable(reason)
    values, lines, warnings = _parse(data)
    return EnvFile(
        "available",
        values=MappingProxyType(values),
        warnings=tuple(warnings),
        lines=MappingProxyType(lines),
    )
