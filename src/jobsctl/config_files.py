"""Find, read, merge and validate the config files (interface.md, Config).

`~/.config/jobsctl/config.yaml` (`XDG_CONFIG_HOME` respected) or `--config PATH`, then the
`config.d/*.yaml` drop-ins next to it in lexical order. Maps merge, hook lists append, every other
value (scalars and other lists) is replaced by the later file. Config comes only from files: no
environment variable other than `XDG_CONFIG_HOME` and `HOME` (where the default file lives) is read.

Every failure is `JobsctlError("config_invalid")` naming the file and the key, never an input
value: YAML parser messages quote the input, so they are replaced by the position alone.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from jobsctl.config import HOOK_NAMES, Config
from jobsctl.errors import JobsctlError
from jobsctl.textsafe import echo, sanitize

MAX_FILE_BYTES: Final = 1024 * 1024
MAX_DROP_INS: Final = 256
_MAX_ERRORS_SHOWN: Final = 5


def _name(path: Path) -> str:
    return sanitize(str(path), inline=True)


def _invalid(message: str) -> JobsctlError:
    return JobsctlError("config_invalid", message)


def default_config_path() -> Path:
    """`$XDG_CONFIG_HOME/jobsctl/config.yaml` (only if absolute), else `~/.config/...`."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path(os.path.expanduser("~")) / ".config"
    return base / "jobsctl" / "config.yaml"


def handoff_args(config_path: Path | None) -> list[str]:
    """The `--config` arguments an internal process or triggered pass is started with: the
    absolute path of an explicit `--config`, nothing when the default config is in use."""
    if config_path is None:
        return []
    return ["--config", os.path.abspath(config_path)]


class _Rejected(Exception):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader without anchors and aliases (no expansion bombs) and without duplicate keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        line = event.start_mark.line + 1
        if isinstance(event, yaml.AliasEvent):
            raise _Rejected("aliases are not allowed", line)
        if getattr(event, "anchor", None) is not None:
            raise _Rejected("anchors are not allowed", line)
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:
                continue  # unhashable: the base class reports it
            if duplicate:
                raise _Rejected(
                    f"duplicate key {echo(str(key), 64)!r}", key_node.start_mark.line + 1
                )
        return super().construct_mapping(node, deep)


def _read_regular(path: Path) -> bytes:
    """The bytes of a regular file of at most `MAX_FILE_BYTES`; never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise _invalid(f"{_name(path)}: cannot be read ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _invalid(f"{_name(path)}: not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise _invalid(f"{_name(path)}: larger than {MAX_FILE_BYTES} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise _invalid(f"{_name(path)}: cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise _invalid(f"{_name(path)}: larger than {MAX_FILE_BYTES} bytes")
    return data


def read_yaml_file(path: Path) -> dict[Any, Any]:
    """One config file as a mapping (an empty file is `{}`)."""
    name = _name(path)
    try:
        text = _read_regular(path).decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(f"{name}: not valid UTF-8") from None
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 (a SafeLoader subclass)
    except _Rejected as exc:
        where = f" at line {exc.line}" if exc.line else ""
        raise _invalid(f"{name}: {exc}{where}") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise _invalid(f"{name}: invalid YAML{where}") from None
    except (RecursionError, ValueError, OverflowError):
        raise _invalid(f"{name}: invalid YAML (too deeply nested or out of range)") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise _invalid(f"{name}: the top level must be a mapping")
    return document


def merge(base: dict[Any, Any], over: dict[Any, Any], path: tuple[Any, ...] = ()) -> dict[Any, Any]:
    """`over` on top of `base`: maps merge, hook lists append, anything else is replaced."""
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge(current, value, (*path, key))
        elif (
            path == ("hooks",)
            and key in HOOK_NAMES
            and isinstance(current, list)
            and isinstance(value, list)
        ):
            out[key] = current + value
        else:
            out[key] = value
    return out


def _location(loc: tuple[Any, ...]) -> str:
    parts = [echo(str(part), 64) for part in loc if part != "[key]"]
    return ".".join(parts) if parts else "(top level)"


def validate(document: dict[Any, Any], source: str) -> Config:
    """`Config` from a merged mapping; an error names `source` and each offending key."""
    try:
        return Config.model_validate(document)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        lines = [f"{_location(e['loc'])}: {e['msg']}" for e in errors[:_MAX_ERRORS_SHOWN]]
        if len(errors) > _MAX_ERRORS_SHOWN:
            lines.append(f"and {len(errors) - _MAX_ERRORS_SHOWN} more")
        raise _invalid(f"{source}: " + "; ".join(lines)) from None


def _dropin_paths(directory: Path) -> list[Path]:
    try:
        entries = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except NotADirectoryError:
        raise _invalid(f"{_name(directory)}: not a directory") from None
    except OSError as exc:
        raise _invalid(f"{_name(directory)}: cannot be read ({exc.strerror})") from None
    names = [n for n in entries if n.endswith(".yaml") and not n.startswith(".")]
    if len(names) > MAX_DROP_INS:
        raise _invalid(f"{_name(directory)}: more than {MAX_DROP_INS} drop-in files")
    return [directory / n for n in names]


def load_config(explicit: Path | None = None) -> Config:
    """Load, merge and validate the config.

    `explicit` is the `--config` file (it must exist); without it the default file may be absent,
    which gives the built-in defaults. Drop-ins are read from `config.d/` next to the file, also
    when the file itself is absent. Each file is validated on its own (so an error names its
    file), then the merge is validated as a whole (duplicate hook names across files).
    """
    path = explicit if explicit is not None else default_config_path()
    files: list[Path] = []
    if explicit is not None or os.path.lexists(path):
        files.append(path)
    files.extend(_dropin_paths(path.parent / "config.d"))
    merged: dict[Any, Any] = {}
    for file in files:
        document = read_yaml_file(file)
        validate(document, _name(file))
        merged = merge(merged, document)
    return validate(merged, "merged config" if len(files) > 1 else _name(path))
