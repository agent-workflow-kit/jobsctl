"""Config models: every key of `docs/interface.md` Config, validated, with built-in defaults.

The defaults equal `config.example.yaml` and `schemas/config.json`. Models are strict (no
coercion: `"1"` is not an integer, `1` is not a string) and forbid unknown keys. Durations are
parsed to integer seconds, `~/` at the start of `env_file` and `env.path_prepend` entries is
expanded, and no validator ever puts an input value into its message, so an error can name an
`env.*` key without echoing what it held (`jobsctl.config_files` renders the messages).

Precedence is flags > `queues.<name>` > global config > defaults; `Config.queue` resolves the
last three for one queue and the command layer applies its flags on top.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic import StringConstraints as Str

# TODO(#4): the duration grammar moves to `durations.py`; this import follows it.
from jobsctl.cli_values import DurationError, parse_duration
from jobsctl.textsafe import echo

_QUEUE_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ENV_NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
_LABEL_PATTERN: Final = r"^[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}$"
_PATH_PATTERN: Final = r"^(/|~/)[^\n]*$"

INT_MAX: Final = 2**31 - 1  # SQLite-safe bound for integer settings
MAX_TEXT_BYTES: Final = 131072  # an env.set value or hook cmd, as UTF-8 (the --env cap)

HOOK_NAMES: Final = (
    "pre-start",
    "post-start",
    "post-finish",
    "post-fail",
    "post-held",
    "post-cancel",
)

DEFAULT_ENV_INHERIT: Final = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_*",
    "TZ",
    "TERM",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
)


def _seconds(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("must be a duration such as 30s, 10m, 2h or 30d")
    try:
        return parse_duration(value)
    except DurationError as exc:
        raise ValueError(str(exc)) from None


def _seconds_or_null(value: Any) -> Any:
    return None if value is None else _seconds(value)


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


def _utf8(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be valid UTF-8 text (no lone surrogates)") from None
    return value


def _at_most_128k(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"must be at most {MAX_TEXT_BYTES} bytes (UTF-8)")
    return value


def _not_null(value: Any) -> Any:
    if value is None:
        raise ValueError("must not be null")
    return value


def _unique(values: list[Any]) -> list[Any]:
    if len(set(values)) != len(values):
        raise ValueError("entries must be unique")
    return values


def _expand_home(value: str) -> str:
    """`~/rest` becomes `<home>/rest`; an absolute path stays; NUL and lone surrogates are
    refused. An empty or relative `HOME` is an unknown home."""
    _utf8(_no_nul(value))
    if not value.startswith("~/"):
        return value
    home = os.path.expanduser("~")
    if os.environ.get("HOME") == "" or not home.startswith("/"):
        raise ValueError("~ cannot be expanded: the home directory is unknown")
    return home.rstrip("/") + value[1:]


Duration = Annotated[int, BeforeValidator(_seconds)]
OptionalDuration = Annotated[int | None, BeforeValidator(_seconds_or_null)]
QueueName = Annotated[str, Str(pattern=_QUEUE_PATTERN)]
EnvName = Annotated[str, Str(pattern=ENV_NAME_PATTERN)]
Label = Annotated[str, Str(pattern=_LABEL_PATTERN)]
ConfigPath = Annotated[str, Str(pattern=_PATH_PATTERN), AfterValidator(_expand_home)]
EnvPattern = Annotated[str, Str(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}\*?$")]
Cap = Annotated[int, Field(ge=0, le=INT_MAX)]
TailBytes = Annotated[int, Field(ge=1, le=262144)]
TailLines = Annotated[int, Field(ge=1, le=INT_MAX)]
Text128K = Annotated[
    str, AfterValidator(_no_nul), AfterValidator(_utf8), AfterValidator(_at_most_128k)
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EnvConfig(_Model):
    inherit: Annotated[list[EnvPattern], AfterValidator(_unique)] = Field(
        default_factory=lambda: list(DEFAULT_ENV_INHERIT)
    )
    set: dict[EnvName, Text128K] = Field(default_factory=dict)
    path_prepend: list[ConfigPath] = Field(default_factory=list)


class _HookEntry(_Model):
    # Absent is unset; an explicit null is refused, as in schemas/config.json.
    name: Annotated[
        Annotated[str, Str(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")] | None,
        BeforeValidator(_not_null),
    ] = None
    cmd: Annotated[Text128K, Str(min_length=1)]
    timeout: Duration = 30
    queues: Annotated[list[QueueName], AfterValidator(_unique)] | None = None
    exclude_queues: Annotated[list[QueueName], AfterValidator(_unique)] = Field(
        default_factory=list
    )
    labels: Annotated[list[Label], AfterValidator(_unique)] = Field(default_factory=list)

    @property
    def identity(self) -> str:
        """`name` if set, else `cmd`."""
        return self.name if self.name is not None else self.cmd


class PreHookEntry(_HookEntry):
    """A `pre-start` hook: `async` and `include_output` are post-hook fields, so they are unknown
    keys here."""


class PostHookEntry(_HookEntry):
    async_: bool = Field(default=False, alias="async")
    include_output: bool = False


def _unique_names(entries: list[Any]) -> list[Any]:
    """A duplicate `name`, or two entries with one identity (`name` if set, else `cmd`: an
    unnamed `{cmd: foo}` next to `{name: foo}`, or two unnamed entries with one `cmd`), is
    invalid: `hook_id`, `vetoed_by` and `override_hooks` must name exactly one hook."""
    names: set[str] = set()
    identities: set[str] = set()
    for entry in entries:
        if entry.name is not None:
            if entry.name in names:
                raise ValueError(f"duplicate hook name {entry.name!r} in one list")
            names.add(entry.name)
        if entry.identity in identities:
            raise ValueError(f"duplicate hook identity {echo(entry.identity, 64)!r} in one list")
        identities.add(entry.identity)
    return entries


PreHooks = Annotated[list[PreHookEntry], AfterValidator(_unique_names)]
PostHooks = Annotated[list[PostHookEntry], AfterValidator(_unique_names)]


class HooksConfig(_Model):
    pre_start: PreHooks = Field(default_factory=list, alias="pre-start")
    post_start: PostHooks = Field(default_factory=list, alias="post-start")
    post_finish: PostHooks = Field(default_factory=list, alias="post-finish")
    post_fail: PostHooks = Field(default_factory=list, alias="post-fail")
    post_held: PostHooks = Field(default_factory=list, alias="post-held")
    post_cancel: PostHooks = Field(default_factory=list, alias="post-cancel")

    def entries(self, hook: str) -> list[PreHookEntry] | list[PostHookEntry]:
        """The entries of `hook` (one of `HOOK_NAMES`), in configured order."""
        if hook not in HOOK_NAMES:
            raise KeyError(hook)
        return getattr(self, hook.replace("-", "_"))


class MeConfig(_Model):
    name: Annotated[str, Str(pattern=r"^[A-Za-z0-9._@-]{1,64}$")] | None = None


class QueueConfig(_Model):
    """One `queues.<name>` entry; an absent key means the global value (see `Config.queue`).

    `kill_grace`, `tail_bytes` and `tail_lines` default to `None` (absent) but refuse an explicit
    null, as `schemas/config.json` does."""

    cap: Cap | None = None
    priority: Annotated[int, Field(ge=-1000, le=1000)] = 0
    timeout: OptionalDuration = None
    max_retries: Annotated[int, Field(ge=0, le=100)] = 0
    retry_delay: Duration = 60
    kill_grace: Annotated[int | None, BeforeValidator(_seconds)] = None
    tail_bytes: Annotated[TailBytes | None, BeforeValidator(_not_null)] = None
    tail_lines: Annotated[TailLines | None, BeforeValidator(_not_null)] = None


@dataclass(frozen=True)
class QueueSettings:
    """The settings of one queue after precedence queue > global > default."""

    cap: int | None
    priority: int
    timeout: int | None
    max_retries: int
    retry_delay: int
    kill_grace: int
    tail_bytes: int
    tail_lines: int


@dataclass(frozen=True)
class UnknownQueueSelector:
    """A hook selector naming a queue that is neither configured nor known (a warning only)."""

    hook: str
    index: int
    field: str  # "queues" or "exclude_queues"
    queue: str


class Config(_Model):
    default_cap: Cap | None = 1
    global_cap: Cap | None = None
    strict_queues: bool = False
    auto_run: bool = True
    run_interval: Duration = 10
    kill_grace: Duration = 30
    tail_bytes: TailBytes = 16384
    tail_lines: TailLines = 200
    tail_flush_interval: Duration = 5
    mask_env: Annotated[list[EnvName], AfterValidator(_unique)] = Field(default_factory=list)
    retention: OptionalDuration = 30 * 86400
    env: EnvConfig = Field(default_factory=EnvConfig)
    env_file: ConfigPath | None = None
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    hooks_strip_env: Annotated[list[EnvName], AfterValidator(_unique)] = Field(default_factory=list)
    busy_timeout: Duration = 5
    me: MeConfig = Field(default_factory=MeConfig)
    queues: dict[QueueName, QueueConfig] = Field(default_factory=dict)

    def queue(self, name: str) -> QueueSettings:
        """Settings of queue `name`: its own entry, else the global value, else the default.

        A queue that is not configured gets `default_cap` and the global values. `cap: null` in a
        queue entry is unbounded; an absent `cap` is `default_cap`.
        """
        entry = self.queues.get(name)
        if entry is None:
            entry = QueueConfig()
        return QueueSettings(
            cap=entry.cap if "cap" in entry.model_fields_set else self.default_cap,
            priority=entry.priority,
            timeout=entry.timeout,
            max_retries=entry.max_retries,
            retry_delay=entry.retry_delay,
            kill_grace=entry.kill_grace if entry.kill_grace is not None else self.kill_grace,
            tail_bytes=entry.tail_bytes if entry.tail_bytes is not None else self.tail_bytes,
            tail_lines=entry.tail_lines if entry.tail_lines is not None else self.tail_lines,
        )

    def unknown_queue_selectors(
        self, known_queues: Collection[str] = ()
    ) -> list[UnknownQueueSelector]:
        """Hook `queues` and `exclude_queues` entries naming a queue that is neither configured
        nor in `known_queues` (queues that have had jobs). Never an error, also with
        `strict_queues`; the caller logs `unknown_queue_selector` once per change of state."""
        found: list[UnknownQueueSelector] = []
        for hook in HOOK_NAMES:
            for index, entry in enumerate(self.hooks.entries(hook)):
                for field in ("queues", "exclude_queues"):
                    for queue in getattr(entry, field) or ():
                        if queue not in self.queues and queue not in known_queues:
                            found.append(UnknownQueueSelector(hook, index, field, queue))
        return found

    def actor_name(self, os_user: str) -> str:
        """`me.name`, else the OS user name: the name of the default actor."""
        return self.me.name if self.me.name is not None else os_user
