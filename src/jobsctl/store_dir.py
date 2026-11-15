"""The state directory: location, creation and ownership checks (interface.md, Audit and log).

``<state dir>`` holds ``state.db`` (0600), ``shims/`` and ``diag/`` (0700); the directory itself
is 0700. These are set to their exact mode with chmod right after creation, so neither a
permissive nor a restrictive umask (e.g. 0277) changes it. Missing ancestors jobsctl creates keep
the umask-derived mode plus owner rwx; pre-existing directories are never changed. A final
path component that is a symlink, a directory or file owned by someone else, or a database that
is not a regular file is refused. A directory we own with group or other permissions is
tightened. Ancestors of the state directory may be symlinks (``~/.local`` often is).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jobsctl.errors import JobsctlError


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "state.db"

    @property
    def shims(self) -> Path:
        return self.root / "shims"

    @property
    def diag(self) -> Path:
        return self.root / "diag"


def resolve_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/jobsctl``, else ``~/.local/state/jobsctl``.

    A relative ``XDG_STATE_HOME`` is ignored (the XDG specification says so).
    """
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME", "")
    if base and os.path.isabs(base):
        return Path(base) / "jobsctl"
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "state" / "jobsctl"


def _unsafe(path: Path, why: str) -> JobsctlError:
    return JobsctlError("internal", f"state path {path} is unsafe: {why}")


def _mkdir(path: Path, *, private: bool) -> None:
    """Create ``path``; an existing one is kept.

    A private directory is exactly 0700 regardless of the umask. Otherwise it keeps the
    umask-derived mode plus owner rwx, so children can be created even under umask 0277.
    """
    try:
        os.mkdir(path, 0o700 if private else 0o777)
    except FileExistsError:
        return
    except OSError as exc:
        raise JobsctlError("internal", f"cannot create {path}: {exc.strerror}") from exc
    try:
        if private:
            os.chmod(path, 0o700)  # the umask may have removed our own bits
        else:
            created = stat.S_IMODE(os.lstat(path).st_mode)
            if created & 0o700 != 0o700:
                os.chmod(path, created | 0o700)
    except OSError as exc:
        raise JobsctlError("internal", f"cannot set the mode of {path}: {exc.strerror}") from exc


def _makedirs(path: Path) -> None:
    """``mkdir -p`` for ancestors: created ones keep the umask mode plus owner rwx."""
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current) and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        _mkdir(directory, private=False)


def _secure_dir(path: Path, *, create: bool, repair: bool = True) -> None:
    """Make ``path`` an owned, 0700-or-tighter real directory, creating it when ``create``.

    Without ``repair`` the same checks run but a loose mode is left as it is."""
    if create:
        _mkdir(path, private=True)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _unsafe(path, f"cannot open as a real directory ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise _unsafe(path, "owned by another user")
        extra = stat.S_IMODE(st.st_mode) & 0o077
        if extra and repair:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) & ~0o077)
    except OSError as exc:
        raise _unsafe(path, exc.strerror or "cannot check it") from exc
    finally:
        os.close(fd)


def prepare_state_dir(root: Path, *, create: bool, readonly: bool = False) -> StatePaths:
    """Validate (and with ``create``, create) the state directory and its subdirectories.

    ``readonly`` (never with ``create``) only checks the state directory itself: it changes no
    mode and does not require ``shims/`` or ``diag/`` to exist."""
    paths = StatePaths(root)
    if create:
        _makedirs(root.parent)
    _secure_dir(root, create=create, repair=not readonly)
    if readonly:
        return paths
    for sub in (paths.shims, paths.diag):
        _secure_dir(sub, create=create)
    return paths


def prepare_db_file(db: Path, *, create: bool, readonly: bool = False) -> None:
    """Make sure ``db`` is a private regular file we own; create it empty (0600) when ``create``.

    ``readonly`` opens it read-only for the checks and never changes its mode."""
    flags = (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    try:
        if create:
            try:
                fd = os.open(db, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(db, flags)
        else:
            fd = os.open(db, flags)
    except OSError as exc:
        raise _unsafe(db, f"cannot open ({exc.strerror})") from exc
    try:
        if created:
            os.fchmod(fd, 0o600)  # the umask may have removed our own bits
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _unsafe(db, "not a regular file")
        if st.st_uid != os.geteuid():
            raise _unsafe(db, "owned by another user")
        if stat.S_IMODE(st.st_mode) & 0o077 and not readonly:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) & ~0o077)
    except OSError as exc:
        raise _unsafe(db, exc.strerror or "cannot check it") from exc
    finally:
        os.close(fd)
