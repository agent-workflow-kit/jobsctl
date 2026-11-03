"""Config models and loading: defaults, drop-ins, ranges, hooks, hostile input, no env leaks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from jobsctl.cli_values import parse_duration
from jobsctl.config import Config
from jobsctl.config_files import MAX_FILE_BYTES, handoff_args, load_config
from jobsctl.errors import JobsctlError
from tests.conftest import SCHEMAS, validate_against
from tests.helpers import run_cli

ROOT = Path(__file__).resolve().parent.parent
SECRET = "s3cr3t-VALUE-9f2"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str) -> Config:
    return load_config(write(tmp_path / "c.yaml", text))


def invalid(tmp_path: Path, text: str) -> str:
    with pytest.raises(JobsctlError) as info:
        load(tmp_path, text)
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == 2
    return info.value.message


def default_file(private_home: Path) -> Path:
    return private_home / ".config" / "jobsctl" / "config.yaml"


# --- defaults ---------------------------------------------------------------------------------


def test_no_config_file_gives_the_defaults(private_home: Path) -> None:
    assert load_config() == Config()


def test_defaults_equal_config_example_yaml() -> None:
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert Config.model_validate(example) == Config()


def test_example_yaml_loads_through_the_file_loader(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert load_config(path) == Config()


def test_defaults_equal_schema_defaults() -> None:
    schema = json.loads((SCHEMAS / "config.json").read_text(encoding="utf-8"))
    defaults = {k: v["default"] for k, v in schema["properties"].items() if "default" in v}
    assert set(defaults) == set(schema["properties"])
    assert Config.model_validate(defaults) == Config()


def test_example_validates_against_the_schema() -> None:
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    validate_against("config.json", example)


# --- locations and drop-ins -------------------------------------------------------------------


def test_default_file_respects_xdg_config_home(
    private_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    write(xdg / "jobsctl" / "config.yaml", "default_cap: 7\n")
    write(default_file(private_home), "default_cap: 3\n")
    assert load_config().default_cap == 7


def test_relative_xdg_config_home_is_ignored(
    private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    write(default_file(private_home), "default_cap: 3\n")
    assert load_config().default_cap == 3


def test_explicit_config_must_exist(tmp_path: Path) -> None:
    with pytest.raises(JobsctlError) as info:
        load_config(tmp_path / "missing.yaml")
    assert info.value.code == "config_invalid"


def test_dropins_apply_in_lexical_order_and_merge_maps(tmp_path: Path) -> None:
    base = write(
        tmp_path / "config.yaml",
        "default_cap: 1\nqueues:\n  a: {cap: 1, priority: 5}\nenv:\n  set: {A: '1', B: '1'}\n",
    )
    write(tmp_path / "config.d" / "20-b.yaml", "default_cap: 20\nenv: {set: {B: '2'}}\n")
    write(tmp_path / "config.d" / "10-a.yaml", "default_cap: 10\nqueues: {a: {cap: 4}, b: {}}\n")
    cfg = load_config(base)
    assert cfg.default_cap == 20
    assert cfg.env.set == {"A": "1", "B": "2"}
    assert cfg.queues["a"].cap == 4
    assert cfg.queues["a"].priority == 5  # not reset by the sibling key's override: map merge
    assert "b" in cfg.queues


def test_lexical_order_not_creation_order(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "{}\n")
    write(tmp_path / "config.d" / "b.yaml", "default_cap: 2\n")
    write(tmp_path / "config.d" / "a.yaml", "default_cap: 1\n")
    write(tmp_path / "config.d" / "B.yaml", "default_cap: 9\n")  # uppercase sorts first
    assert load_config(base).default_cap == 2


def test_hook_lists_append_other_lists_replace(tmp_path: Path) -> None:
    base = write(
        tmp_path / "config.yaml",
        "hooks:\n  post-fail: [{name: one, cmd: a}]\nmask_env: [A]\n",
    )
    write(
        tmp_path / "config.d" / "x.yaml",
        "hooks:\n  post-fail: [{name: two, cmd: b}]\n  pre-start: [{cmd: c}]\nmask_env: [B]\n",
    )
    cfg = load_config(base)
    assert [h.name for h in cfg.hooks.post_fail] == ["one", "two"]
    assert [h.cmd for h in cfg.hooks.pre_start] == ["c"]
    assert cfg.mask_env == ["B"]


def test_duplicate_hook_name_across_files_is_invalid(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "hooks:\n  post-fail: [{name: n, cmd: a}]\n")
    write(tmp_path / "config.d" / "x.yaml", "hooks:\n  post-fail: [{name: n, cmd: b}]\n")
    with pytest.raises(JobsctlError) as info:
        load_config(base)
    assert info.value.code == "config_invalid"
    assert "post-fail" in info.value.message


def test_same_name_in_different_hooks_is_fine(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        "hooks:\n  post-fail: [{name: n, cmd: a}]\n  post-held: [{name: n, cmd: a}]\n",
    )
    assert cfg.hooks.post_held[0].name == "n"


def test_dropins_without_a_config_file(private_home: Path) -> None:
    write(default_file(private_home).parent / "config.d" / "x.yaml", "auto_run: false\n")
    assert load_config().auto_run is False


def test_dropin_filtering(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "{}\n")
    write(tmp_path / "config.d" / "notes.txt", "this is not yaml: [\n")
    write(tmp_path / "config.d" / ".hidden.yaml", "default_cap: 99\n")
    write(tmp_path / "config.d" / "a.yml", "default_cap: 98\n")
    assert load_config(base).default_cap == 1


def test_dropin_error_names_the_file(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "{}\n")
    write(tmp_path / "config.d" / "bad.yaml", "default_cap: -1\n")
    with pytest.raises(JobsctlError) as info:
        load_config(base)
    assert "bad.yaml" in info.value.message
    assert "default_cap" in info.value.message


def test_dropin_directory_named_yaml_is_refused(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "{}\n")
    (tmp_path / "config.d" / "x.yaml").mkdir(parents=True)
    with pytest.raises(JobsctlError):
        load_config(base)


def test_config_d_that_is_a_file_is_refused(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "{}\n")
    write(tmp_path / "config.d", "x")
    with pytest.raises(JobsctlError):
        load_config(base)


def test_symlinked_config_is_followed(tmp_path: Path) -> None:
    real = write(tmp_path / "real.yaml", "default_cap: 5\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    assert load_config(link).default_cap == 5


def test_dangling_symlink_dropin_is_refused(tmp_path: Path) -> None:
    base = write(tmp_path / "config.yaml", "{}\n")
    (tmp_path / "config.d").mkdir()
    (tmp_path / "config.d" / "x.yaml").symlink_to(tmp_path / "nowhere")
    with pytest.raises(JobsctlError):
        load_config(base)


def test_fifo_config_does_not_block(tmp_path: Path) -> None:
    fifo = tmp_path / "c.yaml"
    os.mkfifo(fifo)
    with pytest.raises(JobsctlError) as info:
        load_config(fifo)
    assert info.value.code == "config_invalid"


def test_config_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(JobsctlError):
        load_config(tmp_path)


# --- no environment variables -----------------------------------------------------------------


def test_jobsctl_variables_change_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = load(tmp_path, "default_cap: 2\n")
    for name, value in (
        ("JOBSCTL_DEFAULT_CAP", "9"),
        ("JOBSCTL_AUTO_RUN", "false"),
        ("JOBSCTL_CONFIG", str(tmp_path / "other.yaml")),
        ("JOBSCTL_ENV_FILE", "/etc/passwd"),
        ("JOBSCTL_QUEUES", "{x: {}}"),
        ("JOBSCTL_ME_NAME", "mallory"),
    ):
        monkeypatch.setenv(name, value)
    assert load(tmp_path, "default_cap: 2\n") == before
    assert load_config() == Config()


# --- ranges and types -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("default_cap: -1", "default_cap"),
        ("global_cap: -1", "global_cap"),
        ("default_cap: '1'", "default_cap"),
        ("default_cap: 1.0", "default_cap"),
        ("default_cap: true", "default_cap"),
        ("strict_queues: 'yes'", "strict_queues"),
        ("auto_run: 1", "auto_run"),
        ("tail_bytes: 262145", "tail_bytes"),
        ("tail_bytes: 0", "tail_bytes"),
        ("tail_lines: 0", "tail_lines"),
        ("run_interval: 0s", "run_interval"),
        ("run_interval: 05m", "run_interval"),
        ("run_interval: 10", "run_interval"),
        ("run_interval: 10w", "run_interval"),
        ("run_interval: ' 10s'", "run_interval"),
        ('run_interval: "10s\\n"', "run_interval"),
        ("kill_grace: null", "kill_grace"),
        ("retention: 36501d", "retention"),
        ("retention: 99999999999999999999999d", "retention"),
        ("retention: 0", "retention"),
        ("busy_timeout: -5s", "busy_timeout"),
        ("tail_flush_interval: 1.5s", "tail_flush_interval"),
        ("queues: {a: {cap: -1}}", "queues.a.cap"),
        ("queues: {a: {cap: '2'}}", "queues.a.cap"),
        ("queues: {a: {priority: 1001}}", "queues.a.priority"),
        ("queues: {a: {priority: -1001}}", "queues.a.priority"),
        ("queues: {a: {max_retries: 101}}", "queues.a.max_retries"),
        ("queues: {a: {max_retries: -1}}", "queues.a.max_retries"),
        ("queues: {a: {tail_bytes: 262145}}", "queues.a.tail_bytes"),
        ("queues: {a: {timeout: 0s}}", "queues.a.timeout"),
        ("queues: {a: {retry_delay: null}}", "queues.a.retry_delay"),
        ("queues: {a: {kill_grace: 3x}}", "queues.a.kill_grace"),
        ("queues: {a: {bogus: 1}}", "queues.a.bogus"),
        ("queues: {A: {}}", "queues.A"),
        ("queues: {'a b': {}}", "queues"),
        ("queues: {a: null}", "queues.a"),
        ("queues: [a]", "queues"),
        ("bogus: 1", "bogus"),
        ("env: {bogus: 1}", "env.bogus"),
        ("me: {name: 'a b'}", "me.name"),
        ("me: {name: ''}", "me.name"),
        ("mask_env: [A, A]", "mask_env"),
        ("mask_env: ['1A']", "mask_env.0"),
        ('mask_env: ["A\\n"]', "mask_env.0"),
        ("hooks_strip_env: ['A=B']", "hooks_strip_env.0"),
        ("env: {inherit: ['LC_*', 'LC_*']}", "env.inherit"),
        ("env: {inherit: ['*']}", "env.inherit.0"),
        ("env: {set: {'1X': a}}", "env.set.1X"),
        ("env: {path_prepend: ['relative/bin']}", "env.path_prepend.0"),
        ("env: {path_prepend: ['~other/bin']}", "env.path_prepend.0"),
        ('env: {path_prepend: ["/a\\nb"]}', "env.path_prepend.0"),
        ("env_file: relative/env", "env_file"),
        ("env_file: ''", "env_file"),
        ("env_file: 5", "env_file"),
        ("hooks: {bogus: []}", "hooks.bogus"),
        ("hooks: {pre-start: {cmd: a}}", "hooks.pre-start"),
        ("hooks: [a]", "hooks"),
    ],
)
def test_invalid_key_is_named(tmp_path: Path, text: str, key: str) -> None:
    assert key in invalid(tmp_path, text + "\n")


@pytest.mark.parametrize(
    "text",
    [
        "default_cap: null",
        "default_cap: 0",
        "global_cap: 0",
        "tail_bytes: 262144",
        "retention: null",
        "retention: 36500d",
        "queues: {a: {cap: null}}",
        "queues: {a: {cap: 0, priority: -1000, max_retries: 100, timeout: 2h}}",
        "queues: {a: {kill_grace: 1m, tail_bytes: 1, tail_lines: 1}}",
        "env: {inherit: [PATH, 'LC_*', _X]}",
        "me: {name: kim.o@host-1}",
    ],
)
def test_boundary_values_are_accepted(tmp_path: Path, text: str) -> None:
    load(tmp_path, text + "\n")


def test_durations_become_seconds(tmp_path: Path) -> None:
    cfg = load(
        tmp_path, "run_interval: 2m\nretention: 1d\nkill_grace: 1h\nqueues: {a: {timeout: 90s}}\n"
    )
    assert (cfg.run_interval, cfg.retention, cfg.kill_grace) == (120, 86400, 3600)
    assert cfg.queues["a"].timeout == 90
    assert cfg.queue("a").timeout == 90


def test_config_durations_use_the_cli_grammar_and_schema_pattern() -> None:
    schema = json.loads((SCHEMAS / "config.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/duration"})
    for text in ("1s", "36500d", "876000h", "36501d", "05m", "0s", "36500d ", "9" * 40 + "s"):
        try:
            parse_duration(text)
            cli_ok = True
        except ValueError:
            cli_ok = False
        assert validator.is_valid(text) == cli_ok, text
        model_ok = True
        try:
            Config.model_validate({"run_interval": text})
        except ValueError:
            model_ok = False
        assert model_ok == cli_ok, text


# --- queue resolution -------------------------------------------------------------------------


def test_queue_precedence_queue_over_global_over_default(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        "default_cap: 3\nkill_grace: 2m\ntail_bytes: 100\ntail_lines: 10\n"
        "queues:\n  a: {cap: 5, kill_grace: 1m, tail_bytes: 50}\n  b: {cap: null}\n  c: {}\n",
    )
    a, b, c, unknown = (cfg.queue(n) for n in ("a", "b", "c", "zzz"))
    assert (a.cap, a.kill_grace, a.tail_bytes, a.tail_lines) == (5, 60, 50, 10)
    assert b.cap is None  # explicit null: unbounded
    assert c.cap == 3  # absent: default_cap
    assert unknown.cap == 3
    assert (unknown.kill_grace, unknown.priority, unknown.max_retries) == (120, 0, 0)
    assert (unknown.retry_delay, unknown.timeout) == (60, None)


def test_queue_cap_zero_is_kept(tmp_path: Path) -> None:
    assert load(tmp_path, "default_cap: 4\nqueues: {a: {cap: 0}}\n").queue("a").cap == 0


# --- env and paths ----------------------------------------------------------------------------


def test_tilde_expands_in_env_file_and_path_prepend(tmp_path: Path, private_home: Path) -> None:
    cfg = load(
        tmp_path, "env_file: ~/.config/jobsctl/env\nenv: {path_prepend: ['~/bin', /opt/x]}\n"
    )
    assert cfg.env_file == f"{private_home}/.config/jobsctl/env"
    assert cfg.env.path_prepend == [f"{private_home}/bin", "/opt/x"]


def test_tilde_with_unknown_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os.path, "expanduser", lambda p: p)
    assert "env_file" in invalid(tmp_path, "env_file: ~/x\n")


def test_nul_is_refused(tmp_path: Path) -> None:
    assert "env.set.A" in invalid(tmp_path, 'env: {set: {A: "x\\0y"}}\n')
    assert "hooks" in invalid(tmp_path, 'hooks: {post-fail: [{cmd: "a\\0b"}]}\n')
    assert "env_file" in invalid(tmp_path, 'env_file: "/a\\0b"\n')


@pytest.mark.parametrize("value", ["42", "[a, b]", "{k: v}", "null", "true", "2026-01-01", "1.5"])
def test_env_set_wrong_types_name_the_key(tmp_path: Path, value: str) -> None:
    assert "env.set.TOKEN" in invalid(tmp_path, f"env:\n  set:\n    TOKEN: {value}\n")


def test_secret_looking_env_values_never_reach_a_message(tmp_path: Path) -> None:
    messages = [
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: [{SECRET}]\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: {{a: {SECRET}}}\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: {SECRET}\n  bogus: {SECRET}\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: !!int {SECRET}\n"),
        invalid(tmp_path, f"env:\n  set: [{SECRET}]\n"),
        invalid(tmp_path, f"env:\n  set: {SECRET}\n"),
        invalid(tmp_path, f"env:\n  path_prepend: [{SECRET}]\n"),
        invalid(tmp_path, f"env:\n  path_prepend: {SECRET}\n"),
        invalid(tmp_path, f"env:\n  inherit: [{SECRET}!]\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: &a {SECRET}\n    X: *a\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: {SECRET}\n    TOKEN: {SECRET}\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: 'unterminated {SECRET}\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: {SECRET}\n  : [\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: {SECRET}\n\tbad: tab\n"),
        invalid(tmp_path, f"env_file: {SECRET}\n"),
        invalid(tmp_path, f"env:\n  set:\n    TOKEN: {SECRET}\ndefault_cap: {SECRET}\n"),
    ]
    for message in messages:
        assert SECRET not in message


def test_env_set_secret_never_in_cli_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path / "c.yaml", f"env:\n  set:\n    TOKEN: [{SECRET}]\n")
    for extra in ((), ("--json",)):
        result = run_cli(capsys, "--config", str(path), "queues", *extra)
        assert result.code == 2
        assert SECRET not in result.out + result.err
        assert "env.set.TOKEN" in result.out + result.err


# --- hook entries -----------------------------------------------------------------------------


def test_hook_entry_defaults_and_identity(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        "hooks:\n  pre-start:\n    - cmd: ./gate\n    - {name: g-2, cmd: ./gate2, timeout: 10m}\n",
    )
    first, second = cfg.hooks.pre_start
    assert (first.name, first.timeout, first.queues, first.exclude_queues, first.labels) == (
        None,
        30,
        None,
        [],
        [],
    )
    assert first.identity == "./gate"
    assert second.identity == "g-2"
    assert second.timeout == 600


def test_post_hook_fields(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        "hooks:\n  post-fail:\n    - {cmd: x, async: true, include_output: true, "
        "queues: [a], exclude_queues: [b], labels: ['k=v', 'ticket=PROJ-1']}\n"
        "  post-held: [{cmd: y}]\n",
    )
    hook = cfg.hooks.post_fail[0]
    assert hook.async_ is True and hook.include_output is True
    assert hook.queues == ["a"] and hook.exclude_queues == ["b"]
    assert cfg.hooks.post_held[0].async_ is False
    assert cfg.hooks.post_held[0].include_output is False


@pytest.mark.parametrize("field", ["async: true", "include_output: true", "async: false"])
def test_post_only_fields_are_rejected_on_pre_start(tmp_path: Path, field: str) -> None:
    message = invalid(tmp_path, f"hooks:\n  pre-start: [{{cmd: x, {field}}}]\n")
    assert "hooks.pre-start.0." in message


def test_post_only_fields_fine_on_every_post_hook(tmp_path: Path) -> None:
    for name in ("post-start", "post-finish", "post-fail", "post-held", "post-cancel"):
        load(tmp_path, f"hooks:\n  {name}: [{{cmd: x, async: true, include_output: true}}]\n")


@pytest.mark.parametrize(
    ("entry", "key"),
    [
        ("{}", "cmd"),
        ("{cmd: ''}", "cmd"),
        ("{cmd: 5}", "cmd"),
        ("{cmd: [a, b]}", "cmd"),
        ("{cmd: x, name: Bad}", "name"),
        ("{cmd: x, name: ''}", "name"),
        ("{cmd: x, name: '-a'}", "name"),
        ('{cmd: x, name: "a\\n"}', "name"),
        (f"{{cmd: x, name: '{'a' * 65}'}}", "name"),
        ("{cmd: x, timeout: 30}", "timeout"),
        ("{cmd: x, timeout: 0s}", "timeout"),
        ("{cmd: x, timeout: 36501d}", "timeout"),
        ("{cmd: x, queues: a}", "queues"),
        ("{cmd: x, queues: [A]}", "queues.0"),
        ("{cmd: x, queues: [a, a]}", "queues"),
        ("{cmd: x, exclude_queues: [a, a]}", "exclude_queues"),
        ("{cmd: x, exclude_queues: null}", "exclude_queues"),
        ("{cmd: x, labels: ['nokey']}", "labels.0"),
        ("{cmd: x, labels: ['K=v']}", "labels.0"),
        ("{cmd: x, labels: ['k=v', 'k=v']}", "labels"),
        ("{cmd: x, unknown: 1}", "unknown"),
        ("{cmd: x, retries: 1}", "retries"),
    ],
)
def test_invalid_hook_entries_name_the_key(tmp_path: Path, entry: str, key: str) -> None:
    message = invalid(tmp_path, f"hooks:\n  post-fail: [{entry}]\n")
    assert f"hooks.post-fail.0.{key}" in message or (
        key in ("queues", "exclude_queues", "labels") and f"hooks.post-fail.0.{key}" in message
    )


def test_duplicate_hook_name_in_one_list_is_config_invalid(tmp_path: Path) -> None:
    message = invalid(tmp_path, "hooks:\n  pre-start: [{name: g, cmd: a}, {name: g, cmd: b}]\n")
    assert "hooks.pre-start" in message and "'g'" in message


def test_unnamed_hooks_may_repeat_a_cmd(tmp_path: Path) -> None:
    # Only a duplicate `name` is config_invalid (interface.md, Hooks).
    cfg = load(tmp_path, "hooks:\n  post-fail: [{cmd: a}, {cmd: a}]\n")
    assert len(cfg.hooks.post_fail) == 2


def test_unknown_queue_selector_is_a_warning_only(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        "strict_queues: true\nqueues: {known: {}}\nhooks:\n"
        "  pre-start: [{cmd: a, queues: [known, ghost]}]\n"
        "  post-fail: [{cmd: b, exclude_queues: [ghost2, seen]}]\n",
    )
    found = cfg.unknown_queue_selectors(known_queues={"seen"})
    assert [(f.hook, f.index, f.field, f.queue) for f in found] == [
        ("pre-start", 0, "queues", "ghost"),
        ("post-fail", 0, "exclude_queues", "ghost2"),
    ]
    assert cfg.unknown_queue_selectors(known_queues={"ghost", "ghost2", "seen"}) == []


def test_queues_null_selects_every_queue_without_warning(tmp_path: Path) -> None:
    cfg = load(tmp_path, "hooks:\n  post-fail: [{cmd: a, queues: null}]\n")
    assert cfg.unknown_queue_selectors() == []


# --- hostile files ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "default_cap: !!python/object/apply:os.system ['true']\n",
        "x: !!python/name:os.system\n",
        "a: &a [1]\nb: *a\n",
        "a: &a x\n",
        "default_cap: 1\ndefault_cap: 2\n",
        "queues:\n  a: {cap: 1}\n  a: {cap: 2}\n",
        "env:\n  set: {A: x, A: y}\n",
        "- a\n- b\n",
        "just a string\n",
        "{unclosed\n",
        "a: 1\n---\nb: 2\n",
        "default_cap: 1\n\tx: 2\n",
        "? [a]\n: b\n",
        "default_cap: 1\n<<: {default_cap: 2, default_cap: 3}\n",
    ],
)
def test_hostile_yaml_is_config_invalid(tmp_path: Path, text: str) -> None:
    invalid(tmp_path, text)


def test_billion_laughs_is_refused_fast(tmp_path: Path) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 10):
        lines.append(f"a{i}: &a{i} [{', '.join(f'*a{i - 1}' for _ in range(9))}]")
    assert "anchors" in invalid(tmp_path, "\n".join(lines) + "\n")


def test_deeply_nested_yaml_is_config_invalid(tmp_path: Path) -> None:
    invalid(tmp_path, "x: " + "[" * 100_000 + "]" * 100_000 + "\n")
    invalid(tmp_path, "x: " + "{a: " * 50_000 + "1" + "}" * 50_000 + "\n")


def test_huge_integer_is_config_invalid(tmp_path: Path) -> None:
    invalid(tmp_path, "default_cap: " + "9" * 5000 + "\n")


def test_oversize_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("# " + "x" * MAX_FILE_BYTES + "\n")
    with pytest.raises(JobsctlError) as info:
        load_config(path)
    assert info.value.code == "config_invalid"


def test_non_utf8_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_bytes(b"default_cap: 1\n# \xff\xfe\n")
    with pytest.raises(JobsctlError):
        load_config(path)


def test_empty_and_comment_only_files_are_defaults(tmp_path: Path) -> None:
    assert load(tmp_path, "") == Config()
    assert load(tmp_path, "# nothing\n") == Config()
    assert load(tmp_path, "{}\n") == Config()


def test_unicode_keys_and_values_are_handled(tmp_path: Path) -> None:
    message = invalid(tmp_path, "‮qüeue: 1\n")
    assert "‮" not in message


def test_long_hostile_key_is_truncated(tmp_path: Path) -> None:
    message = invalid(tmp_path, ("k" * 10_000) + ": 1\n")
    assert len(message) < 600


def test_many_errors_are_summarized(tmp_path: Path) -> None:
    message = invalid(tmp_path, "".join(f"k{i}: 1\n" for i in range(50)))
    assert "more" in message and len(message) < 600


# --- --config hand-off and dispatch -----------------------------------------------------------


def test_handoff_args_are_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert handoff_args(None) == []
    monkeypatch.chdir(tmp_path)
    assert handoff_args(Path("rel/c.yaml")) == ["--config", str(tmp_path / "rel/c.yaml")]
    assert handoff_args(Path("/abs/c.yaml")) == ["--config", "/abs/c.yaml"]


def test_relative_config_is_resolved_to_absolute_in_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jobsctl import cli_registry, commands
    from jobsctl.cli_registry import Context

    commands.load_all()
    table: dict[str, cli_registry.Handler] = {}
    monkeypatch.setattr(cli_registry, "_HANDLERS", table)
    seen: list[Context] = []

    def handler(ctx: Context) -> int:
        seen.append(ctx)
        return 0

    table["queues"] = handler
    write(tmp_path / "sub" / "c.yaml", "me: {name: kim}\ndefault_cap: 6\n")
    monkeypatch.chdir(tmp_path)
    assert run_cli(capsys, "--config", "sub/c.yaml", "queues").code == 0
    ctx = seen[0]
    assert ctx.config_path == tmp_path / "sub" / "c.yaml"
    assert ctx.config.default_cap == 6
    assert ctx.me_name == "kim"
    assert ctx.actor == "auto:unknown"


def test_me_name_defaults_to_the_os_user(tmp_path: Path) -> None:
    assert Config().actor_name("os-user") == "os-user"
    assert load(tmp_path, "me: {name: kim}\n").actor_name("os-user") == "kim"


def test_invalid_config_exits_2_with_the_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path / "c.yaml", "default_cap: -1\n")
    result = run_cli(capsys, "--config", str(path), "queues", "--json")
    assert result.code == 2
    envelope = result.envelope()
    validate_against("cli/error.json", envelope)
    assert envelope["error"]["code"] == "config_invalid"
    assert "default_cap" in envelope["error"]["message"]


def test_invalid_config_is_reported_after_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path / "c.yaml", "default_cap: -1\n")
    result = run_cli(capsys, "--config", str(path), "--json", "hold")
    assert result.code == 2
    assert result.envelope()["error"]["code"] == "usage"


def test_invalid_default_config_fails_every_command(
    private_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(default_file(private_home), "auto_run: maybe\n")
    result = run_cli(capsys, "queues", "--json")
    assert result.code == 2
    assert result.envelope()["error"]["code"] == "config_invalid"
