# jobsctl: implementation plan

Status: planned, 2026-10-31. Implements [interface.md](interface.md) (normative) using the internal design in [design.md](design.md). Work is tracked as GitHub issues in this repository; each issue names its stage below. When an issue is picked up, its implementer expands it into bite-sized test-first steps (write the failing test, see it fail, implement, see it pass, commit) before writing code.

## Conventions for every stage

- Test-first. Every behavior in a task starts as a failing test.
- Every process spawn, signal, `prctl`, `flock`, `/proc` read and clock read goes through `src/jobsctl/gateway.py` (and `procs.py` on top of it); unit tests replace them with fakes.
- Integration tests run real processes against a temporary `XDG_STATE_HOME` and `XDG_CONFIG_HOME` (never the user's), with small shell-script jobs; they run in CI and are not `live`. Only real cron and macOS checks are `live`.
- Every `--json` output gets a golden-file test validated against `schemas/` (a `referencing` registry keyed by `$id`); every hook payload, log and audit record written in a test is validated too.
- Every spike finding that design.md "Live verification" lists as relied on (spikes A, B and C) becomes a regression test in the stage that relies on it.
- Database access follows design.md "Data model": `autocommit=True` connections, explicit `BEGIN IMMEDIATE` for writes, no log append inside a write transaction.
- `make verify` passes before every commit. PR titles are Conventional Commit headers; no `!` before 1.0.
- Runtime dependencies: `pyyaml`, `pydantic`; `textual` only in the `tui` extra. Test-only: `jsonschema`, `referencing`.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli.py` | 0 | argparse surface, global flags, dispatch, human and `--json` output, error envelopes, exit codes, check order (usage → not found/ambiguous → refusals → human gate), `--yes` no-op |
| `config.py` | 0 | YAML + `config.d` merge, Pydantic models, defaults equal to `config.example.yaml`, unknown-queue-selector warnings, absolute `--config` propagation |
| `validate.py`, `durations.py`, `timestamps.py` | 0 | grammars (queues, labels, unique keys, env names, actors, decision ids, hook names), durations, UTC millisecond timestamps |
| `ids.py` | 0 | `jb-` id generation against jobs and `pruned_ids`, prefix resolution (2/10/21) |
| `gateway.py` | 0 | the only boundary: `Popen`, `os.kill`/`killpg`, `waitid`, `prctl`, `flock`, `/proc`, clocks |
| `procs.py` | 0 | process identity (Linux `/proc` field 22, boot id; macOS `libproc`, `kern.bootsessionuuid`), liveness, pidfd with the allow-listed ctypes `syscall()` fallback and the re-check before `kill` without pidfds, dumpability, group probing |
| `store.py` | 0 | connection setup, additive migrations at open for CLI commands, `user_version` checks for long-lived and internal processes, compare-and-set helpers, outbox writes |
| `audit.py` | 0 | `log.jsonl`/`audit.jsonl` with `flock` + `state.db` `seq` counters, once-per-change-of-state warnings |
| `order.py` | 1 | position keys, `rank`, `move`/`pin`/`priority`, selection across queues, `blocked_by`, capacity |
| `env.py` | 1 | `env_file` loading and checks (ancestors, `O_NOFOLLOW`/`O_NONBLOCK`, format), canonical environment, job environment, masking set |
| `commands/` | 1–4 | one module per command group |
| `runner.py` | 2 | the pass: lease, reconcile, claim loop with the `pass_requested` counter, shim spawn and identity record, delivery hand-off, retention, checkpoint; `run --loop` |
| `shim.py` | 2–4 | start sequence, output pump, tail flush, final commit and result file, timeout, signal sequence |
| `output.py` | 2 | ring buffer, masking (window rules), sanitizing, compression |
| `kill.py` | 3 | `_kill` for force-cancel after a shim loss |
| `reconcile.py` | 3 | reconcile rules and findings |
| `hooks.py`, `deliver.py` | 4 | hook processes (WNOWAIT reservation, timeout, interrupted CLI commands), selectors, outbox delivery, `_deliver` and its async hooks |
| `cron.py` | 5 | `cron install`/`uninstall` block |
| `tui/` | 6 | `browse` (Textual, `tui` extra) |

## Stage 0: Foundations

**Goal**: CLI skeleton, config, grammars, ids, state store, logging and the process-identity layer, with no queue behavior yet.
**Success Criteria**: `jobsctl --version` works; every command parses its full flag set (surface frozen per interface.md) and exits 2 on usage errors with the error envelope; config loads with drop-ins and fails fast with `config_invalid` naming the key; `state.db` opens with WAL and migrates additively; log and audit records carry gap-free `seq`; process identities are read correctly on Linux.
**Tests**:

- argparse: every command and flag of interface.md "Commands" and "Global flags"; flag-combination usage errors (`--wait-timeout` without `--wait`/`--force`, `cancel --all` without a selector, id plus selectors, `--dry-run` with `--wait`, `--actor auto:jobsctl`).
- config: defaults equal `config.example.yaml`; drop-in merge; hook entries (duration timeouts, duplicate `name` → `config_invalid`); no `JOBSCTL_<KEY>` variables read.
- grammars, durations (`none` where allowed), timestamps; ids and prefixes (short → 2, ambiguous → 21, unknown or pruned → 10).
- store: connection setup per spike B1 pitfalls; `BEGIN IMMEDIATE`; a migration adds a column with a default; long-lived processes see `version_skew`.
- audit/log: 10 concurrent writers × 300 lines give strictly increasing, gap-free `seq` in file order (spike C6); no append inside a write transaction.
- procs: field 22 parsed after the last `)` (spike A3); zombie = not alive; boot id; pidfd via `os.pidfd_open` and via the ctypes fallback (spike C1), the fallback only on allow-listed architectures, the identity check after `pidfd_open`, and the re-check before `kill` on `ENOSYS`/`EPERM` (design.md implementation notes); `PR_SET_DUMPABLE` makes `/proc/<pid>/environ` unreadable to a sibling (spike C4).

**Status**: Not Started

## Stage 1: Queue store and ordering (0.1)

**Goal**: Jobs can be added, ordered, held, canceled while not running, labeled and inspected; capacity is reported; nothing runs yet.
**Success Criteria**: `add`, `list`, `show`, `move`, `pin`, `unpin`, `priority`, `hold`, `release` (manual holds), `cancel` (queued and held, selectors), `label`, `capacity`, `queues`, `prune` and `export` behave as interface.md specifies, with golden `--json` outputs.
**Tests**:

- `add`: implicit queues and `queue_created`, `strict_queues`, unique keys across queues (racing adds create one job), `existing_job_held` and `pass_env_undefined` warnings, secret refusals (`secret_in_argv`, `secret_in_env`, `--env` names defined in `env_file`), size limits, `--dry-run` projection.
- `env_file`: ancestor checks, symlink and FIFO rejection (spike C5), format rules (CRLF, invalid lines, duplicates, ignored names), unavailable vs. not configured.
- ordering: pin > priority > position; `move --before/--after` adopts the neighbor's priority and pin; self-move no-op; held jobs keep their place; `rank`.
- selection (pure function): queue priority, oldest head, caps, `global_cap`, `not_before`, skipped `--pass-env` jobs, tie-breakers; capacity (`free`, global ready count, `effective_free ≤ free`, `cap: null`, `blocked_by` incl. `global_paused`).
- hold/release/cancel: every Status transitions row that does not involve a process; idempotent no-ops; `release` of queued; selector rules (`refused` list, exit 20 output, 21 candidates).
- `prune`/retention by `ended_at`, `pruned_ids`, `queues_seen` cleanup; `export --since/--after-seq`.

**Status**: Not Started

## Stage 2: Running jobs (0.2)

**Goal**: Jobs run under detached shims up to the caps, with captured, masked output and recorded outcomes; the v1 bar "cap enforced and reorder works" is met.
**Success Criteria**: a pass claims and starts jobs up to the caps under concurrent passes; start triggers start jobs within about a second; jobs get the canonical and job environments; outputs, exits, leftovers and timeouts are recorded; `wait`, `add --wait`, `resolve`, `retry` (incl. from an `exit_13` hold) and automatic retry work.
**Tests**:

- caps never exceeded under 20 concurrent passes (spike B1); trigger coalescing (`pass_requested` checked atomically at lease release; busy re-spawn); `run --loop` reaps only the children it spawned, by PID; `already_running` reports the holder.
- shim detach survives the pass's exit and a hangup (spike A1); stdin `/dev/null` (spike A7); cwd = state dir and `<python> -I -m jobsctl` argv for internal processes; identity recorded by the pass and polled by the shim; `claim_id` compare-and-set; atomic spawn vs. a racing `cancel` of `starting`.
- environment: canonical environment (inherited names, `env.set.PATH` and `env.path_prepend`, `PATH` cleaning, `JOBSCTL_*` removed, no `env_file` values), ignored `env_file` names in hook environments, job environment order, `missing_env`, `cwd_missing`, `spawn_failed` with and without a resolved path.
- output: 2 GiB at bounded memory (spike B4); masking property tests (every cut offset, newline-free streams, live-flush withholding; spike B5); sanitizer (spike B7); compression.
- exit mapping (spike A6), leftovers counted not signaled, zombie leader held with `WNOWAIT` and `Popen.returncode` set after reaping (spike C2).
- timeouts and `overrides` (`none`); automatic retry (`not_before`, budget from the attempt's `effective` snapshot, the 3rd spawn failure final); `retry --attempt N` replay after a cancel or hold; `wait` exits 0/12/13/20/22/130.
- result file written before the final commit; final-commit retry under contention.

**Status**: Not Started

## Stage 3: Cancellation and recovery (0.3)

**Goal**: Running jobs can be stopped safely by a human, and every crash, reboot and shim loss is recovered without killing anything automatically.
**Success Criteria**: `cancel --force` and timeouts run the signal sequence; a SIGTERM without a force request ends `terminated`; `shim_lost` jobs keep their slot and can be force-canceled through `_kill`; `reconcile` applies every rule of interface.md "Reconcile" exactly once.
**Tests**:

- signal sequence against a shell job with children (spike A4): exact PID by pidfd, then group SIGTERM, then SIGKILL, per job `kill_grace`; a process that left the group is never signaled; force check at tail flush (lost SIGTERM).
- `cancel --force`: human gate, audit, `--wait-timeout 0`, expiry and Ctrl-C → 12 `cancel_pending`, repeated request re-sends SIGTERM or re-spawns `_kill`, `--unique-key --force`, `--dry-run` projection.
- shim loss (spike A5): job alive → `shim_lost`; job gone → `failed` (`lost`) or `canceled` (`forced`); duplicate `_kill` exits after its re-check.
- reconcile: `boot_changed` (confirmed vs. unconfirmed), `shim_dead`, `claim_orphaned`, `result_file_finalized` (re-reads the force request), `force_resumed`; leases (holder identity, expiry fallback, `runner_stuck`).
- SIGTERM while `starting` → `terminated`; SIGTERM while waiting for the lease → exit without writing.

**Status**: Not Started

## Stage 4: Hooks and escalation (0.4)

**Goal**: All six hooks fire with at-least-once, in-order delivery; `pre-start` can veto or escalate; humans can override an escalation; jobsctl is feature-complete for its workflow consumers.
**Success Criteria**: `pre-start` outcomes map to `canceled` (`vetoed`) and `held` (`escalated`); `release --override-escalation` works with the human gate and audit; post hooks and `include_output`; outbox redelivery by `_deliver` under the `deliver` lease; async hooks run by `_deliver` with at most 8 running, one per job, delivered at least once.
**Tests**:

- hook processes: `/bin/sh -c` in their own group, `WNOWAIT` reservation, timeout SIGTERM → 5 s → SIGKILL, a hook that ignores SIGTERM (spike C3), stdout/stderr caps, decision-id parsing and validation, `hook_messages`, `hook_ran` log events; a long-lived process with a live reserved leader and an exited sibling child reaps only the sibling; a CLI command interrupted during a sync hook stops its group, leaves the event undelivered and exits 130 (`interrupted`) or 1.
- payloads validate against `schemas/hooks/`; selectors (`queues`, `exclude_queues`, `labels` at transition time); `JOBSCTL_HOOK`/`JOBSCTL_ID`/`JOBSCTL_OBJECT_TYPE`; `hooks_strip_env`.
- delivery: transition and event in one transaction; earlier events delivered before `pre-start`; cancel of `starting` delivers `post-cancel`; redelivery after the firing process dies (same `event_id`); async hooks in outbox order per job, one per job and at most 8 at once, not blocking the lease holder, marked delivered only after the hook ended (redelivered when `_deliver` dies mid-hook).
- override: `override_hooks` kept across automatic retries until the hooks ran, cleared by `retry` and terminal ends; vetoes still veto.
- `pre-start` `event_id` = `<claim_id>:pre-start:<hook index>`.

**Status**: Not Started

## Stage 5: Cron, upgrades and platform readiness (0.5)

**Goal**: jobsctl installs into cron safely, survives upgrades with running jobs, and is checked on macOS.
**Success Criteria**: `cron install` writes the quoted block and `uninstall` removes it; upgrades in both directions keep running jobs correct; the macOS checklist passes manually; `live` tests exist for real cron and macOS.
**Tests**:

- `cron install`: `PATH` filtering, XDG variables, quoting, `%` escaping, `path_entry_dropped`, replacing an existing block; `live`: a real cron pass starts a queued job and a shim outlives it (spike A2), crontab restored byte-identically.
- upgrades: an old shim against a migrated database finishes normally; a new shim against an older database exits before confirming without counting a spawn failure; `run --loop` re-execs on a newer schema and on a changed installed version; long-lived commands exit 12 `version_skew`.
- macOS checklist (`live`, manual): identity via `libproc`, `kern.bootsessionuuid`, group probing without a subreaper, `cancel --force`, cron.
- diagnostics files created on first write and pruned after 7 days.

**Status**: Not Started

## Stage 6: TUI (0.6)

**Goal**: `jobsctl browse` lets a human watch queues and act on jobs; every action maps to a CLI command.
**Success Criteria**: views and keys of interface.md "TUI"; `--focus` and `--result-file`; exits 2 with the install hint without Textual; runs as a child of another TUI.
**Tests**: Textual pilot tests for each key (reorder via `move`, pin, priority, hold, release with confirmation, resolve, cancel with `--wait-timeout 0`, retry, filter, refresh); `--result-file` intents validated against `schemas/cli/browse-result.json`; live tail refresh; sanitized display.

**Status**: Not Started

## Version roadmap

- **0.1** (stage 1): queue store and ordering; useful for inspection only.
- **0.2** (stage 2): jobs run up to the caps; meets the v1 bar "cap enforced and reorder works".
- **0.3** (stage 3): safe cancellation and recovery.
- **0.4** (stage 4): hooks and escalation; feature-complete for review-watch, review-respond, ticket-work and the harness.
- **0.5** (stage 5): cron, upgrades and macOS readiness.
- **0.6** (stage 6): `browse`.
- **1.0**: stages 0–6 done; the v1 review and ticket slices have run through jobsctl without interface changes; the macOS checklist passed; the interface is declared stable (a deliberate breaking-change commit per architecture §18.3).
- **1.x** (additive): an opt-in masked full output log; a `pre-add` hook (roadmap hook candidate); `PIDFD_SIGNAL_PROCESS_GROUP` on Linux 6.9 and later; further `blocked_by` reasons.
- **2.0**: reserved for a non-additive state or interface change, shipped with the drain step of interface.md "Upgrades".
