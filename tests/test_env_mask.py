"""The masking set: which values are masked, what is not, and how text is masked."""

from __future__ import annotations

import random
from pathlib import Path

from jobsctl import env_canonical as ec
from jobsctl import env_file as ef
from jobsctl import env_mask as em
from jobsctl.config import Config


def cfg(**kw: object) -> Config:
    return Config.model_validate(kw)


def file_with(tmp_path: Path, body: str) -> ef.EnvFile:
    d = tmp_path / "s"
    d.mkdir(mode=0o700, exist_ok=True)
    p = d / "env"
    p.write_text(body)
    p.chmod(0o600)
    return ef.load_env_file(str(p), check_ancestors_from=tmp_path)


NOFILE = ef.EnvFile("not_configured")


def mset(
    config: Config,
    canonical: dict[str, str] | None = None,
    env_file: ef.EnvFile = NOFILE,
    pass_env: dict[str, str] | None = None,
) -> em.MaskingSet:
    return em.masking_values(config, ec.CanonicalEnv(canonical or {}), env_file, pass_env or {})


def test_mask_env_values_from_canonical_environment() -> None:
    result = mset(
        cfg(mask_env=["TOK", "ABSENT"]), {"TOK": "tok-value-1234", "OTHER": "other-value-1"}
    )
    assert result.mask("x tok-value-1234 y other-value-1") == "x *** y other-value-1"


def test_every_env_file_value_is_masked_whatever_its_name(tmp_path: Path) -> None:
    file = file_with(tmp_path, "PATH=/secret/path-value\nA=aaaaaaaa\n")
    assert mset(cfg(), {}, file).mask("aaaaaaaa /secret/path-value") == "*** ***"


def test_pass_env_values_added() -> None:
    assert mset(cfg(), {}, NOFILE, {"X": "passed-value"}).mask("passed-value") == "***"


def test_union_of_reads_masks_both_old_and_new_value(tmp_path: Path) -> None:
    file = file_with(tmp_path, "T=new-value-xx\n")
    result = mset(cfg(), {}, file, {"T": "old-value-xx"})
    assert result.mask("old-value-xx new-value-xx") == "*** ***"


def test_unavailable_env_file_adds_nothing() -> None:
    assert mset(cfg(), {}, ef.EnvFile("unavailable")).values == ()


def test_short_values_not_masked_and_warned(tmp_path: Path) -> None:
    file = file_with(tmp_path, "SHORT=1234567\nEXACT=12345678\n")
    result = mset(cfg(mask_env=["M"]), {"M": "abc"}, file, {"P": "pw"})
    assert result.mask("1234567 12345678 abc pw") == "1234567 *** abc pw"
    assert sorted((w.code, w.variable) for w in result.warnings) == [
        ("short_secret_not_masked", "M"),
        ("short_secret_not_masked", "P"),
        ("short_secret_not_masked", "SHORT"),
    ]


def test_length_counts_characters_not_bytes() -> None:
    result = mset(cfg(), {}, NOFILE, {"U": "é" * 7, "V": "é" * 8})
    assert result.mask("é" * 7 + " " + "é" * 8) == "ééééééé ***"


def test_empty_values_are_neither_masked_nor_warned(tmp_path: Path) -> None:
    file = file_with(tmp_path, "E=\n")
    result = mset(cfg(mask_env=["M"]), {"M": ""}, file, {"P": ""})
    assert result.values == ()
    assert result.warnings == ()
    assert result.mask("abc") == "abc"


def test_warning_carries_name_never_value(tmp_path: Path) -> None:
    result = mset(cfg(), {}, NOFILE, {"P": "pw"})
    assert "pw" not in repr(result.warnings).replace("P", "")  # only the name "P"
    assert result.warnings[0].variable == "P"


def test_longest_first_and_overlapping_values() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "abcdefgh", "B": "abcdefghijkl", "C": "efghijklmnop"})
    # leftmost match wins; at one position the longest does
    assert result.mask("abcdefghijkl") == "***"
    assert result.mask("xx abcdefgh yy") == "xx *** yy"
    assert result.mask("abcdefghijklmnop") == "***mnop"
    assert result.values[0] == "abcdefghijkl"


def test_regex_special_values_are_literal() -> None:
    value = r"a.*+?^${}()|[]\\b"
    result = mset(cfg(), {}, NOFILE, {"A": value})
    assert result.mask(f"[{value}]") == "[***]"
    assert result.mask("a" * 20) == "a" * 20


def test_masked_text_does_not_remask_inside_replacement() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "********"})
    assert result.mask("********") == "***"


def test_multiline_value_masked_line_by_line(tmp_path: Path) -> None:
    pem = "-----BEGIN KEY-----\nAAAAAAAAAAAAAAAA\nshort\nBBBBBBBBBBBBBBBB\n-----END KEY-----"
    result = mset(cfg(), {}, NOFILE, {"K": pem})
    out = result.mask("log: " + pem + "\nlater AAAAAAAAAAAAAAAA and BBBBBBBBBBBBBBBB and short")
    assert "AAAA" not in out and "BBBB" not in out and "BEGIN" not in out
    assert "short" in out.splitlines()[-1]
    # CRLF output of a multi-line value
    assert result.mask("AAAAAAAAAAAAAAAA\r\n") == "***\r\n"


def test_longest_masked_value_for_window_discard() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "abcdefgh", "B": "é" * 20})
    assert result.longest_bytes == 40
    assert mset(cfg()).longest_bytes == 0


def test_no_values_masks_nothing() -> None:
    assert mset(cfg()).mask("anything") == "anything"
    assert mset(cfg()).mask("") == ""


def test_dedupes_values() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "same-value-1", "B": "same-value-1"})
    assert result.values == ("same-value-1",)


def test_repr_hides_values() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "super-secret-value"})
    assert "super-secret-value" not in repr(result)
    assert "super-secret-value" not in str(result)


def test_contains_masked_value_for_add_refusal() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "super-secret-value"})
    assert result.contains_secret("curl -H super-secret-value")
    assert not result.contains_secret("curl -H nothing")


def test_hostile_large_input_is_linear_enough() -> None:
    result = mset(cfg(), {}, NOFILE, {"A": "a" * 8 + "b"})
    text = "a" * 2_000_000
    assert result.mask(text) == text


def test_random_embeddings_always_masked() -> None:
    rng = random.Random(1234)
    secrets = ["s3cr3t-token-A", "pa$$word.*1234", "tokén-中文-xx"]
    result = mset(cfg(), {}, NOFILE, dict(enumerate(map(str, secrets))))  # type: ignore[arg-type]
    for _ in range(200):
        pieces = [
            rng.choice(secrets + ["noise", " ", "\n", "x" * rng.randint(0, 9)]) for _ in range(8)
        ]
        out = result.mask("".join(pieces))
        for s in secrets:
            assert s not in out


def test_huge_value_and_many_values() -> None:
    big = "x" * (1024 * 1024 - 1)
    many = {f"V{i}": f"value-number-{i:06d}" for i in range(3000)}
    result = mset(cfg(), {}, NOFILE, {"BIG": big, **many})
    assert result.mask("a" + big + "b value-number-002999") == "a***b ***"
