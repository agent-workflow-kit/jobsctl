"""Job ids: generation, collisions, prefix resolution (interface.md, Identifiers)."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable

import pytest

from jobsctl import ids
from jobsctl.errors import JobsctlError

JB = re.compile(r"jb-[a-z2-7]{8}")


def fixed(*chunks: bytes) -> Callable[[int], bytes]:
    it = iter(chunks)
    return lambda n: next(it)


def test_generated_ids_match_the_grammar() -> None:
    seen = {ids.new_job_id() for _ in range(2000)}
    assert all(JB.fullmatch(i) for i in seen)
    assert len(seen) == 2000


def test_alphabet_edges_are_reachable_and_exact() -> None:
    assert ids.new_job_id(lambda n: b"\x00" * n) == "jb-aaaaaaaa"
    assert ids.new_job_id(lambda n: b"\xff" * n) == "jb-77777777"


def test_every_character_of_the_alphabet_is_produced() -> None:
    chars = set()
    for v in range(0, 1 << 10):
        raw = (v << 30).to_bytes(5, "big")
        chars.add(ids.new_job_id(lambda n, raw=raw: raw)[3])
    assert chars == set(ids.ID_ALPHABET)


def test_random_source_must_return_the_requested_size() -> None:
    with pytest.raises(ValueError):
        ids.new_job_id(lambda n: b"\x00")


def test_generation_retries_on_a_collision_with_a_live_or_pruned_id() -> None:
    live, pruned = "jb-aaaaaaaa", "jb-aaaaaaab"
    taken = {live, pruned}.__contains__
    source = fixed(b"\x00" * 5, bytes([0, 0, 0, 0, 1]), b"\xff" * 5)
    assert ids.generate_job_id(taken, random_bytes=source) == "jb-77777777"


def test_generation_gives_up_with_internal_after_max_attempts() -> None:
    calls = itertools.count()

    def always(_: str) -> bool:
        next(calls)
        return True

    with pytest.raises(JobsctlError) as exc:
        ids.generate_job_id(always, max_attempts=5)
    assert exc.value.code == "internal"
    assert next(calls) == 5


def test_a_free_id_is_returned_without_retry() -> None:
    assert ids.generate_job_id(lambda _: False, random_bytes=lambda n: b"\x00" * n) == "jb-aaaaaaaa"


@pytest.mark.parametrize(
    ("text", "norm"),
    [
        ("abcd", "jb-abcd"),
        ("jb-abcd", "jb-abcd"),
        ("jb-abcdefgh", "jb-abcdefgh"),
        ("abcdefgh", "jb-abcdefgh"),
        ("jbab", "jb-jbab"),  # no hyphen: `jb` is part of the prefix
        ("2222", "jb-2222"),
        ("7777", "jb-7777"),
    ],
)
def test_normalize(text: str, norm: str) -> None:
    assert ids.normalize_id_input(text) == norm


@pytest.mark.parametrize(
    "text",
    [
        "", "jb-", "jb-abc", "abc", "jb-abcdefghi", "abcdefghi", "JB-abcd", "jb-ABCD", "Abcd",
        "jb-ab1d", "jb-ab8d", "jb-ab0d", "jb-abc\n", "abcd\n", "\nabcd", " abcd", "jb-ab d",
        "jb-jb-abcd", "jb--abcd", "jb_abcd", "ｊb-abcd", "jb-аbcd", "jb-abcd\x00", "jb-ab​cd",
        "jb-é" * 3, "a" * 10_000, "jb-abcd/../x", "jb-abc%", "jb-ab*d", "jb-ab_d",
    ],
)  # fmt: skip
def test_normalize_rejects(text: str) -> None:
    with pytest.raises(ids.IdError):
        ids.normalize_id_input(text)


def finder(*existing: str) -> Callable[[str], list[str]]:
    return lambda prefix: [i for i in existing if i.startswith(prefix)]


def test_unique_prefix_resolves_to_the_full_id() -> None:
    find = finder("jb-abcdefgh", "jb-abxxxxxx")
    assert ids.resolve_job_id("abcd", find) == "jb-abcdefgh"
    assert ids.resolve_job_id("jb-abcdefgh", find) == "jb-abcdefgh"
    assert ids.resolve_job_id("jb-abcde", find) == "jb-abcdefgh"


def test_three_characters_is_usage_exit_2() -> None:
    with pytest.raises(JobsctlError) as exc:
        ids.resolve_job_id("jb-abc", finder("jb-abcdefgh"))
    assert exc.value.code == "usage" and exc.value.exit_code == 2


def test_the_lookup_is_not_called_for_a_malformed_input() -> None:
    def boom(_: str) -> list[str]:
        raise AssertionError("must not look up")

    with pytest.raises(JobsctlError):
        ids.resolve_job_id("ABCD", boom)


def test_several_matches_are_ambiguous_exit_21_with_sorted_candidates() -> None:
    find = finder("jb-abcdzzzz", "jb-abcdaaaa", "jb-abcdmmmm")
    with pytest.raises(JobsctlError) as exc:
        ids.resolve_job_id("abcd", find)
    assert exc.value.code == "ambiguous" and exc.value.exit_code == 21
    assert exc.value.candidates == ["jb-abcdaaaa", "jb-abcdmmmm", "jb-abcdzzzz"]


def test_duplicates_from_the_lookup_do_not_make_a_match_ambiguous() -> None:
    assert ids.resolve_job_id("abcd", lambda p: ["jb-abcdefgh", "jb-abcdefgh"]) == "jb-abcdefgh"


def test_unknown_and_pruned_ids_are_not_found_exit_10() -> None:
    live = finder("jb-abcdefgh")  # a pruned id is simply absent from jobs
    for text in ("jb-zzzzzzzz", "wxyz", "jb-pruned22"):
        with pytest.raises(JobsctlError) as exc:
            ids.resolve_job_id(text, live)
        assert exc.value.code == "not_found" and exc.value.exit_code == 10


def test_a_misbehaving_lookup_is_an_internal_error() -> None:
    for bad in (["jb-zzzzzzzz"], ["not-an-id"], ["jb-abcd"]):
        with pytest.raises(JobsctlError) as exc:
            ids.resolve_job_id("abcd", lambda p, bad=bad: bad)
        assert exc.value.code == "internal"


def test_usage_message_is_sanitized() -> None:
    with pytest.raises(JobsctlError) as exc:
        ids.resolve_job_id("\x1b[31mjb-\r\n", finder())
    assert "\x1b" not in exc.value.message and "\n" not in exc.value.message


def test_usage_message_never_echoes_a_value_after_equals() -> None:
    with pytest.raises(JobsctlError) as exc:
        ids.resolve_job_id("abcd=S3CRET", finder())
    assert exc.value.code == "usage"
    assert "'abcd=...'" in exc.value.message
    assert "S3CRET" not in exc.value.message


def test_is_job_id() -> None:
    assert ids.is_job_id("jb-abcdefgh")
    for text in ("jb-abcdefg", "jb-abcdefgh\n", "jb-ABCDEFGH", "abcdefgh", "jb-abcdefg1"):
        assert not ids.is_job_id(text)


def test_error_envelopes_validate_against_the_schema() -> None:
    from tests.conftest import validate_against

    for text, find in (
        ("abc", finder()),
        ("zzzz", finder()),
        ("abcd", finder("jb-abcdaaaa", "jb-abcdbbbb")),
    ):
        with pytest.raises(JobsctlError) as exc:
            ids.resolve_job_id(text, find)
        validate_against("cli/error.json", exc.value.envelope())


def test_label_limit_envelope_validates_against_the_schema() -> None:
    from jobsctl import validate
    from tests.conftest import validate_against

    with pytest.raises(JobsctlError) as exc:
        validate.merge_labels([(f"k{i}", "v") for i in range(32)], [("x", "y")])
    validate_against("cli/error.json", exc.value.envelope())
