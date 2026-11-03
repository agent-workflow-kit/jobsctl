"""Helpers for driving the CLI in-process."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from jobsctl.cli import main


@dataclass
class Result:
    code: int
    out: str
    err: str

    def envelope(self) -> dict[str, Any]:
        data = json.loads(self.out)
        assert isinstance(data, dict)
        return data


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Result:
    code = main(list(argv))
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)
