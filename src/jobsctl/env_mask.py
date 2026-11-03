"""The masking set: the secret values masked in tails, hook output and logs.

Per docs/interface.md, Masking and sanitizing, step 4: the values of the `mask_env` variables (in
the canonical environment of the masking process), of every variable defined in `env_file`, and,
for a shim, of the `--pass-env` values it read at start; the union of those reads. Values of
fewer than 8 characters are not masked (`short_secret_not_masked`, naming the variable); an
empty value is neither masked nor warned about. A multi-line value is masked line by line.

This module owns the *set* and the replacement of its members in text. The rest of the masking
pipeline (windowing, decoding, escape removal, sanitizing) belongs to the output module
(TODO(#16)), which uses `MaskingSet.mask` and `MaskingSet.longest_bytes`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from jobsctl.config import Config
from jobsctl.env_canonical import CanonicalEnv
from jobsctl.env_file import EnvFile, EnvWarning

MIN_MASKED_LENGTH: Final = 8
REPLACEMENT: Final = "***"


@dataclass(frozen=True)
class MaskingSet:
    """Distinct masked values, longest first. Values are kept out of the repr."""

    values: tuple[str, ...] = field(repr=False)
    warnings: tuple[EnvWarning, ...] = ()
    _pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    @property
    def longest_bytes(self) -> int:
        """UTF-8 length of the longest masked value (0 if none): the output window rules use it."""
        return max((len(v.encode("utf-8", "replace")) for v in self.values), default=0)

    def contains_secret(self, text: str) -> bool:
        """Whether `text` contains a masked value (`add`'s `secret_in_argv`/`secret_in_env`)."""
        return self._pattern is not None and self._pattern.search(text) is not None

    def mask(self, text: str) -> str:
        """Replace every occurrence of a masked value with `***` in one left-to-right pass: at a
        position the longest value wins, and replaced text is never scanned again."""
        if self._pattern is None:
            return text
        return self._pattern.sub(REPLACEMENT, text)


def _candidates(value: str) -> list[str]:
    """The strings to mask for one value: the value, plus each of its lines if it has several."""
    found = [value]
    if "\n" in value or "\r" in value:
        for line in re.split(r"\r\n|\r|\n", value):
            if line != value:
                found.append(line)
    return found


def masking_values(
    config: Config,
    canonical: CanonicalEnv,
    env_file: EnvFile,
    pass_env_values: Mapping[str, str] | None = None,
) -> MaskingSet:
    """Build the masking set from `mask_env` (looked up in `canonical`), every `env_file` value
    and the `--pass-env` values a shim read at start."""
    named: list[tuple[str, str]] = []
    for name in config.mask_env:
        if name in canonical.values:
            named.append((name, canonical.values[name]))
    named.extend(env_file.values.items())
    named.extend((pass_env_values or {}).items())

    chosen: set[str] = set()
    warnings: list[EnvWarning] = []
    warned: set[str] = set()
    for name, value in named:
        if value == "":
            continue
        if len(value) < MIN_MASKED_LENGTH:
            if name not in warned:
                warned.add(name)
                warnings.append(EnvWarning("short_secret_not_masked", variable=name))
            continue
        chosen.update(c for c in _candidates(value) if len(c) >= MIN_MASKED_LENGTH)

    ordered = tuple(sorted(chosen, key=lambda v: (-len(v), v)))
    pattern = re.compile("|".join(re.escape(v) for v in ordered)) if ordered else None
    return MaskingSet(ordered, tuple(warnings), pattern)
