"""The duration grammar (interface.md, Durations)."""

from __future__ import annotations

import pytest

from jobsctl import durations
from jobsctl.durations import (
    MAX_DURATION_SECONDS,
    DurationError,
    looks_like_duration,
    parse_duration,
)

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("10m", 600), ("2h", 7200), ("30d", 2592000), ("1s", 1), ("36500d", 3153600000)],
)
def test_valid(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        "0s", "1w", "-1m", "none", "05m", "+1m", "1.5h", "10", "", "m", "1m\n", "\n1m", " 1m",
        "1M", "1ms", "١٠s", "1m\x00", "1​m", "１m", "1 m", "1e3s",
    ],
)  # fmt: skip
def test_invalid(text: str) -> None:
    with pytest.raises(DurationError):
        parse_duration(text)
    assert not looks_like_duration(text)


def test_cap_boundary_per_unit() -> None:
    for unit, factor in UNITS.items():
        top = MAX_DURATION_SECONDS // factor
        assert parse_duration(f"{top}{unit}") == top * factor
        with pytest.raises(DurationError, match="100 years"):
            parse_duration(f"{top + 1}{unit}")


def test_huge_digit_strings_never_reach_int() -> None:
    for text in ("9" * 5000 + "s", "9" * 13 + "s", "1" + "0" * 12 + "s"):
        with pytest.raises(DurationError, match="100 years"):
            parse_duration(text)


def test_error_message_does_not_echo_input() -> None:
    with pytest.raises(DurationError) as exc:
        parse_duration("S3CRET")
    assert "S3CRET" not in str(exc.value)


def test_looks_like_duration_ignores_the_cap() -> None:
    assert looks_like_duration("99999999999999d")
    assert not looks_like_duration("0s")
    assert not looks_like_duration("2026-10-31T12:00:00Z")


@pytest.mark.parametrize("text", ["1m\n", "1mX", " 1m", "1m1", "\n1m"])
def test_looks_like_duration_matches_the_whole_text(text: str) -> None:
    assert not looks_like_duration(text)


def test_digit_cap_refuses_13_digits_before_int(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: str) -> int:
        raise AssertionError("int() reached")

    monkeypatch.setattr(durations, "int", boom, raising=False)
    with pytest.raises(DurationError, match="100 years"):
        parse_duration("1" + "0" * 12 + "s")


def test_digit_cap_lets_12_digits_reach_the_value_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def spy(text: str) -> int:
        seen.append(text)
        return int(text)

    monkeypatch.setattr(durations, "int", spy, raising=False)
    with pytest.raises(DurationError, match="100 years"):
        parse_duration("9" * 12 + "s")
    assert seen == ["9" * 12]
