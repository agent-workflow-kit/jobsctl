"""Timestamps (interface.md, Timestamps): RFC 3339, UTC, `Z`, milliseconds."""

from __future__ import annotations

import datetime as dt
import random

import pytest

from jobsctl.timestamps import TimestampError, format_timestamp, parse_timestamp, since_cutoff


def test_round_trip_keeps_milliseconds_with_z() -> None:
    rng = random.Random(7)
    for _ in range(500):
        when = dt.datetime(2000, 1, 1, tzinfo=dt.UTC) + dt.timedelta(
            milliseconds=rng.randrange(0, 4_000_000_000_000)
        )
        text = format_timestamp(when)
        assert text.endswith("Z") and len(text) == 24
        assert parse_timestamp(text) == when
        assert format_timestamp(parse_timestamp(text)) == text


def test_sub_millisecond_digits_are_truncated_not_rounded() -> None:
    when = dt.datetime(2026, 1, 1, 0, 0, 0, 999999, tzinfo=dt.UTC)
    assert format_timestamp(when) == "2026-01-01T00:00:00.999Z"


def test_early_years_are_zero_padded() -> None:
    assert format_timestamp(dt.datetime(5, 1, 2, tzinfo=dt.UTC)) == "0005-01-02T00:00:00.000Z"
    assert parse_timestamp("0005-01-02T00:00:00.000Z") == dt.datetime(5, 1, 2, tzinfo=dt.UTC)


def test_offset_input_is_converted_to_utc() -> None:
    assert format_timestamp(parse_timestamp("2026-10-31T23:30:00-05:00")) == (
        "2026-11-01T04:30:00.000Z"
    )
    assert format_timestamp(parse_timestamp("2026-03-08T02:30:00+05:45")) == (
        "2026-03-07T20:45:00.000Z"
    )


def test_output_ignores_the_local_timezone_of_the_input() -> None:
    zone = dt.timezone(dt.timedelta(hours=-7))
    assert format_timestamp(dt.datetime(2026, 11, 1, 1, 30, tzinfo=zone)) == (
        "2026-11-01T08:30:00.000Z"
    )


@pytest.mark.parametrize(
    "text",
    [
        "", "2026-10-31", "2026-10-31T12:00:00", "2026-10-31 12:00:00Z", "2026-02-29T00:00:00Z",
        "2026-10-31T24:00:00Z", "2026-10-31T12:00:60Z", "2026-10-31T12:00:00+24:00",
        "2026-10-31T12:00:00+00:60", "2026-10-31T12:00:00Z\n", "2026-10-31T12:00:00Z\x00",
        " 2026-10-31T12:00:00Z", "２０２６-10-31T12:00:00Z", "2026-10-31T12:00:00.1234567890Z",
        "9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+05:00", "2026-10-31T12:00:00+0200",
    ],
)  # fmt: skip
def test_rejects(text: str) -> None:
    with pytest.raises(TimestampError):
        parse_timestamp(text)


def test_leap_day_is_accepted() -> None:
    assert format_timestamp(parse_timestamp("2024-02-29T00:00:00Z")) == "2024-02-29T00:00:00.000Z"


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError):
        format_timestamp(dt.datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        since_cutoff(5, dt.datetime(2026, 1, 1))


def test_format_overflow_is_a_timestamp_error() -> None:
    edge = dt.datetime(1, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    with pytest.raises(TimestampError):
        format_timestamp(edge)


def test_since_cutoff_uses_the_injected_now() -> None:
    now = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.UTC)
    assert since_cutoff(3600, now) == dt.datetime(2026, 11, 3, 11, 0, tzinfo=dt.UTC)
    assert since_cutoff(100 * 365 * 86400, now).year == 1926


def test_since_cutoff_overflow_is_a_timestamp_error() -> None:
    with pytest.raises(TimestampError):
        since_cutoff(100 * 365 * 86400, dt.datetime(50, 1, 1, tzinfo=dt.UTC))
