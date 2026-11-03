"""Hook output capture, excerpts (masked, sanitized, cut after masking) and decision ids."""

from __future__ import annotations

import json

import pytest

from jobsctl.hook_output import (
    CAPTURE_LIMIT,
    MAX_DECISION_IDS,
    Capture,
    excerpt,
    parse_decision_ids,
)

SECRET = "s3cr3t-VALUE-9f2"


def mask(text: str) -> str:
    return text.replace(SECRET, "***")


def cap(data: bytes, limit: int = CAPTURE_LIMIT) -> Capture:
    c = Capture(limit)
    c.add(data)
    return c


def test_capture_keeps_the_first_bytes_and_counts_the_rest() -> None:
    c = Capture(10)
    for chunk in (b"abcdef", b"ghijkl", b"mn"):
        c.add(chunk)
    assert c.data == b"abcdefghij" and c.seen == 14 and c.truncated


def test_capture_exactly_at_the_limit_is_not_truncated() -> None:
    assert not cap(b"x" * 10, 10).truncated
    assert cap(b"x" * 11, 10).truncated


def test_zero_length_output() -> None:
    c = cap(b"")
    assert c.data == b"" and not c.truncated
    assert excerpt(c, 2048, mask) == ""
    assert parse_decision_ids(c, mask) == ([], [])


def test_excerpt_masks_and_sanitizes() -> None:
    raw = f"\x1b[31mfail\x1b[0m token={SECRET}\x07\x00".encode() + b"\xff end"
    out = excerpt(cap(raw), 2048, mask)
    assert out == "fail token=***\\u0007\\u0000\\xff end"
    assert SECRET not in out and "\x1b" not in out


def test_excerpt_cut_follows_masking_never_splits_a_mask_or_a_character() -> None:
    out = excerpt(cap((SECRET + "é").encode() * 1000), 10, mask)
    assert len(out.encode()) <= 10 and SECRET[:6] not in out
    out = excerpt(cap("é".encode() * 100), 5, mask)
    assert out == "éé" and len(out.encode()) == 4


def test_a_truncated_capture_drops_its_last_partial_line() -> None:
    # the cut could hold the start of a masked value: never show it
    data = b"first\n" + SECRET.encode()[:8]
    c = Capture(len(data))
    c.add(data + b"more")
    assert c.truncated
    assert excerpt(c, 2048, mask) == "first\n"
    one_line = Capture(5)
    one_line.add(b"abcdefghij")
    assert excerpt(one_line, 2048, mask) == ""


def test_excerpt_rewritten_lines_keep_the_last_segment() -> None:
    assert excerpt(cap(b"progress 1\rprogress 2\r\nok\n"), 2048, mask) == "progress 2\nok\n"


def ids(doc: object) -> tuple[list[str], list[str]]:
    return parse_decision_ids(cap(json.dumps(doc).encode()), mask)


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ({"decision_id": "abc"}, ["abc"]),
        ({"decision_id": ["a", "b.c:d@e/f-g_h"]}, ["a", "b.c:d@e/f-g_h"]),
        ({"decision_id": ["a", "a", "b"]}, ["a", "b"]),
        ({"decision_id": "x" * 128}, ["x" * 128]),
        ({"decision_id": []}, []),
        ({"other": 1}, []),
        ({"decision_id": None}, []),
    ],
)
def test_valid_decision_ids(doc: object, expected: list[str]) -> None:
    got, bad = ids(doc)
    assert got == expected and (bad == [] or doc == {"decision_id": None})


@pytest.mark.parametrize(
    "value",
    ["", "x" * 129, "has space", "new\nline", "ünï", "a;b", 5, True, {"a": 1}, ["nested"], None],
)
def test_invalid_decision_ids_are_dropped_with_a_detail(value: object) -> None:
    got, bad = ids({"decision_id": ["ok", value]})
    assert got == ["ok"] and len(bad) == 1


def test_invalid_detail_is_sanitized_masked_and_short() -> None:
    got, bad = ids({"decision_id": [f"\x1b[31m{SECRET} line\nbreak" + "z" * 500]})
    assert got == []
    assert len(bad[0].encode()) <= 64 and SECRET not in bad[0]
    assert "\n" not in bad[0] and "\x1b" not in bad[0]


def test_too_many_decision_ids_are_capped() -> None:
    got, bad = ids({"decision_id": [f"id{n}" for n in range(MAX_DECISION_IDS + 5)]})
    assert len(got) == MAX_DECISION_IDS and len(bad) == 5


@pytest.mark.parametrize(
    "raw",
    [
        b"plain text",
        b"[1,2]",
        b'"decision_id"',
        b"{",
        b'{"decision_id": "a"',
        b"\xff\xfe",
        b'{"decision_id": "a\xff"}',
        b"[" * 100000,
        b"\x00",
        b'{"decision_id": NaN}',
    ],
)
def test_non_json_or_wrong_shaped_stdout_yields_nothing_quietly(raw: bytes) -> None:
    assert parse_decision_ids(cap(raw), mask)[0] == []


def test_truncated_stdout_is_not_parsed() -> None:
    doc = json.dumps({"decision_id": "abc", "pad": "x" * 100}).encode()
    c = Capture(len(doc) - 1)
    c.add(doc)
    assert parse_decision_ids(c, mask) == ([], [])


def test_surrounding_whitespace_is_fine() -> None:
    assert parse_decision_ids(cap(b'\n  {"decision_id": "a"}  \n'), mask) == (["a"], [])
