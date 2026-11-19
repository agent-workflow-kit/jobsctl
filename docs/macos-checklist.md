# macOS checklist

Status: **nothing here has been run on a Mac.** jobsctl is developed and tested on Linux. This document
has three parts: what the code does on macOS and how much of that is known ([static audit](#static-audit)),
what a human has to check on a Mac before 1.0 ([checklist](#checklist)), and a [results template](#results-template).
Issue: [#28](https://github.com/agent-workflow-kit/jobsctl/issues/28). The issue is closed only when
this checklist has been run and its results recorded there.

Evidence levels used below:

- **Linux**: a test runs on Linux with fakes (libproc replaced by a fake object, `os.waitid` removed). It proves the
  logic, not the macOS system call behind it.
- **Static**: read from the code; no test.
- **Mac needed**: unknown until someone runs it on a Mac.

## Static audit

| Path | Code | What it does on darwin | Evidence | Risk |
|---|---|---|---|---|
| Process identity | `proc_host.DarwinHost.process_info`, `gateway_proc.DarwinLibproc.bsdinfo` | `proc_pidinfo(PROC_PIDTBSDINFO)` into a 136-byte `proc_bsdinfo`; start time is `pbi_start_tvsec * 10^6 + pbi_start_tvusec`; state is `Z` only for `SZOMB` | Linux (fake lib, `bsdinfo` through `DarwinLibproc` with a fake `libproc`, including the `EIO` size and pid check); struct layout, constants and stability of the start time: Mac needed | A wrong struct layout reads garbage; the size check (`got != size`, `pbi_pid != pid`) turns that into `EIO`, i.e. unknown, never dead |
| Liveness, "gone" | `bsdinfo` | A return of 0 with `errno` 0 or `ESRCH` means no such process | Mac needed | If macOS returns 0 for a process we may not inspect (sandbox, hardened runtime), a live process reads as gone and `reconcile` could mark a job `lost`. Check with a process of another user |
| Zombie visibility | `bsdinfo`, `wait_state` | A zombie must still answer `proc_pidinfo` with `pbi_status == SZOMB`. A miss with `arg` 0 (`ESRCH`, or a return of 0 with `errno` 0) is retried once with `arg` 1 (xnu is believed to look up a zombie only then) | Linux: the retry, with a fake `libproc` (`test_a_zombie_that_answers_only_with_arg_1_is_read_by_the_retry`); that macOS answers for a zombie at all, and with which `arg`: **needs a Mac** | If it answers `ESRCH` for both, a held zombie leader reads as dead: `signal_group(allow_zombie=True)` refuses, `adopt_shims` does not adopt a zombie shim |
| Boot id | `DarwinLibproc.boot_uuid` | `/usr/sbin/sysctl -n kern.bootsessionuuid` through `gateway.run`, 5 s timeout; empty, non-zero exit, missing binary or timeout is `BootIdUnavailable` (liveness unknown) | Linux (fake `gateway.run`: non-zero exit is empty, missing binary and timeout propagate and `DarwinHost.boot_id` maps them to unavailable); real value: Mac needed | None found statically; the absolute path avoids cron's short `PATH` |
| Held-leader wait | `gateway_proc.waitid_nowait` -> `emulated_waitid_nowait` | `os.waitid` does not exist on macOS CPython. The shim, `_leader_exited`, `_own_unreaped_child` and `run_hook` all call it. It now reads the zombie state and wait status (`pbi_xstatus`) from libproc instead of `waitid(WEXITED\|WNOWAIT)`; a non-child or vanished PID raises `ChildProcessError(ECHILD)`; a blocking wait polls every 20 ms | Linux (fake state, decoding of exit 0, 7, 255, signals and a core dump, errors); that `pbi_xstatus` holds the wait status of an exited process: **needs a Mac** (the live test checks exit 7, exit 3 and SIGKILL through the emulated path) | **Before this branch the shim could not even be imported on macOS** (`shim_pump` named `os.waitid_result` at import time) and every wait raised `ENOSYS`. If `pbi_xstatus` is not the wait status, exit codes are wrong; the live tests check exits 7 and 3 and SIGKILL. Fallback if libproc cannot see zombies: kqueue `EVFILT_PROC` with `NOTE_EXIT`/`NOTE_EXITSTATUS` |
| Signaling | `proc_signal`, `gateway_proc.kill/killpg` | No pidfd (`pidfd_supported()` is False): identity re-check, then `kill`/`killpg` | Linux (fake host) | Documented window between the check and the signal for processes jobsctl did not spawn ([Residual risks](interface.md#residual-risks)) |
| pidfd | `gateway_proc.pidfd_*` | Guarded by `sys.platform != "linux"`; never called on darwin | Linux | None |
| Process groups | `gateway.spawn_job` (`process_group=0`), `DarwinHost.scan_group` | New group per job; members listed with `proc_listpids(PROC_PGRP_ONLY)`, each read with `proc_pidinfo` | Linux (fake lib; `proc_listpids` size handling, with a fake `libproc`: a buffer that fills is retried larger, up to 5 times); the real listing size semantics: **needs a Mac** | A listing that fails reads as an empty group (no error is raised for `group_pids`): a count of 0 would then be wrong. This asymmetry with `child_pids` (which raises) is deliberate and kept: the group listing is probed again by later passes. Verified only by the live test |
| `scan_children` | `DarwinHost.scan_children`, `child_pids` | **New.** `proc_listpids(PROC_PPID_ONLY=6)` then each PID read and filtered by `ppid`; a failed listing, in either `proc_listpids` call, or one that keeps outgrowing its buffer, is an incomplete scan | Linux (fake lib); constant and listing: needs a Mac | macOS has no child subreaper, so an orphan goes to `launchd` and is **not found**: only live or zombie direct children of the shim count. This is a limit of the platform, documented in [Platform notes](interface.md#platform-notes) |
| Dumpability | `proc_linux.set_undumpable`, `env_file.disable_dumpable` | No-ops off Linux (`PR_SET_DUMPABLE` does not exist). There is no `/proc/<pid>/environ` on macOS; whether another same-user process can read a process's environment some other way (for example `sysctl` `KERN_PROCARGS2`) was not checked | Static | The `env_file` secrets are not protected from a same-user process the way they are on Linux. Recorded under [Residual risks](interface.md#residual-risks); the `KERN_PROCARGS2` question needs a Mac |
| Child subreaper | `proc_linux.set_child_subreaper` | No-op (returns False) | Linux (`test_non_linux_helpers_are_noops`) | See `scan_children` |
| `flock` | `gateway.flock_exclusive`, `log_files`, `cron` lock | `fcntl.flock` on local APFS is advisory and exclusive; the lock dies with the process | Static; live test: 12 concurrent `add` | NFS and other network volumes may emulate `flock` weakly; deliberately not tested (see [Not covered](#not-covered)). The state directory must be on a local volume |
| Durability | `os.fsync` in `result_file`, `log_files`, SQLite | On macOS `fsync` does not force the drive cache to media (`F_FULLFSYNC` does) | Static | Only matters for power loss; Linux already accepts `synchronous=NORMAL` ([Durability](interface.md#residual-risks)). Not changed |
| Temp directories | `cron.TEMP_ROOTS`, `env_canonical._TEMP_ROOTS` | **Changed.** `PATH` entries under `/tmp`, `/run/user`, `/private/tmp`, `/var/folders` and `/private/var/folders` are dropped (the lexical and the real path are both checked, which already covered `/tmp` -> `/private/tmp`) | Linux (`test_darwin_paths.py`) | A `PATH` entry under `/var/tmp` is kept, as on Linux. `TMPDIR` on macOS points into `/var/folders`; it is inherited by jobs but is not a `PATH` entry |
| `crontab` helpers | `gateway.crontab_read/install/remove` | `crontab -l`, `crontab -` (stdin), `crontab -r` with `LC_ALL=C`; "no crontab for" is matched on stderr | Linux (fake `crontab`); the real program: Mac needed | macOS `crontab` may print a different "no crontab" message or refuse `-` without Full Disk Access; the checklist exercises each. macOS cron is not enabled by default |
| Locale and encoding | `gateway` (bytes in and out), `textsafe` | Text is handled as UTF-8 with replacement; `LC_ALL=C` is used only for `crontab` | Static | A job's `LANG` is the user's, not jobsctl's concern |
| `ps` | none | `ps` is not used anywhere in `src/` | Static | None |
| `sysctl` | `DarwinLibproc.boot_uuid` | Only `kern.bootsessionuuid` | see Boot id | None |
| `ctypes` | `gateway_proc` | `CDLL("/usr/lib/libproc.dylib")` only inside `DarwinLibproc` (never constructed off macOS); `CDLL(None)` + `prctl` only on Linux | Static | `libproc.dylib` is in the dyld shared cache on recent macOS: `CDLL` should still load it by path. Mac needed |
| Re-exec | `upgrade.reexec_argv`, `os.execve` | Same PID, same arguments | Linux | `sys.orig_argv` and `execve` behave the same; live test `test_run_loop_re_execs_in_place_and_stops_on_sigterm` |
| Signals in `run` | `commands/run.py` | `pthread_sigmask` in the main thread | Static | None expected |
| Browse TUI | `tui/`, `tests/test_browse_pty.py` | Nothing under `tui/` reads `/proc`. The pty tests use `pty.openpty` and `termios`, which exist on macOS | Linux; macOS: Mac needed | The `linux_only` mark was replaced by a capability check; a macOS run is the first evidence. Terminal.app and iTerm2 are manual below |
| Hooks | `hook_run` | Uses `wait_exited_keep`, now through the emulation | Mac needed | `tests/test_hook_run.py`, `test_hook_interrupt.py` stay Linux-only; the hook leader's group reservation on macOS is unproven |
| Test harness | `tests/kill_guard.py`, `tests/spike_helpers.py` | `kill_guard` finds a parent through the platform host (libproc on macOS); `spike_helpers` reads start time and liveness through the host (`start_time`, `alive`; `Procs` is used by `tests/test_browse_pty.py` on macOS too) and keeps `stat_fields` (raw `/proc`) Linux-only | Linux (fake host for the guard; the helpers on the real Linux host); libproc side: Mac needed | A failed read, or a host that cannot be built, means "not a descendant": the guard refuses, never allows. The host is built on first use, so a libproc load failure cannot break the import of every test module. `Procs.adopt` and `stat_fields` are used only by the Linux spike tests |

### Linux-only tests: marked, and what is still unclassified

Tests that need a facility macOS lacks carry a skip on the **capability**, from `tests/capabilities.py`:
`needs_proc` (`/proc/<pid>/...` readable), `needs_waitid` (`os.waitid`) and `needs_prctl` (child subreaper,
dumpable flag; the platform check jobsctl itself uses). A skip names its reason (`needs /proc (Linux)`). The
marks are written from reading the tests; **none has been run on a Mac**, so a Mac run may find more tests to
mark and may find marked ones that would pass.

The list is not kept by hand: `tests/test_platform_marks.py` greps every test file for `/proc`, `waitid`, `prctl`,
`subreaper`, `pidfd`, `fdinfo` and `LinuxHost`. It fails if a file that matches is missing from its `CLASSIFIED` table or a file classified as
marked contains no capability skip. The check is per file: a file naming a Linux facility must carry a capability
skip or be classified; marking each test inside it is by review. The table gives the class and reason for each file. In short:

- **marked** (capability skip): `test_runner.py` (the fake shim's descriptor listing and a spy on `os.waitid`),
  `test_runner_processes.py` (the `/dev/null` descriptor test), `test_shim.py` (the `SigIgn` read from `/proc`; the
  zombie-state probe now goes through the host and is unmarked), `test_shim_processes.py`
  (`/proc` reads, subreaper, dumpable), `test_leftovers.py` (a real subreaper and `os.waitid`), `test_shim_exit.py`
  (the whole module: `os.waitid`; the `os.CLD_*` constants are read with `getattr` so the module still collects without them), `test_shim_output.py` (the tests using a `LinuxHost`
  subclass), `test_wait_processes.py` (`/proc/<pid>/fd`), `test_env_file.py` (`prctl`), plus the files that
  were already marked: `test_proc_real.py`, `test_spike_*.py`, `test_hook_run.py`, `test_hook_interrupt.py` (classified by review in the
  table: it names no facility itself), `test_cron_command.py` (`/proc/locks`). Other files that name pidfd or
  `LinuxHost` only through fake hosts are classified `faked`; `tests/hook_real.py` is a Linux helper used by the marked hook tests;
- **ported**: `tests/kill_guard.py` reads the parent PID through the platform host (`/proc` on Linux, libproc through
  `DarwinHost` on macOS) instead of `/proc`; the Linux side is tested with a fake host, the libproc side is
  **Mac needed**. `tests/spike_helpers.py`: `start_time` and `alive` (and so `Procs`, used by `tests/test_browse_pty.py` on macOS) read
  through the host; `stat_fields` and `Procs.adopt` stay Linux-only (raw `/proc/<pid>/stat`, used by the `linux_only` spikes);
- **hooks**: `test_hook_run.py` and `test_hook_interrupt.py` stay Linux-only until the live run shows the `run_hook`
  `waitid` emulation works on a Mac.

The five files named in [#80](https://github.com/agent-workflow-kit/jobsctl/issues/80) (`tests/test_reconcile_processes.py`,
`tests/test_shim_output.py` (the tests that do not use the `LinuxHost` subclass), `tests/test_runner_processes.py` (the
rest), `tests/test_upgrade.py` and `tests/test_claims.py`), and any other test that starts real processes through the
shared helpers (`tests/shim_real.py`, `tests/shim_helpers.py`, `tests/runner_helpers.py`, `tests/store_helpers.py`),
name no Linux-only facility, so they run unmarked and unchecked on macOS. Whether they pass was not checked and stays
**unclassified** until step 10: record their result. The fake shim in `tests/runner_helpers.py` lists its descriptors
only where `/proc/self/fd` exists (macOS has no equivalent that was ported).

### Not covered

By design (spike notes on [#31](https://github.com/agent-workflow-kit/jobsctl/issues/31)): weak or emulated
`flock` on NFS and similar volumes, a real full disk, and the C6 `flock` probes (macOS `flock` differs on NFS, so they
are not carried over).

## Checklist

Do the steps in order. Record each result in the [template](#results-template). Stop and record the output
if a step fails; do not "fix" the machine.

### 0. Setup

- macOS version (`sw_vers`), architecture (`uname -m`), Python 3.12 or newer (`python3 --version`), SQLite 3.35 or
  newer (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`).
- Install [uv](https://docs.astral.sh/uv/), clone the repository, check out the branch under test, then
  `uv sync --all-extras`.
- Use a **local APFS volume** for the checkout and the state directory (the default `~/.local/state` is local).
- Start with a private environment for everything except the cron section:

  ```sh
  : "${HOME_REAL:=$HOME}"; export HOME_REAL   # keep the real one: step 7 needs it (or use a fresh shell for step 7)
  export SCRATCH="$(mktemp -d /private/tmp/jobsctl-mac.XXXXXX)"
  export HOME="$SCRATCH/home" XDG_CONFIG_HOME="$SCRATCH/home/.config" XDG_STATE_HOME="$SCRATCH/home/.local/state"
  mkdir -p "$HOME" "$XDG_CONFIG_HOME/jobsctl" "$XDG_STATE_HOME"
  printf 'auto_run: false\n' > "$XDG_CONFIG_HOME/jobsctl/config.yaml"
  ```

  `HOME`, `XDG_CONFIG_HOME` and `XDG_STATE_HOME` now point into the scratch directory; any `XDG_*` variable you had
  set before is lost in this shell. Do step 7 in a **fresh shell** (or restore `HOME` from `$HOME_REAL` and unset or
  restore the `XDG_*` values) so `cron install` does not write scratch paths into the real crontab.

  (A private `HOME` hides your `uv` cache; run `uv sync` first and call `.venv/bin/jobsctl` and `.venv/bin/python`
  afterwards, or set `UV_CACHE_DIR`.)

### 1. Automated live tests

```sh
make live-macos
# or
JOBSCTL_LIVE_MACOS=1 uv run pytest -m live tests/test_macos_live.py -v -rA
```

Expect `passed` for every test and no skips (a skip names its reason: `macOS only`, or `set JOBSCTL_LIVE_MACOS=1`).
The tests use a private `HOME` and state directory, never the crontab, signal only PIDs they started or
identified, and bound every wait (60 s). What each covers:

| Test | Checklist item |
|---|---|
| `test_proc_bsdinfo_layout_and_the_identity_of_this_process` | `proc_bsdinfo` is 136 bytes; ppid, pgid, start time stable (spike A3 equivalent) |
| `test_boot_id_is_the_boot_session_uuid_and_stable` | `kern.bootsessionuuid` |
| `test_a_zombie_child_is_visible_and_its_status_is_read_without_reaping` | zombie via `proc_pidinfo`, `pbi_xstatus` = wait status (exit 7, SIGKILL), reap by exact PID, then `ECHILD` |
| `test_a_running_child_is_not_exited_and_a_blocking_wait_returns_after_it_exits` | held-leader wait |
| `test_process_groups_children_and_the_signal_fallback_without_pidfds` | no pidfd (spike C1 equivalent), group listing, `scan_children`, `killpg` after the identity check |
| `test_a_reused_or_gone_identity_is_never_signaled` | identity guard |
| `test_exit_codes_and_a_death_by_signal_are_recorded` | exit record through a real shim |
| `test_the_pass_exits_while_the_shim_keeps_the_job_running` | shim survives the pass |
| `test_a_leftover_in_the_job_group_is_counted_and_never_signaled` | leftovers by group (spike C2 equivalent) |
| `test_cancel_signals_the_group_term_then_kill` | signal sequence: TERM ends it; a TERM-ignoring job ends by KILL |
| `test_concurrent_adds_are_all_recorded_with_distinct_ranks` | `flock` and SQLite locking on this volume |
| `test_run_loop_re_execs_in_place_and_stops_on_sigterm` | `run --loop` re-exec |

The `uv run pytest` suite itself (`-m 'not live'`) is also worth running once; see [Linux-only tests](#linux-only-tests-marked-and-what-is-still-unclassified).

### 2. Manual: identity, liveness, boot id

With the private environment from step 0:

```sh
.venv/bin/python - <<'EOF'
import os
from jobsctl.proc_host import default_host
from jobsctl.proc_identity import identify, check_identity
h = default_host()
print(h.system, h.boot_id())
ident = identify(h, os.getpid())
print(ident, check_identity(h, ident))
EOF
sysctl -n kern.bootsessionuuid
```

Expected: `darwin`, the same UUID twice, an identity `boot:pid:start` and an alive result. Then, as another user
if you can, start a process and read its own PID (`$!` after `sudo -u <other> sleep 60 &` is `sudo`'s PID, not the
`sleep`'s): `sudo -u <other> sh -c 'echo $$ > /private/tmp/other.pid; exec sleep 60' &`, then `cat /private/tmp/other.pid`.
Repeat `check_identity` for that PID (the identity from `identify`, which may itself fail: record the error). `Liveness`
is `alive`, `dead` or `unknown` (the reason string says why, for example unreadable): **expected `alive` or `unknown`,
never `dead`**. Record what you see; a `dead` result is a bug (liveness risk above).

### 3. Manual: process groups and leftovers

```sh
.venv/bin/jobsctl add -- sh -c 'sleep 300 & echo $! > "$HOME/left.pid"'
.venv/bin/jobsctl run
sleep 2
.venv/bin/jobsctl show --json "$(.venv/bin/jobsctl --json list | python3 -c 'import sys,json;print(json.load(sys.stdin)["jobs"][0]["id"])')" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["leftovers"])'
ps -o pid,ppid,pgid,stat,command -p "$(cat "$HOME/left.pid")"
```

Expected: `done 1`, and `ps` shows the `sleep` still running with the job's process group. Then stop it by its
recorded PID only: `kill "$(cat "$HOME/left.pid")"`. (`HOME` is inherited by jobs and private here; `$SCRATCH` is not in a job's
environment, so job scripts use `$HOME`; the shell you type in still sees both.)

A daemon that leaves the group (`sh -c 'perl -e "use POSIX; POSIX::setsid(); sleep 300" & echo $! > "$HOME/d.pid"'`)
is **expected not to be counted** (no subreaper; it goes to `launchd`). Confirm `leftovers` is 0 and record it;
kill it by `"$HOME/d.pid"`.

### 4. Manual: signal sequence

`cancel --force` needs a human actor: pass `--actor human:<you>` outside a terminal.

```sh
.venv/bin/jobsctl add --kill-grace 3s -- sh -c 'trap "" TERM; echo $$ > "$HOME/l.pid"; exec sleep 300'
.venv/bin/jobsctl run
.venv/bin/jobsctl cancel --force --actor human:me --wait-timeout 60s <id>
.venv/bin/jobsctl --json show <id>
```

Expected: status `canceled`, `exit.kind` `signaled` with signal 9 after about 3 s (TERM ignored, then KILL); a job without
the `trap` ends with signal 15. `kill -0 "$(cat "$HOME/l.pid")"` fails afterwards.

### 5. Manual: shim survives the parent, `kill` of the shim

```sh
.venv/bin/jobsctl add -- sh -c 'sleep 20; touch "$HOME/done"'
.venv/bin/jobsctl run          # returns at once
```

Close the terminal tab that ran `run` (or log out of the shell). After 25 s `"$HOME/done"` (in a new tab, with the same
private `HOME`) must exist and the job be `done`.

Then start another job that records its leader PID, and find its shim as the leader's parent:

```sh
.venv/bin/jobsctl add -- sh -c 'echo $$ > "$HOME/leader.pid"; exec sleep 300'
.venv/bin/jobsctl run
sleep 2
LEADER="$(cat "$HOME/leader.pid")"
SHIM="$(ps -o ppid= -p "$LEADER" | tr -d ' ')"
ps -o pid,ppid,command -p "$LEADER,$SHIM"     # confirm the shim is a `jobsctl _shim` process
kill -9 "$SHIM"                               # that exact, confirmed PID only
.venv/bin/jobsctl reconcile
```

The job must follow the documented `shim_lost` handling in [interface.md](interface.md#statuses-and-reasons), not stay
`running` (record the status and reason you see). Afterwards stop the job's `sleep` by `"$LEADER"` if it is still alive.

### 6. Manual: state directory and `flock` on APFS

- Confirm the state directory is local: `df "$XDG_STATE_HOME"` shows an `apfs` device (`mount | grep "$(df "$XDG_STATE_HOME" | tail -1 | cut -d' ' -f1)"`).
- The `test_concurrent_adds` live test is the check. Optionally run `jobsctl run --loop --interval 1s` in two terminals
  at once: only one holds the `runner` lease (`already_running: true` in the other's JSON).
- Do **not** test on a network volume; it is out of scope.

### 7. Manual: cron (real crontab)

Do this in a **fresh shell**, not the one from step 0 (its `HOME` and `XDG_*` are scratch paths that `cron install` would write into the
real crontab). Use a throwaway user if you can (`sudo sysadminctl -addUser jobsctltest ...` and a login shell as that user; the exact
`sysadminctl` options are **needs a Mac**: check `man sysadminctl`). Otherwise back up first and restore after. In the fresh shell,
`HOME_REAL` is simply `$HOME`:

```sh
export HOME_REAL="$HOME"
crontab -l > "$HOME_REAL/crontab.backup" 2>"$HOME_REAL/crontab.err"; echo "$?" > "$HOME_REAL/crontab.rc"
cat "$HOME_REAL/crontab.rc" "$HOME_REAL/crontab.err"
```

The recorded exit status in `crontab.rc` decides how to restore: `0` means a crontab existed and `crontab.backup` is
its content; `1` with a "no crontab" message means there was none and the backup is empty. Any other status: stop and record it.
Record the exact "no crontab" message (expected: `crontab: no crontab for <user>`, exit 1).

1. `jobsctl cron install` -> `crontab -l` shows one `# jobsctl managed` block with an absolute path, a `PATH` without any
   `/var/folders` entry, and `XDG_*` values. Run it twice: the second is `changed: false`.
2. `crontab -` (stdin install) must have worked (it is how `install` writes). If macOS answers with an
   `Operation not permitted`, grant **Full Disk Access** to the terminal app (System Settings -> Privacy & Security),
   and to `/usr/sbin/cron` for the entry to run (`sudo launchctl list | grep cron`), then repeat. Record that this was needed.
   All of this (the need for Full Disk Access, for it on `/usr/sbin/cron`, and the `launchctl list` output) is **needs a Mac**: not
   verified, written from memory of macOS and may be out of date.
3. Run `tests/test_cron_live.py` only if cron is running on this Mac (`JOBSCTL_LIVE_CRON=1 uv run pytest -m live tests/test_cron_live.py`; it
   saves and restores the crontab byte for byte, takes up to 3 minutes and refuses to run if a block exists). It is Linux-oriented:
   the macOS cron daemon may be disabled; a skip or timeout here is a result to record, not a failure of the entry.
4. `jobsctl cron uninstall` -> the block is gone and the rest is byte-identical to the backup
   (`crontab -l | cmp - "$HOME_REAL/crontab.backup"`) when `crontab.rc` is `0`. If there was no crontab before (`1`), `crontab -l` fails with the same
   "no crontab for" message.
5. Restore if anything went wrong, by the recorded status: if `cat "$HOME_REAL/crontab.rc"` is `0`, `crontab "$HOME_REAL/crontab.backup"`
   (only if that file exists and is the backup you took); if it is `1` (there was no crontab), `crontab -r`. Either one **overwrites
   every change made to the crontab since the backup**.

### 8. Manual: `run --loop` re-exec

The live test covers the re-exec. By hand: start `jobsctl run --loop --interval 5s` in a terminal, note its PID, upgrade or
reinstall jobsctl with a different version (`uv pip install` of another build), and watch it re-exec **under the same PID** and keep
passing; `SIGTERM` stops it with exit 0.

### 9. Manual: browse TUI

In **Terminal.app** and in **iTerm2** (each separately), with a few jobs in the private state directory:

```sh
.venv/bin/jobsctl browse
```

Check: the screen is drawn and refreshes; arrow keys, `q` quits with the terminal restored (no stray alternate screen); resizing redraws;
`Ctrl-C`, `kill -TERM <pid>` and `kill -HUP <pid>` from another tab exit with 130 and restore the terminal; mouse selection works;
`--result-file out.json` writes `{"intent": ..., "selected": [...]}` on `o`. Then run the pty tests:
`uv run pytest tests/test_browse_pty.py -v` (these are no longer marked Linux-only).

### 10. What to record when something fails

Everything below, pasted into the issue. No hostnames, usernames or tokens.

- the step, the exact command and the full output
- `sw_vers`, `uname -m`, `python3 -VV`, `uv --version`, the jobsctl commit (`git rev-parse HEAD`)
- for a libproc failure: the output of `python3 - <<'EOF'` with `ctypes.sizeof(jobsctl.gateway_proc._ProcBsdInfo)` and the `errno` after
  `DarwinLibproc().bsdinfo(pid)` for the PID
- for a cron failure: `crontab -l` before and after, the `crontab.err` file, whether Full Disk Access was granted
- `ps -o pid,ppid,pgid,stat,command -p <pids>` for every process involved
- the contents of `$XDG_STATE_HOME/jobsctl/diag/` (private; remove values you do not want to share)

## Results template

```text
Checklist run: <date>   Commit: <sha>
Mac: <model/arch>   macOS: <version>   Python: <x.y.z>   SQLite: <x.y.z>   Volume: APFS / other

1  make live-macos                      pass / fail / skipped: <n passed, failures: names>
2  identity, liveness, boot id           pass / fail   other-user pid read as: alive / unknown / dead
3  process groups and leftovers          pass / fail   setsid daemon counted: yes / no
4  signal sequence                       pass / fail   TERM-ignored job ended by signal: <n> after <s>
5  shim survives the parent / reconcile  pass / fail
6  flock, state dir on APFS              pass / fail
7  cron install/uninstall                pass / fail   Full Disk Access needed: yes / no   crontab -: ok / refused
                                          "no crontab" message: <exact text>   restored byte-identical: yes / no
8  run --loop re-exec                    pass / fail
9  browse TUI  Terminal.app / iTerm2     pass / fail / pass / fail   pty tests: <n passed>
10 full `uv run pytest`                  <n passed, n skipped (reasons), n failed: names; the five unclassified files>

Surprises / decisions needed:
```

## Linux 6.9+ real-kernel check (pending)

Owner: the maintainer, on a Linux 6.9 or later host (not this one: it runs 5.15, where the flag is refused
with `EINVAL` and only the `killpg` fallback runs). Issue #35 (`PIDFD_SIGNAL_PROCESS_GROUP`) is not accepted
until this was run.

```bash
make live-pidfd
# or
JOBSCTL_LIVE_PIDFD=1 uv run pytest -m live tests/test_pidfd_group_live.py -v -rA
```

It starts its own process groups only and signals them by recorded identity (`tests/kill_guard.py` guards
every signal). It skips with a reason on an older kernel. It must show four passes and no skip, and check:

| Check | Test |
|---|---|
| The flag reaches the leader and every member | `test_the_flag_reaches_every_member_and_the_leader` |
| It still reaches the members of a leader that exited but is not reaped (what `_kill` relies on) | `test_the_flag_reaches_members_of_a_leader_that_exited_but_is_not_reaped` |
| A reaped leader gives `ESRCH`, the group is not signaled | `test_a_reaped_leader_gets_nothing_and_the_member_is_left_alone` |
| A wrong identity sends nothing | `test_the_flag_is_never_sent_for_a_wrong_identity` |

If the second one fails (the kernel does not deliver to an unreaped zombie leader's group), nothing unsafe
happens (`_kill` then ends as before and records the members as leftovers); record the result on #35 and decide
whether to drop the `_kill` stepping after the leader exits.
