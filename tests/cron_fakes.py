"""A fake ``crontab`` program for tests: the crontab lives in a plain file, never the real one."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

SCRIPT = """#!/bin/sh
# FAKE_CRONTAB: the file holding the crontab (absent: no crontab).
# FAKE_CRONTAB_FAIL: a word; the matching operation (list, install, remove) fails.
# FAKE_CRONTAB_LOG: appended with one line per call.
echo "$*" >> "$FAKE_CRONTAB_LOG"
case "$1" in
-l)
    case "$FAKE_CRONTAB_FAIL" in *list*) echo "crontab: permission denied" >&2; exit 2;; esac
    if [ -e "$FAKE_CRONTAB" ]; then cat "$FAKE_CRONTAB"; exit 0; fi
    echo "crontab: no crontab for fake" >&2
    exit 1
    ;;
-r)
    case "$FAKE_CRONTAB_FAIL" in *remove*) echo "crontab: cannot remove" >&2; exit 2;; esac
    rm -f "$FAKE_CRONTAB"
    ;;
-)
    case "$FAKE_CRONTAB_FAIL" in *install*) echo "crontab: bad crontab" >&2; exit 2;; esac
    cat > "$FAKE_CRONTAB.new" && mv "$FAKE_CRONTAB.new" "$FAKE_CRONTAB"
    ;;
*)
    echo "crontab: bad argument $1" >&2
    exit 2
    ;;
esac
"""


class FakeCrontab:
    def __init__(self, root: Path) -> None:
        self.store = root / "crontab.store"
        self.log = root / "crontab.log"

    def set(self, data: bytes) -> None:
        self.store.write_bytes(data)

    def get(self) -> bytes | None:
        return self.store.read_bytes() if self.store.exists() else None

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def install_fake_crontab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    bindir = tmp_path / "fake-crontab-bin"
    bindir.mkdir()
    script = bindir / "crontab"
    script.write_text(SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    fake = FakeCrontab(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CRONTAB", str(fake.store))
    monkeypatch.setenv("FAKE_CRONTAB_LOG", str(fake.log))
    monkeypatch.delenv("FAKE_CRONTAB_FAIL", raising=False)
    return fake
