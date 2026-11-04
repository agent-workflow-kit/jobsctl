"""The masking set: the secret values masked in tails, hook output and logs.

Per docs/interface.md, Masking and sanitizing, step 4: the values of the `mask_env` variables (in
the canonical environment of the masking process), of every variable defined in `env_file`, and,
for a shim, of the `--pass-env` values it read at start; the union of those reads. Values of
fewer than 8 characters are not masked (`short_secret_not_masked`, naming the variable); an
empty value is neither masked nor warned about. A multi-line value is masked line by line.

This module owns the *set* and the replacement of its members in text. The rest of the masking
pipeline (windowing, decoding, escape removal, sanitizing) is `output_tail.py`, which uses
`MaskingSet.mask`, `MaskingSet.spans` and `MaskingSet.longest_bytes`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from jobsctl.config import Config
from jobsctl.env_canonical import CanonicalEnv
from jobsctl.env_file import EnvFile, EnvWarning

MIN_MASKED_LENGTH: Final = 8
REPLACEMENT: Final = "***"
_OVERLAP_REACH: Final = 256  # see MaskingSet.spans
_MAX_NESTING: Final = 48  # well inside the regex compiler's recursion limit


@dataclass(frozen=True)
class MaskingSet:
    """Distinct masked values, longest first. Values are kept out of the repr."""

    values: tuple[str, ...] = field(repr=False)
    warnings: tuple[EnvWarning, ...] = ()
    _pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    _overlap: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    @property
    def longest_bytes(self) -> int:
        """UTF-8 length of the longest masked value (0 if none): the output window rules use it."""
        return max((len(v.encode("utf-8", "replace")) for v in self.values), default=0)

    def contains_secret(self, text: str) -> bool:
        """Whether `text` contains a masked value (`add`'s `secret_in_argv`/`secret_in_env`)."""
        return self._pattern is not None and self._pattern.search(text) is not None

    def spans(self, text: str) -> list[tuple[int, int]]:
        """The `(start, end)` intervals `mask` replaces, in order and disjoint.

        Leftmost-longest matches are found first (one linear pass). A value that starts inside
        a match and ends after it (`abcdefgh` and `fghijklm` in `abcdefghijklm`) extends the
        match, so no part of any occurrence is left unmasked. Only values starting within the
        last `_OVERLAP_REACH` characters of a match are looked for: trying every position of a
        long match costs the square of its length."""
        if self._pattern is None or self._overlap is None:
            return []
        merged: list[tuple[int, int]] = []
        for m in self._pattern.finditer(text):
            start, end = m.span()
            pos = max(start + 1, end - _OVERLAP_REACH)
            while pos < end:
                at = self._overlap.match(text, pos)
                if at is not None and at.end(1) > end:
                    end = at.end(1)
                    pos = max(pos + 1, end - _OVERLAP_REACH)
                else:
                    pos += 1
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def mask(self, text: str) -> str:
        """Replace every occurrence of a masked value with `***` (see `spans`); replaced text is
        never scanned again."""
        return replace_spans(text, self.spans(text))


def replace_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """`text` with each of the sorted, disjoint `spans` replaced by `***`."""
    if not spans:
        return text
    parts: list[str] = []
    pos = 0
    for start, stop in spans:
        parts.append(text[pos:start])
        parts.append(REPLACEMENT)
        pos = stop
    parts.append(text[pos:])
    return "".join(parts)


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
    trie = _trie_pattern(sorted(chosen), 0)
    # TODO(#8): the walk costs a few microseconds per position when many values share a long
    # prefix with the text (190 values "a"*k+"b" on 20000 "a" take about 1 s): an Aho-Corasick
    # automaton would be linear.
    pattern = re.compile(trie) if ordered else None
    overlap = re.compile("(?=(" + trie + "))") if ordered else None
    return MaskingSet(ordered, tuple(warnings), pattern, overlap)


def _trie_pattern(strings: list[str], depth: int) -> str:
    """A regex matching the longest of `strings` (sorted, distinct; `""` means "a value may end
    here") at a position, shaped as a prefix tree so its cost per text position does not grow
    with the number of values.

    Each node lists its children (distinct first characters, so at most one can match) before
    the empty end-of-value option, so the regex engine settles on the longest value along the one
    path the text selects. Chains are collapsed into literals. Below `_MAX_NESTING` levels the
    rest of a subtree is a flat alternation, longest first (same result, no deeper nesting), so a
    hostile set of values that prefix each other cannot exhaust the regex compiler's recursion.
    """
    if depth >= _MAX_NESTING:
        return "(?:" + "|".join(re.escape(s) for s in sorted(strings, key=len, reverse=True)) + ")"
    branches: list[str] = []
    ends_here = False
    i = 0
    while i < len(strings):
        first = strings[i]
        if first == "":
            ends_here = True
            i += 1
            continue
        j = i + 1
        while j < len(strings) and strings[j][0] == first[0]:
            j += 1
        group = strings[i:j]
        prefix = os.path.commonprefix([group[0], group[-1]])  # of the whole group, as it is sorted
        branch = re.escape(prefix)
        if len(group) > 1:
            branch += _trie_pattern([g[len(prefix) :] for g in group], depth + 1)
        branches.append(branch)
        i = j
    if ends_here:
        branches.append("")
    return "(?:" + "|".join(branches) + ")"
