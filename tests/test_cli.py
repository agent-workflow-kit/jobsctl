from __future__ import annotations

import pytest

from jobsctl.cli import main


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "jobsctl 0.0.0"


def test_no_arguments_returns_usage_exit_code() -> None:
    assert main([]) == 2
