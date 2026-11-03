"""Flag value grammars used by the argparse surface (interface.md Durations, Timestamps)."""

from __future__ import annotations

import argparse
import datetime as dt
from typing import Any

import pytest

from jobsctl.cli_values import (
    DurationError,
    cwd,
    duration,
    duration_or_none,
    duration_or_zero,
    env_name,
    format_timestamp,
    parse_duration,
    parse_timestamp,
    positive_int,
    timestamp_or_duration,
)


@pytest.mark.parametrize(
    ("text", "seconds"), [("30s", 30), ("10m", 600), ("2h", 7200), ("30d", 2592000), ("1s", 1)]
)
def test_valid_durations(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        "0s",
        "0m",
        "0d",
        "00m",
        "05m",
        "007s",
        "1w",
        "-1m",
        "+1m",
        "1.5h",
        "10",
        "m",
        "",
        " 1m",
        "1m ",
        "1M",
        "١٠s",
        "1m\n",
        "1ms",
        "9" * 30 + "s",
        "99999999999d",
    ],
)
def test_invalid_durations(text: str) -> None:
    with pytest.raises(DurationError):
        parse_duration(text)


def test_cancel_wait_timeout_accepts_only_the_literal_zero() -> None:
    assert duration_or_zero("0") == 0
    assert duration_or_zero("5m") == 300
    for text in ("00", "0s", "0m", "0d", "-0", "0 ", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            duration_or_zero(text)


def test_duration_cap_is_100_years_and_huge_digit_strings_are_a_duration_error() -> None:
    assert parse_duration("36500d") == 36500 * 86400
    with pytest.raises(DurationError, match="100 years"):
        parse_duration("36501d")
    with pytest.raises(DurationError, match="100 years"):
        parse_duration("9" * 5000 + "s")


@pytest.mark.parametrize(
    "text",
    ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+05:00"],
)
def test_timestamp_overflow_is_a_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(text)
    with pytest.raises(argparse.ArgumentTypeError):
        timestamp_or_duration(text)


@pytest.mark.parametrize(
    "convert", [duration, duration_or_zero, duration_or_none, positive_int, timestamp_or_duration]
)
def test_huge_digit_strings_give_short_messages_without_internals(convert: Any) -> None:
    for text in ("9" * 5000, "9" * 5000 + "s", "-" + "9" * 5000):
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            convert(text)
        message = str(exc.value)
        assert len(message) < 300
        assert "int(" not in message
        assert "ValueError" not in message


def test_env_name_never_echoes_a_value() -> None:
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        env_name("NAME=s3cretvalue")
    assert "s3cretvalue" not in str(exc.value)


def test_cwd_rejects_a_newline() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cwd("/a\nb")
    assert cwd("/a/../b") == "/b"


def test_timestamp_format_has_millis_and_z() -> None:
    when = dt.datetime(2026, 10, 31, 12, 0, 0, 123456, tzinfo=dt.UTC)
    assert format_timestamp(when) == "2026-10-31T12:00:00.123Z"
    assert (
        format_timestamp(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC))
        == "2026-01-02T03:04:05.000Z"
    )


def test_timestamp_format_converts_offsets_and_rejects_naive() -> None:
    tz = dt.timezone(dt.timedelta(hours=2))
    assert format_timestamp(dt.datetime(2026, 1, 1, 2, 0, tzinfo=tz)) == "2026-01-01T00:00:00.000Z"
    with pytest.raises(ValueError):
        format_timestamp(dt.datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("2026-10-31T12:00:00Z", "2026-10-31T12:00:00.000Z"),
        ("2026-10-31T12:00:00.5Z", "2026-10-31T12:00:00.500Z"),
        ("2026-10-31T12:00:00.123456789Z", "2026-10-31T12:00:00.123Z"),
        ("2026-10-31T12:00:00+02:00", "2026-10-31T10:00:00.000Z"),
        ("2026-10-31T12:00:00-05:30", "2026-10-31T17:30:00.000Z"),
        ("2026-10-31t12:00:00z", "2026-10-31T12:00:00.000Z"),
    ],
)
def test_parse_timestamp_accepts_rfc3339_with_any_offset(text: str, iso: str) -> None:
    assert format_timestamp(parse_timestamp(text)) == iso


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026-10-31",
        "2026-10-31T12:00:00",
        "2026-10-31 12:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-31T24:00:00Z",
        "2026-10-31T12:00:60Z",
        "yesterday",
        "2026-10-31T12:00:00+0200",
        "2026-10-31T12:00:00.Z",
        "2026-10-31T12:00:00+25:00",
        "２０２６-10-31T12:00:00Z",
    ],
)
def test_parse_timestamp_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(text)
