"""Actor grammar, default actor and the human-only gate."""

from __future__ import annotations

import pytest

from jobsctl.actors import default_actor, parse_actor, require_human, sanitize_user_name
from jobsctl.errors import JobsctlError


@pytest.mark.parametrize("value", ["human:me", "auto:bot", "human:a.b_c@d-e", "auto:" + "x" * 64])
def test_valid_actors(value: str) -> None:
    assert parse_actor(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "me",
        "human:",
        "human:" + "x" * 65,
        "robot:x",
        "human:a b",
        "human:a\n",
        "HUMAN:x",
        "human:é",
    ],
)
def test_invalid_actors(value: str) -> None:
    with pytest.raises(JobsctlError) as exc:
        parse_actor(value)
    assert exc.value.code == "usage"


def test_reserved_actor_is_usage() -> None:
    with pytest.raises(JobsctlError) as exc:
        parse_actor("auto:jobsctl")
    assert exc.value.exit_code == 2
    assert parse_actor("auto:jobsctl2") == "auto:jobsctl2"


def test_default_actor() -> None:
    assert default_actor(me_name="kim", stdin_isatty=True) == "human:kim"
    assert default_actor(me_name="kim", stdin_isatty=False) == "auto:unknown"


def test_user_name_is_fitted_to_the_grammar() -> None:
    assert sanitize_user_name("kim") == "kim"
    assert sanitize_user_name("a b/c") == "a_b_c"
    assert sanitize_user_name("x" * 100) == "x" * 64
    assert sanitize_user_name("") == "unknown"
    assert parse_actor("human:" + sanitize_user_name("é\n"))


def test_human_gate_tty_passes() -> None:
    require_human("cancel --force", stdin_isatty=True, explicit_actor=None)
    require_human("cancel --force", stdin_isatty=True, explicit_actor="auto:x")


def test_human_gate_human_actor_passes_without_tty() -> None:
    require_human("cancel --force", stdin_isatty=False, explicit_actor="human:kim")


@pytest.mark.parametrize("actor", [None, "auto:x", "auto:unknown"])
def test_human_gate_refuses_otherwise(actor: str | None) -> None:
    with pytest.raises(JobsctlError) as exc:
        require_human("release --override-escalation", stdin_isatty=False, explicit_actor=actor)
    assert exc.value.code == "needs_human"
    assert exc.value.exit_code == 13
    assert "release --override-escalation" in exc.value.message
