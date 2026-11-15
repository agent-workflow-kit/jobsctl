"""Rendering a masked, sanitized tail from a raw window (interface.md, Output capture and
Masking and sanitizing, steps 1 to 6; spikes B5 and B7).

`render_tail` is pure: it takes the raw bytes the capture keeps and returns text that is safe to
store and show. The same function serves a live flush (`final=False`, which withholds the end of
the window) and the final tail after the drain (`final=True`).

Safety invariants, in pipeline order:

- A masked value is looked for in the decoded text before escape removal and in the stripped
  text after it, and what either finds is masked. A stray escape introducer directly before a
  secret would otherwise swallow the secret's first character in step 3, and a secret that
  contains escape-like bytes is only whole before it; a secret split by an escape sequence is
  only whole after. Each text is scanned once, and its spans serve the discard, the live end
  and the replacement.
- A cut (the discard of a dropped start, the end of a live flush) never falls inside a complete
  masked value: the discard moves forward past it, the live end back to its start.
- A live flush only shows text that later output cannot change: it excludes a trailing
  incomplete UTF-8 sequence and escape sequence, everything after the last newline and at least
  the longest masked value minus one character at the end.
"""

from __future__ import annotations

import codecs
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from jobsctl.env_mask import MaskingSet, replace_spans
from jobsctl.textsafe import (
    escape_unsafe,
    incomplete_escape_start,
    strip_escapes_mapped,
    strip_introducers_mapped,
)

DROP_MARGIN: Final = 256  # step 1: characters discarded beyond the longest masked value

Spans = list[tuple[int, int]]
Strip = Callable[[str], tuple[str, list[int]]]


@dataclass(frozen=True)
class RenderedTail:
    text: str
    # True when the text lacks earlier output: it was dropped from the ring buffer, or cut to
    # the tail size (step 6).
    truncated: bool


def render_tail(
    window: bytes,
    *,
    dropped: bool,
    masking: MaskingSet,
    tail_bytes: int,
    tail_lines: int,
    final: bool,
) -> RenderedTail:
    """Apply steps 1 to 6 to `window`, the kept raw output.

    `dropped` says that output before the window was dropped. A live flush (`final=False`)
    withholds what later output could still change (see the module docstring), so the start of
    a secret still being written is never shown.
    """
    text = _decode(window, final)  # step 2
    if not final:
        text = text[: incomplete_escape_start(text)]
    strips = _loose_strips(masking)
    found = _merge(masking.spans(text) + _decoded_spans(text, strips, masking))
    head = 0
    if dropped and "\n" in text:  # step 1 with a newline: its end, before escapes can hide it
        head = _newline_drop_end(text, masking, found)
    # The discarded head is stripped on its own, so an unterminated sequence in a partial first
    # line cannot hide the rest; the kept text is not cut yet, so a value split by an escape
    # sequence across the discard is still found whole.
    stripped, origin, kept_from = _strip_split(text, head, strip_escapes_mapped)
    loose = _decoded_spans(text, strips, masking, head)
    text = stripped
    spans = _merge(masking.spans(text) + _locate(found + loose, origin))  # step 3 done
    start = 0
    if dropped:  # step 1
        start = kept_from if head else _fallback_end(text, masking)
        start = _past_span(start, spans)
    end = len(text) if final else _live_end(text, masking, spans)
    masked = replace_spans(text[start:end], _clip(spans, start, end))  # step 4
    text, cut = _cut(escape_unsafe(masked), tail_bytes, tail_lines)  # steps 5 and 6
    return RenderedTail(text, dropped or cut)


def _strip_split(text: str, head: int, strip: Strip) -> tuple[str, list[int], int]:
    """`strip` applied to `text[:head]` and `text[head:]` separately: the joined result, each of
    its characters' index in `text`, and where the second part starts in the result."""
    cleaned, origin = strip(text[:head])
    rest, rest_origin = strip(text[head:])
    return cleaned + rest, origin + [i + head for i in rest_origin], len(cleaned)


def _loose_strips(masking: MaskingSet) -> list[Strip]:
    """The other ways of removing escapes that a masked value can be whole in (see
    `_decoded_spans`): a stray ESC loses only itself, everywhere, or only before a character a
    masked value starts with (so escapes inside the value are still removed whole)."""
    firsts = "".join(sorted({v[0] for v in masking.values}))
    return [
        strip_introducers_mapped,
        lambda text: strip_introducers_mapped(text, firsts),
    ]


def _decoded_spans(text: str, strips: list[Strip], masking: MaskingSet, head: int = 0) -> Spans:
    """The masked values found in `text` after each of `strips` (ways of removing escapes other
    than the one step 3 uses), as spans of `text`. A value a stray introducer breaks in step 3
    is whole in one of them."""
    found: Spans = []
    for strip in strips:
        stripped, origin, _ = _strip_split(text, head, strip)
        found += [(origin[a], origin[b - 1] + 1) for a, b in masking.spans(stripped)]
    return found


def _locate(found: Spans, origin: list[int]) -> Spans:
    """`found` (spans of the text before step 3) in the stripped text: each span becomes the
    run of stripped characters that came from it (empty runs are left out)."""
    located = ((bisect_left(origin, a), bisect_left(origin, b)) for a, b in found)
    return [(a, b) for a, b in located if a < b]


def _merge(spans: Spans) -> Spans:
    """Sorted, with overlapping spans joined (adjacent ones stay apart)."""
    merged: Spans = []
    for start, stop in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _decode(window: bytes, final: bool) -> str:
    """Step 2. A live flush leaves out a trailing incomplete UTF-8 sequence: its bytes may
    become one character, so showing them as `\\xNN` would change what was shown."""
    decoder = codecs.getincrementaldecoder("utf-8")("backslashreplace")
    return decoder.decode(window, final)


def _clip(spans: Spans, start: int, end: int) -> Spans:
    """The spans inside `text[start:end]`, shifted to it. Neither cut falls inside a span."""
    return [(a - start, b - start) for a, b in spans if a >= start and b <= end]


def _newline_drop_end(text: str, masking: MaskingSet, spans: Spans) -> int:
    """Step 1 when the window has a newline: the end of the discarded start, through the
    (n+1)-th newline, n being the most newlines in one masked value (so n = 0 is the first
    newline): a value cut at the window's start has at most n newlines left to show. With fewer
    newlines, through the last one, or the fallback's characters if that is later."""
    lines = max((v.count("\n") for v in masking.values), default=0)
    end = -1
    for _ in range(lines + 1):
        end = text.find("\n", end + 1)
        if end < 0:
            return _past_span(max(text.rfind("\n") + 1, _fallback_end(text, masking)), spans)
    return _past_span(end + 1, spans)


def _fallback_end(text: str, masking: MaskingSet) -> int:
    """The first (longest masked value + 256) characters (a character is at least a byte of a
    value, so this covers at least the bytes the spec counts)."""
    return min(masking.longest_bytes + DROP_MARGIN, len(text))


def _past_span(end: int, spans: Spans) -> int:
    """`end`, moved forward past a complete masked value it would cut, whose rest would
    otherwise show."""
    for start, stop in spans:
        if start < end < stop:
            return stop
        if start >= end:
            break
    return end


def _live_end(text: str, masking: MaskingSet, spans: Spans) -> int:
    """Where a live flush ends in the stripped `text`: at the last newline, and at least the
    longest masked value minus one character earlier, never inside a complete masked value.

    One character of `text` is at least one byte of a masked value's characters, so
    `longest_bytes - 1` characters cover every incomplete value at the end. A later byte can only
    change text after the last newline (`\\r` rewrites and escape sequences do not reach back
    across a newline), so everything before the end is final.
    """
    end = text.rfind("\n") + 1
    end = min(end, max(len(text) - max(masking.longest_bytes - 1, 0), 0))
    for start, stop in spans:
        if start < end < stop:
            return start
        if start >= end:
            break
    return end


def _cut(text: str, tail_bytes: int, tail_lines: int) -> tuple[str, bool]:
    """Step 6: keep the last `tail_lines` lines and at most `tail_bytes` bytes, cutting on a
    line boundary. A last line that alone exceeds `tail_bytes` is cut mid-line (after masking,
    so no secret can be cut: only its replacement)."""
    cut = False
    ends_with_newline = text.endswith("\n")
    lines = (text[:-1] if ends_with_newline else text).split("\n")
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
        cut = True
    kept = "\n".join(lines) + ("\n" if ends_with_newline else "")
    data = kept.encode("utf-8")
    if len(data) <= tail_bytes:
        return kept, cut or kept != text
    window = data[-tail_bytes:] if tail_bytes > 0 else b""
    # The window starts on a line boundary if the byte before it is a newline.
    on_boundary = len(data) > tail_bytes and data[-tail_bytes - 1 : -tail_bytes] == b"\n"
    if not on_boundary:
        newline = window.find(b"\n")
        rest = window[newline + 1 :] if newline >= 0 else b""
        if rest.strip(b"\n"):
            window = rest
        else:  # no line boundary with content: keep the end of the last line
            window = _drop_continuation(window)
    return window.decode("utf-8"), True


def _drop_continuation(data: bytes) -> bytes:
    """Drop leading UTF-8 continuation bytes (0x80-0xBF) left by cutting inside a character."""
    i = 0
    while i < len(data) and 0x80 <= data[i] <= 0xBF:
        i += 1
    return data[i:]
