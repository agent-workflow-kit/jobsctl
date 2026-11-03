"""Find, read, merge and validate the config files (interface.md, Config).

`~/.config/jobsctl/config.yaml` (`XDG_CONFIG_HOME` respected) or `--config PATH`, then the
`config.d/*.yaml` drop-ins next to it in byte-wise lexical order. Maps merge, hook lists append,
every other value (scalars and other lists) is replaced by the later file. Config comes only from
files: no environment variable other than `XDG_CONFIG_HOME` and `HOME` (where the default file
lives) is read.

Every failure is `JobsctlError("config_invalid")` naming the file and the key, never an input
value: YAML parser messages quote the input, so they are replaced by the position alone.
Scalars resolve by the YAML 1.2 core schema; anchors, aliases, explicit tags, non-string keys and
duplicate keys are refused.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from jobsctl.config import ENV_NAME_PATTERN, HOOK_NAMES, Config
from jobsctl.errors import JobsctlError
from jobsctl.textsafe import echo, sanitize

MAX_FILE_BYTES: Final = 256 * 1024
MAX_DROP_INS: Final = 256
MAX_TOTAL_BYTES: Final = 1024 * 1024  # the main file and every drop-in together
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
    return ["--config", str(absolute(config_path))]


def absolute(path: str | os.PathLike[str]) -> Path:
    """`path` joined to the working directory when relative, without collapsing `..` (the kernel
    resolves `link/..` through the symlink; a lexical collapse would name another file)."""
    text = os.fspath(path)
    return Path(text if os.path.isabs(text) else os.path.join(os.getcwd(), text))


def _at(mark: Any) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}"


class _Rejected(Exception):
    """A refusal whose message already carries its position and never an input value."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 core-schema scalars and without anchors, aliases (no expansion
    bombs), explicit tags, non-string keys and duplicate keys."""

    yaml_implicit_resolvers: dict[Any, Any] = {}  # noqa: RUF012 (PyYAML's class-level table)

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        where = _at(event.start_mark)
        if isinstance(event, yaml.AliasEvent):
            raise _Rejected(f"aliases are not allowed at {where}")
        if getattr(event, "anchor", None) is not None:
            raise _Rejected(f"anchors are not allowed at {where}")
        if getattr(event, "tag", None) is not None:
            raise _Rejected(f"YAML tags are not allowed at {where}")
        return super().compose_node(parent, index)

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (ValueError, OverflowError):
            raise _Rejected(f"invalid value at {_at(node.start_mark)}") from None

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise _Rejected(
                    f"mapping key at {_at(key_node.start_mark)} must be a string (quote it)"
                )
            if key in seen:
                raise _Rejected(f"duplicate key at {_at(key_node.start_mark)}")
            seen.add(key)
        return super().construct_mapping(node, deep)


# YAML 1.2 core schema, narrowed: decimal integers without leading zeros or `_`.
for _tag, _pattern, _first in (
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)\Z", "tTfF"),
    ("int", r"^[-+]?(?:0|[1-9][0-9]*)\Z", "-+0123456789"),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z",
        "-+0123456789.",
    ),
    ("null", r"^(?:~|null|Null|NULL|)\Z", ["~", "n", "N", ""]),
):
    _StrictLoader.add_implicit_resolver(
        f"tag:yaml.org,2002:{_tag}", re.compile(_pattern), list(_first)
    )


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
    return _parse(_read_regular(path), _name(path))


def _parse(data: bytes, name: str) -> dict[Any, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(f"{name}: not valid UTF-8") from None
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 (a SafeLoader subclass)
    except _Rejected as exc:
        raise _invalid(f"{name}: {exc}") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at {_at(mark)}" if mark else ""
        raise _invalid(f"{name}: invalid YAML{where}") from None
    except RecursionError:
        raise _invalid(f"{name}: invalid YAML (too deeply nested)") from None
    except (ValueError, OverflowError):
        raise _invalid(f"{name}: invalid value") from None
    except Exception:  # noqa: BLE001 (any parser failure; its text may quote the input)
        raise _invalid(f"{name}: invalid YAML") from None
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
    """`loc` as a dotted key; an `env.set` key outside the name grammar is not echoed (it may be
    a `NAME=value` with a secret in it)."""
    parts: list[str] = []
    for index, part in enumerate(loc):
        if part == "[key]":
            continue
        text = str(part)
        if loc[index - 2 : index] == ("env", "set") and not re.fullmatch(ENV_NAME_PATTERN, text):
            parts.append("<invalid name>")
        else:
            parts.append(echo(text, 64))
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
        entries = sorted(os.listdir(directory), key=os.fsencode)
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


def _cross_file_duplicates(sources: dict[str, list[tuple[Any, str]]]) -> None:
    """A hook name used in two files (one file's own duplicates fail its own validation)."""
    for hook, entries in sources.items():
        first: dict[str, str] = {}
        for name, file in entries:
            if not isinstance(name, str):
                continue
            if name in first:
                raise _invalid(
                    f"hooks.{hook}: duplicate hook name {echo(name, 64)!r} "
                    f"(in {first[name]} and {file})"
                )
            first[name] = file


def load_config(explicit: Path | None = None) -> Config:
    """Load, merge and validate the config.

    `explicit` is the `--config` file (it must exist); without it the default file may be absent,
    which gives the built-in defaults. Drop-ins are read from `config.d/` next to the file, also
    when the file itself is absent. Each file is validated on its own (so an error names its
    file), then the merge is validated as a whole. Each file is at most `MAX_FILE_BYTES` and all
    of them together at most `MAX_TOTAL_BYTES`.
    """
    path = explicit if explicit is not None else default_config_path()
    files: list[Path] = []
    if explicit is not None or os.path.lexists(path):
        files.append(path)
    files.extend(_dropin_paths(path.parent / "config.d"))
    merged: dict[Any, Any] = {}
    sources: dict[str, list[tuple[Any, str]]] = {hook: [] for hook in HOOK_NAMES}
    total = 0
    for file in files:
        name = _name(file)
        data = _read_regular(file)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise _invalid(
                f"{name}: the config files together are larger than {MAX_TOTAL_BYTES} bytes"
            )
        document = _parse(data, name)
        validate(document, name)
        for hook, entries in document.get("hooks", {}).items():
            sources[hook].extend((entry.get("name"), name) for entry in entries)
        merged = merge(merged, document)
    _cross_file_duplicates(sources)
    return validate(merged, "merged config" if len(files) > 1 else _name(path))
