"""Rendering a masked, sanitized tail from a raw window (interface.md, Output capture and
Masking and sanitizing, steps 1 to 6; spikes B5 and B7).

`render_tail` is pure: it takes the raw bytes the capture keeps and returns text that is safe to
store and show. The same function serves a live flush (`final=False`, which withholds the end of
the window) and the final tail after the drain (`final=True`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from jobsctl.env_mask import MaskingSet
from jobsctl.textsafe import escape_unsafe, strip_escapes

DROP_MARGIN: Final = 256  # step 1: bytes discarded beyond the longest masked value


@dataclass(frozen=True)
class RenderedTail:
    text: str
    # True when the text lacks earlier output: the capture dropped it, or the tail size cut it.
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
    withholds everything after the last newline and, at the end, at least the longest masked
    value minus one byte, so the start of a secret still being written is never shown.
    """
    text = strip_escapes(window.decode("utf-8", "backslashreplace"))  # steps 2 and 3
    if dropped:  # step 1, on the decoded text so no masked value is cut by the discard
        text = text[_drop_end(text, masking) :]
    if not final:
        text = text[: _live_end(text, masking)]
        masked = masking.mask(text)
    else:
        masked = masking.mask(text)  # step 4
    text, cut = _cut(escape_unsafe(masked), tail_bytes, tail_lines)  # steps 5 and 6
    return RenderedTail(text, dropped or cut)


def _drop_end(text: str, masking: MaskingSet) -> int:
    """Step 1: the end of the discarded start: through the first newline, or the first
    (longest masked value + 256) characters when there is none (a character is at least a
    byte of a value, so this covers at least the bytes the spec counts). The discard moves
    forward past a complete masked value it would cut, whose rest would otherwise show."""
    newline = text.find("\n")
    end = newline + 1 if newline >= 0 else min(masking.longest_bytes + DROP_MARGIN, len(text))
    for start, stop in masking.spans(text):
        if start < end < stop:
            return stop
        if start >= end:
            break
    return end


def _live_end(text: str, masking: MaskingSet) -> int:
    """Where a live flush ends in `text` (after steps 2 and 3): at the last newline, and at least
    the longest masked value minus one byte earlier, never inside a complete masked value.

    One character of `text` is at least one byte of a masked value's characters, so
    `longest_bytes - 1` characters cover every incomplete value at the end. A later byte can only
    change text after the last newline (`\\r` rewrites and escape sequences do not reach back
    across a newline), so everything before the end is final.
    """
    end = text.rfind("\n") + 1
    end = min(end, max(len(text) - max(masking.longest_bytes - 1, 0), 0))
    for start, stop in masking.spans(text):
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
