"""``cron install`` / ``cron uninstall`` end to end against a fake ``crontab`` program.

The real crontab is never touched: every test puts a fake ``crontab`` first on ``PATH``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobsctl import cron, gateway
from tests.conftest import validate_against
from tests.cron_fakes import FakeCrontab, install_fake_crontab
from tests.helpers import run_cli

JOBSCTL = "/opt/jobsctl bin/jobsctl"
REAL_LOCATE = cron.locate_jobsctl


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    fk = install_fake_crontab(tmp_path, monkeypatch)
    monkeypatch.setattr(cron, "TEMP_ROOTS", ())  # pytest's tmp_path is under /tmp
    monkeypatch.setattr(cron, "locate_jobsctl", lambda argv0, environ: JOBSCTL)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    # the lock and staging files live in the default state dir under the private HOME
    return fk


def _state(home: Path) -> Path:
    return home / ".local" / "state" / "jobsctl"


def install(capsys: pytest.CaptureFixture[str], *flags: str) -> dict[str, object]:
    res = run_cli(capsys, "--json", "cron", "install", *flags)
    assert res.code == 0, res.err + res.out
    doc = res.envelope()
    validate_against("cli/cron.json", doc)
    return doc


def test_install_writes_the_block_and_reports_it(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = install(capsys)
    data = fake.get()
    assert data is not None
    header, entry, rest = data.decode().split("\n")
    assert header == "# jobsctl managed" and rest == ""
    assert entry.startswith("* * * * * PATH='") and entry.endswith(f"'{JOBSCTL}' run --quiet")
    assert doc == {
        "changed": True,
        "dry_run": False,
        "block": header + "\n" + entry,
        "warnings": [],
    }


def test_install_is_idempotent_and_does_not_write_twice(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    install(capsys)
    first = fake.get()
    writes = [c for c in fake.calls() if c != "-l"]
    doc = install(capsys)
    assert doc["changed"] is False
    assert fake.get() == first
    assert [c for c in fake.calls() if c != "-l"] == writes


def test_install_replaces_a_stale_block_in_place(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.set(b"A\n# jobsctl managed\n* * * * * PATH='/old' '/old' run --quiet\nB\n")
    doc = install(capsys)
    assert doc["changed"] is True
    data = fake.get()
    assert data is not None
    lines = data.split(b"\n")
    assert lines[0] == b"A" and lines[1] == b"# jobsctl managed" and lines[3] == b"B"
    assert b"/old" not in data


def test_install_keeps_every_other_byte(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    other = b"# mine\r\nMAILTO=x@example.com\n\n0 5 * * * /bin/b %% \xff\xfe\n@reboot true"
    fake.set(other)
    install(capsys)
    data = fake.get()
    assert data is not None and data.startswith(other + b"\n# jobsctl managed\n")


def test_install_records_xdg_values_and_path_warnings(
    fake: FakeCrontab,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, st = tmp_path / "c o'nfig", tmp_path / "st%ate"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(st))
    good = tmp_path / "good"
    good.mkdir()
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}:relative:{tmp_path}/nope:{good}")
    doc = install(capsys)
    block = str(doc["block"])
    assert f"XDG_CONFIG_HOME='{tmp_path}/c o'\\''nfig'" in block
    assert f"XDG_STATE_HOME='{tmp_path}/st\\%ate'" in block
    assert f"{good}'" in block.split(" XDG_CONFIG_HOME")[0]
    reasons = sorted((w["path"].rsplit("/", 1)[-1], w["reason"]) for w in doc["warnings"])  # type: ignore[index, union-attr]
    assert reasons == [("nope", "missing"), ("relative", "relative")]


def test_install_leaves_out_a_hostile_xdg_value(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x\n* * * * * rm -rf ~")
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 0
    doc = res.envelope()
    validate_against("cli/cron.json", doc)
    assert "rm -rf" not in json.dumps(doc) and "rm -rf" not in (fake.get() or b"").decode()
    assert doc["warnings"][0]["variable"] == "XDG_CONFIG_HOME"


def test_dry_run_changes_nothing_and_shows_the_block(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake.set(b"keep me\n")
    doc = install(capsys, "--dry-run")
    assert doc["dry_run"] is True and doc["changed"] is True and doc["block"]
    assert fake.get() == b"keep me\n"
    assert set(fake.calls()) == {"-l"}
    assert not _state(tmp_path / "home").exists()


def test_dry_run_when_up_to_date_reports_unchanged(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    install(capsys)
    assert install(capsys, "--dry-run")["changed"] is False


def test_dry_run_with_no_crontab_does_not_create_one(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    install(capsys, "--dry-run")
    assert fake.get() is None and set(fake.calls()) == {"-l"}


def test_uninstall_removes_only_the_block(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.set(b"first\nsecond\n")
    install(capsys)
    res = run_cli(capsys, "--json", "cron", "uninstall")
    assert res.code == 0
    doc = res.envelope()
    validate_against("cli/cron.json", doc)
    assert doc == {"changed": True, "dry_run": False, "block": None, "warnings": []}
    assert fake.get() == b"first\nsecond\n"


def test_uninstall_of_a_crontab_with_only_the_block_removes_the_crontab(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    install(capsys)
    assert run_cli(capsys, "cron", "uninstall").code == 0
    assert fake.get() is None


def test_uninstall_without_a_block_or_crontab_is_a_no_op(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    res = run_cli(capsys, "--json", "cron", "uninstall")
    assert res.code == 0 and res.envelope()["changed"] is False
    fake.set(b"x\n")
    res = run_cli(capsys, "--json", "cron", "uninstall")
    assert res.code == 0 and res.envelope()["changed"] is False
    assert fake.get() == b"x\n"
    assert set(fake.calls()) == {"-l"}


@pytest.mark.parametrize("bad", [b"# jobsctl managed\n", b"# jobsctl managed\nfoo\n", b"a\0b\n"])
@pytest.mark.parametrize("verb", ["install", "uninstall"])
def test_unreadable_crontab_is_refused_and_untouched(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], bad: bytes, verb: str
) -> None:
    fake.set(bad)
    res = run_cli(capsys, "--json", "cron", verb)
    assert res.code == 1
    assert res.envelope()["error"]["code"] == "internal"
    assert fake.get() == bad and set(fake.calls()) == {"-l"}


def test_a_failing_crontab_install_leaves_the_crontab_alone(
    fake: FakeCrontab,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake.set(b"precious\n")
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "install")
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "unchanged" in res.envelope()["error"]["message"]
    assert fake.get() == b"precious\n"
    assert fake.calls() == ["-l", "-l", "-"]


def test_a_failing_crontab_read_is_an_error_not_an_empty_crontab(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.set(b"precious\n")
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "list")
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "cannot read the crontab" in res.envelope()["error"]["message"]
    assert fake.get() == b"precious\n" and set(fake.calls()) == {"-l"}


def test_missing_crontab_program_is_an_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setattr(cron, "TEMP_ROOTS", ())
    monkeypatch.setattr(cron, "locate_jobsctl", lambda argv0, environ: JOBSCTL)
    monkeypatch.setenv("PATH", str(empty))
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "not installed" in res.envelope()["error"]["message"]


def test_a_write_that_does_not_read_back_is_reported(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl import gateway

    monkeypatch.setattr(gateway, "crontab_install", lambda data: None)  # a no-op "success"
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "read back" in res.envelope()["error"]["message"]


def test_a_concurrent_edit_between_read_and_write_is_not_clobbered(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl import gateway

    fake.set(b"v1\n")
    real = gateway.crontab_read
    state = {"n": 0}

    def racing() -> bytes | None:
        out = real()
        state["n"] += 1
        if state["n"] == 1:
            fake.set(b"v1\nadded by someone else\n")
        return out

    monkeypatch.setattr(gateway, "crontab_read", racing)
    install(capsys)
    data = fake.get()
    assert data is not None and data.startswith(b"v1\nadded by someone else\n# jobsctl managed\n")


def test_install_without_a_findable_jobsctl_is_an_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no jobsctl, no crontab
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "jobsctl executable" in res.envelope()["error"]["message"]


def test_human_output_prints_the_block_and_warnings_go_to_stderr(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}:/definitely/not/here")
    res = run_cli(capsys, "cron", "install")
    assert res.code == 0
    assert res.out.splitlines()[0] == "# jobsctl managed"
    assert "/definitely/not/here" in res.err and "path_entry_dropped" not in res.out
    assert run_cli(capsys, "--quiet", "cron", "install").out == ""


def test_locate_jobsctl(tmp_path: Path) -> None:
    bindir = tmp_path / "b"
    bindir.mkdir()
    exe = bindir / "jobsctl"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert cron.locate_jobsctl(str(exe), {"PATH": "/usr/bin"}) == str(exe)
    # `python -m jobsctl`: argv[0] is not the script, so PATH decides
    assert cron.locate_jobsctl("/x/jobsctl/__main__.py", {"PATH": str(bindir)}) == str(exe)
    # a relative argv[0] is made absolute against the working directory, symlinks kept
    link = bindir / "link" / "jobsctl"
    link.parent.mkdir()
    os.symlink(exe, link)
    cwd = os.getcwd()
    os.chdir(bindir)
    try:
        assert cron.locate_jobsctl("./link/jobsctl", {"PATH": ""}) == str(
            bindir / "link" / "jobsctl"
        )
    finally:
        os.chdir(cwd)
    with pytest.raises(Exception, match="jobsctl executable"):
        cron.locate_jobsctl("/x/__main__.py", {"PATH": "/nonexistent"})


def test_concurrent_installs_serialize_and_leave_one_block(
    fake: FakeCrontab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real processes racing: every one succeeds and the crontab holds exactly one block."""
    fake.set(b"keep\n")
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    code = (
        "import sys; from jobsctl import cron; "
        f"cron.TEMP_ROOTS = (); cron.locate_jobsctl = lambda a, e: {JOBSCTL!r}; "
        "from jobsctl.cli import main; sys.exit(main(['cron', 'install', '--quiet']))"
    )
    procs = [
        subprocess.Popen([sys.executable, "-I", "-c", code], env=env, stderr=subprocess.PIPE)
        for _ in range(6)
    ]
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()
    data = fake.get()
    assert data is not None
    assert data.count(b"# jobsctl managed\n") == 1 and data.startswith(b"keep\n")


def _is_waiting_on_flock(pid: int) -> bool:
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if "->" in fields and "FLOCK" in fields and str(pid) in fields:
            return True
    return False


def test_a_second_install_blocks_while_the_cron_lock_is_held(
    fake: FakeCrontab, tmp_path: Path
) -> None:
    """Fails without the flock: the second install would finish (and touch the crontab) at once."""
    fake.set(b"keep\n")
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    code = (
        "import sys; from jobsctl import cron; "
        f"cron.TEMP_ROOTS = (); cron.locate_jobsctl = lambda a, e: {JOBSCTL!r}; "
        "from jobsctl.cli import main; sys.exit(main(['cron', 'install', '--quiet']))"
    )
    lock_fd = cron._acquire_lock()
    proc = subprocess.Popen([sys.executable, "-I", "-c", code], env=env, stderr=subprocess.PIPE)
    try:
        try:
            # Wait until the child is demonstrably queued on the lock: /proc/locks lists a
            # blocked waiter as "<n>: -> FLOCK ... <pid> ...".
            deadline = time.monotonic() + 30
            while not _is_waiting_on_flock(proc.pid):
                assert proc.poll() is None, "install exited instead of waiting for cron.lock"
                assert time.monotonic() < deadline, "install never queued on cron.lock"
                time.sleep(0.05)
            assert proc.poll() is None, "install did not wait for cron.lock"
            assert fake.calls() == [] and fake.get() == b"keep\n"
        finally:
            os.close(lock_fd)  # release; the install now proceeds
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
    except BaseException:
        proc.kill()
        proc.wait(timeout=15)
        raise
    data = fake.get()
    assert data is not None and data.count(b"# jobsctl managed\n") == 1


def test_retry_exhaustion_is_an_error_and_writes_nothing(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    count = {"n": 0}

    def always_changing() -> bytes | None:
        count["n"] += 1
        return b"v%d\n" % count["n"]

    monkeypatch.setattr(gateway, "crontab_read", always_changing)
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1
    assert "keeps changing under us" in res.envelope()["error"]["message"]
    assert count["n"] == 6  # three attempts, two reads each
    assert fake.get() is None and fake.calls() == []


def test_no_usable_path_entry_is_an_error(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "relative:")
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "no usable PATH entry" in res.envelope()["error"]["message"]
    assert res.envelope()["error"]["code"] == "internal"


def test_a_jobsctl_path_with_a_control_character_is_an_error(
    fake: FakeCrontab,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exe = tmp_path / "bad\ndir" / "jobsctl"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(cron, "locate_jobsctl", REAL_LOCATE)
    monkeypatch.setattr(cron, "main_argv0", lambda: str(exe))
    fake.set(b"keep\n")
    res = run_cli(capsys, "--json", "cron", "install")
    assert res.code == 1 and "control character" in res.envelope()["error"]["message"]
    assert fake.get() == b"keep\n" and fake.calls() == []


@pytest.mark.parametrize("verb", ["install", "uninstall"])
def test_a_crontab_call_timeout_is_an_error(
    fake: FakeCrontab,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="crontab", timeout=30)

    monkeypatch.setattr(gateway.subprocess, "run", hang)
    res = run_cli(capsys, "--json", "cron", verb)
    assert res.code == 1
    err = res.envelope()["error"]
    assert err["code"] == "internal" and "timed out" in err["message"]


def test_json_reports_a_relative_xdg_value_and_a_path_entry_with_a_newline(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "rel/s")
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}:/evil\n* * * * * rm -rf ~")
    # XDG_STATE_HOME is relative so jobsctl's own state dir falls back to HOME: still private
    doc = install(capsys)
    warnings = doc["warnings"]
    assert isinstance(warnings, list)
    assert {(w.get("variable"), w["reason"]) for w in warnings} == {
        ("XDG_STATE_HOME", "relative"),
        (None, "control"),
    }
    assert "rm -rf" not in str(doc["block"]) and "rm -rf" not in (fake.get() or b"").decode()
    assert len((fake.get() or b"").splitlines()) == 2


def test_a_home_with_a_newline_or_relative_adds_no_local_bin_entry(
    fake: FakeCrontab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "good"
    good.mkdir()
    nl_home = tmp_path / "h\nome"
    (nl_home / ".local" / "bin").mkdir(parents=True)
    rel_home = tmp_path / "rel"
    (rel_home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for home, reason in ((str(nl_home), "control"), ("rel", "relative")):
        result = cron.install(dry_run=True, environ={"PATH": str(good)}, argv0="jobsctl", home=home)
        assert result.block is not None and len(result.block.split("\n")) == 2
        assert f"PATH='{good}'" in result.block
        assert [(w["code"], w["reason"]) for w in result.warnings] == [
            ("path_entry_dropped", reason)
        ]


def test_a_crlf_header_is_refused_by_install_and_uninstall(
    fake: FakeCrontab, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = b"# jobsctl managed\r\n* * * * * PATH='/a' '/j' run --quiet\r\n"
    fake.set(bad)
    for verb in ("install", "uninstall"):
        res = run_cli(capsys, "--json", "cron", verb)
        assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    assert fake.get() == bad and set(fake.calls()) == {"-l"}
