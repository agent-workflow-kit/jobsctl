"""Bounded-memory capture of a job's combined output (interface.md, Output capture).

`OutputCapture` keeps the last `tail_bytes` + longest masked value + 4096 raw bytes, counts every
byte it is fed and remembers whether earlier output was dropped. `snapshot` renders the masked,
sanitized tail (`output_tail.render_tail`). The shim feeds it from the pipe and calls `snapshot`
every `tail_flush_interval` (`final=False`) and once after the drain (`final=True`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from jobsctl.env_mask import MaskingSet
from jobsctl.output_tail import render_tail

WINDOW_MARGIN: Final = 4096


@dataclass(frozen=True)
class TailSnapshot:
    text: str
    bytes_seen: int
    truncated: bool


class OutputCapture:
    def __init__(self, masking: MaskingSet, *, tail_bytes: int, tail_lines: int) -> None:
        self._masking = masking
        self._tail_bytes = tail_bytes
        self._tail_lines = tail_lines
        self.window_size = tail_bytes + masking.longest_bytes + WINDOW_MARGIN
        self._buf = bytearray()
        self.bytes_seen = 0
        self.dropped = False

    @property
    def window(self) -> bytes:
        return bytes(self._buf)

    def feed(self, data: bytes) -> None:
        """Append output; keep only the last `window_size` bytes."""
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError("output is bytes")
        self.bytes_seen += len(data)
        if len(data) >= self.window_size:
            self.dropped = self.dropped or len(data) > self.window_size or bool(self._buf)
            self._buf = bytearray(data[len(data) - self.window_size :])
            return
        self._buf += data
        excess = len(self._buf) - self.window_size
        if excess > 0:
            del self._buf[:excess]
            self.dropped = True

    def snapshot(self, *, final: bool) -> TailSnapshot:
        """The masked, sanitized tail. A live snapshot (`final=False`) withholds the end of the
        window; only the final one, taken after the drain, includes the last partial line."""
        rendered = render_tail(
            bytes(self._buf),
            dropped=self.dropped,
            masking=self._masking,
            tail_bytes=self._tail_bytes,
            tail_lines=self._tail_lines,
            final=final,
        )
        return TailSnapshot(rendered.text, self.bytes_seen, rendered.truncated)
