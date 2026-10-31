# jobsctl: interface

Status: 0.1.0-design, 2026-10-31. This document is the single normative source for jobsctl's rules. [design.md](design.md) records the decisions, the internal architecture and the live verification behind them, and links here instead of restating rules. Reserved exit codes and shared conventions are defined in the awkit design guide (`../../awkit-docs/design-guide.md`).

## Concepts

### Jobs

A **job** is an argv array (never a shell string) plus a working directory, an environment description, a queue, ordering data, labels and an optional unique key. jobsctl does not interpret what a job does. A job runs at most one **attempt** at a time; an attempt is created when a pass claims the job ([Selection](#selection)), and [`retry`](#retry) or an [automatic retry](#automatic-retry) lets the next claim create a new attempt of the same job.

A job's argv never contains untrusted or agent-generated text: submitters pass such text in files that the job command reads. jobsctl cannot verify this; it is the submitter's contract (design guide §4.1).

Jobs run detached under a per-job **shim** ([Shim](#shim)), so a job outlives the command that started it. The runner pass ([Runner](#runner)) only claims jobs and starts shims.

### Identifiers

A job id is `jb-` followed by 8 characters from the lowercase base32 alphabet `a-z2-7` (`^jb-[a-z2-7]{8}$`). Ids are random, checked for collisions and never reused: [`prune`](#prune) keeps the ids it deletes in a compact table that id generation also checks.

Every command that takes a job id accepts any unique prefix of at least 4 characters after `jb-`; the `jb-` itself is optional in input. A shorter prefix exits 2 (`usage`), a prefix matching several jobs exits 21 (`ambiguous`), an unknown id or a pruned job exits 10 (`not_found`). Output always uses the full id.

### Queues

A **queue** is a name matching `^[a-z0-9][a-z0-9_-]{0,63}$`. Queues are implicit: `add --queue <name>` creates the queue on first use with the config key `default_cap` as its cap, and `add --json` reports `queue_created: true` (also logged as a warning, which catches typos). Without `--queue`, a job goes to the queue `default`. Config `queues.<name>` sets a queue's settings before or after its first use ([Config](#config)). With `strict_queues: true`, `add` to a queue that is neither configured nor already known exits 10 (`unknown_queue`).

A queue is **known** once it is configured or has had a job. [`queues`](#queues-1) lists every known queue with its effective settings. A queue removed from config falls back to the defaults (a queue paused with `cap: 0` resumes). A queue stops being known when it is not configured and has no jobs left: retention then deletes its record.

jobsctl has no notion of a repository: it never derives a repo, has no `repos:` config map, and the queue is its only scoping unit for settings (design guide §5 exemption, [design.md](design.md#decisions) D19).

### Labels

`--label key=value` on [`add`](#add) and [`label add`/`label remove`](#label-add--label-remove). A key matches `^[a-z0-9][a-z0-9_.-]{0,63}$`; a value is 0 to 256 characters from `[A-Za-z0-9._:/#@+=,-]`; the pair is split at the first `=`. A job has at most 32 labels; a 33rd exits 2 (`usage`, `details.limit`). Labels are a set of pairs: adding an existing pair is a no-op, a key may carry several values, and output lists the pairs sorted. `--label` filters are repeatable and combine with AND; a filter matches a job that has that pair. jobsctl assigns no meaning to any label key (design guide §9).

### Unique keys

`add --unique-key K` names a piece of work. A key is 1 to 256 characters from `[A-Za-z0-9._:/#@+=,-]` (else exit 2). Keys are unique across all queues among **non-terminal** jobs (`queued`, `starting`, `running`, `held`), enforced by a partial unique index, so two racing `add` calls cannot both create a job. A job waiting for an automatic retry is `queued` and keeps its key.

If a non-terminal job holds K, `add` creates nothing, exits 0 and returns that job with `created: false`; the new call's argv, labels, env, priority and other settings are ignored (a differing argv is logged as `unique_key_reused` with the new argv's `argv_sha256`, never the argv; not an error). If that job is `held`, `add` also returns the warning `existing_job_held`. A terminal job holding K does not block: a new job is created. [`retry`](#retry) of a job whose key another non-terminal job now holds is refused (`duplicate_key`).

## Statuses and reasons

### Statuses

| Status | Meaning | Counts against capacity |
|---|---|---|
| `queued` | waiting to be claimed | as queued ([Capacity](#capacity)) |
| `starting` | claimed by a pass; its shim runs the `pre-start` hooks and prepares the job | yes (as running) |
| `running` | the job's process was started | yes |
| `held` | paused for a human or the submitter; has a [hold reason](#hold-reasons) | no |
| `done` | ended successfully | no (terminal) |
| `failed` | ended unsuccessfully | no (terminal) |
| `canceled` | ended without running to completion by a decision | no (terminal) |

`queued`, `starting`, `running` and `held` are **non-terminal**; `done`, `failed` and `canceled` are **terminal**. `starting` is visible in output.

### End reasons

Every terminal job carries a `reason` from this closed enumeration; it is `null` while the job is `queued`, `starting`, `running` or `held` (a held job's detail is its `hold_reason`):

| Status | Reason | When |
|---|---|---|
| `done` | `exit` | the job exited 0 |
| `done` | `resolved` | [`resolve --as done`](#resolve) |
| `failed` | `exit` | the job exited non-zero, other than 13 |
| `failed` | `signaled` | the job died of a signal jobsctl did not send |
| `failed` | `timeout` | the job's [timeout](#timeouts) expired |
| `failed` | `terminated` | the shim received SIGTERM without a committed force request (e.g. shutdown or logout): while `starting` it killed the running hook's process group, while `running` it ran the [signal sequence](#signal-sequence) |
| `failed` | `lost` | the shim died and the job is gone, or the machine rebooted ([Liveness and recovery](#liveness-and-recovery)) |
| `failed` | `spawn_failed` | `argv[0]` could not be resolved to an absolute path or executed, the shim could not load config, or the shim could not be spawned 3 times in a row |
| `failed` | `missing_env` | a `--pass-env` variable was not available at start ([Job environment](#job-environment)) |
| `failed` | `cwd_missing` | the job's cwd did not exist at start |
| `failed` | `resolved` | [`resolve --as failed`](#resolve) |
| `canceled` | `canceled` | [`cancel`](#cancel) of a queued, starting or held job |
| `canceled` | `forced` | [`cancel --force`](#cancel) of a running job (driven by its shim or, after a shim loss, by `_kill`) |
| `canceled` | `vetoed` | a `pre-start` hook vetoed ([Hooks](#hooks)) |
| `canceled` | `resolved` | [`resolve --as canceled`](#resolve) |
| `queued`, `starting`, `running`, `held` | `null` | |

### Hold reasons

| `hold_reason` | Comes from | [`release`](#hold--release) | [`resolve`](#resolve) |
|---|---|---|---|
| `manual` | [`hold`](#hold--release) of a queued job | requeues it at its previous rank; no flag needed | allowed |
| `escalated` | a `pre-start` hook exited 13 | only with `--override-escalation` (a [human-only flag](#human-only-flags)), else exit 20 (`override_required`): requeues it, and on the next start the hooks that escalated this attempt are treated as allowing; every other hook runs normally and a veto still vetoes | allowed |
| `exit_13` | the job's own command exited 13 | refused (exit 20, `not_releasable`). To continue the work under the same id (same `JOBSCTL_JOB_ID` and `job=` label), run [`retry`](#retry), which requeues the job and keeps the held attempt as recorded; or continue it another way (e.g. `agent-spawn resume`) and resolve the job | allowed; ends the job for good |

A held job keeps its `priority`, `pinned` flag, position and `not_before`, so a released job returns to its previous rank; `release` clears `not_before`, so a job held while waiting for an automatic retry can start at once. A manual hold involves no attempt; an `escalated` or `exit_13` hold ends the current attempt with attempt status `held`. [`retry`](#retry) applies to an `exit_13` hold only; `manual` and `escalated` holds are released.

### Exit record

Each attempt records `exit`, an object or `null`:

| Field | Meaning |
|---|---|
| `kind` | `exited` (normal exit), `signaled` (died of a signal), `spawn_failed` (`argv[0]` could not be resolved or executed), `unknown` (no exit status was observed: `lost` from `running`, or `forced` after a shim loss) |
| `code` | exit status for `exited`, else `null` |
| `signal` | signal number for `signaled`, else `null` |
| `core` | `true` if a core dump was reported, else `false` |

`exit` is `null` when no job process was attempted: `vetoed`, `cwd_missing`, `missing_env`, `lost` and `terminated` from `starting`, `spawn_failed` when the shim never reached `argv[0]` (config or shim spawn failure), and a cancel of a queued job. A shell job that reports a killed child as exit 143 is `exited` with code 143; jobsctl cannot tell it from a real `exit 143` (spike A6).

### Status transitions

Every transition is a compare-and-set on `(id, status, attempt)`; when two commands race, exactly one wins and the other sees the new status. This table is normative; any transition not listed is impossible.

| From | Event | To (reason) | Hook |
|---|---|---|---|
| `queued` | a pass claims it ([Selection](#selection)); a new attempt is created | `starting` | `pre-start` (in the shim) |
| `queued` | `hold` | `held` (`manual`) | `post-held` |
| `queued` | `cancel` | `canceled` (`canceled`) | `post-cancel` |
| `starting` | every `pre-start` hook allows and the job is spawned | `running` | `post-start` |
| `starting` | a `pre-start` hook vetoes | `canceled` (`vetoed`) | `post-cancel` |
| `starting` | a `pre-start` hook escalates, none vetoes | `held` (`escalated`) | `post-held` |
| `starting` | `cancel` | `canceled` (`canceled`) | `post-cancel` |
| `starting` | cwd, environment, config or `argv[0]` check fails, or exec fails | `failed` (`cwd_missing`, `missing_env`, `spawn_failed`) | `post-fail` |
| `starting` | SIGTERM to the shim (a force request cannot exist while `starting`) | `failed` (`terminated`) | `post-fail` |
| `starting` | the shim died after confirming its identity, before the job started; or reboot after it confirmed | `failed` (`lost`) | `post-fail` |
| `starting` | the pass could not spawn the shim; or the shim never confirmed its identity and is gone (including a shim that found an older database schema, [Upgrades](#upgrades), and a reboot); or the claiming pass died before recording the shim | `queued` (attempt deleted) | |
| `starting` | the shim could not be spawned for the 3rd time in a row (never retried automatically) | `failed` (`spawn_failed`) | `post-fail` |
| `running` | exit 0 | `done` (`exit`) | `post-finish` |
| `running` | exit 13 | `held` (`exit_13`) | `post-held` |
| `running` | other exit | `failed` (`exit`) | `post-fail` |
| `running` | died of a signal jobsctl did not send | `failed` (`signaled`) | `post-fail` |
| `running` | timeout expired | `failed` (`timeout`) | `post-fail` |
| `running` | SIGTERM to the shim without a committed force request | `failed` (`terminated`) | `post-fail` |
| `running` | `cancel --force` | `canceled` (`forced`) | `post-cancel` |
| `running` | the shim and the job are gone; or reboot | `failed` (`lost`) | `post-fail` |
| `held` | `release` ([Hold reasons](#hold-reasons)) | `queued` | |
| `held` | `resolve --as done\|failed\|canceled` | `done`, `failed` or `canceled` (`resolved`) | `post-finish`, `post-fail` or `post-cancel` |
| `held` | `cancel` | `canceled` (`canceled`) | `post-cancel` |
| `held` | `retry` of an `exit_13` hold (the held attempt stays `held`) | `queued` | |
| `done`, `failed`, `canceled` | `retry` | `queued` | |

When [automatic retry](#automatic-retry) applies to a failure, the job goes to `queued` with `not_before` instead of `failed`; the attempt is still recorded `failed` with its reason and `post-fail` still fires (its payload's `job` is `queued`).

A job whose shim died while the job process lives stays `running` with `shim_lost: true` ([Liveness and recovery](#liveness-and-recovery)). A `cancel --force` that races a natural exit: the shim's final transaction reads `force_requested_by`; if set, the end is `canceled` (`forced`) whatever the exit.

## Objects

### Job object

`list`, `add`, the mutating commands and hook payloads print this object (hook payloads with `env` reduced to `env_names`); [`show`](#show) adds `attempts` and `output`. Durations are integer seconds. The **current attempt** is the attempt in progress, or the attempt whose end or hold produced the job's status (also after a `resolve` or `cancel` of that hold); a job has none while `queued`, after a manual hold, and after a cancel of a queued or manually held job. Fields marked "current attempt" are `null` without one. Byte limits are exact; the schemas' `maxLength` counts characters and only approximates them.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | [job id](#identifiers) |
| `queue` | string | [queue](#queues) |
| `status` | string | [status](#statuses) |
| `reason` | string or null | [end reason](#end-reasons); `null` unless terminal |
| `hold_reason` | string or null | [hold reason](#hold-reasons); `null` unless `held` |
| `argv` | array of strings | the job's argv |
| `cwd` | string | absolute working directory |
| `env` | object | the `--env` entries (non-secret; at most 256) |
| `pass_env` | array of strings | the `--pass-env` names |
| `labels` | array of strings | `key=value` pairs, sorted |
| `unique_key` | string or null | [unique key](#unique-keys) |
| `priority` | integer | -1000..1000 |
| `pinned` | boolean | in the pinned group |
| `rank` | integer or null | place in its queue while `queued`, else `null` ([Order within a queue](#order-within-a-queue)) |
| `attempt` | integer | the number of the job's last attempt, 0 if it has none (a requeue that deletes an attempt decrements it); the next claim uses `attempt` + 1, which the job sees as `JOBSCTL_ATTEMPT` |
| `retried_from_attempt` | integer or null | the job's `attempt` when [`retry`](#retry) last requeued it (never above `attempt`); `null` if never retried |
| `overrides` | object | the job's own settings (set by `add` or `retry`): `timeout` (seconds, or the string `none` for `--timeout none`), `max_retries`, `kill_grace`; an absent key means the queue's setting applies |
| `effective` | object | the resolved `timeout` (integer, or null for none), `max_retries` and `kill_grace`: for a `starting` or `running` job, the snapshot its shim recorded at start (once recorded); otherwise computed from the current config |
| `not_before` | timestamp or null | a pending [automatic retry](#automatic-retry) starts no earlier; `null` unless `queued` or `held` |
| `blocked_by` | string or null | for a `queued` job, why it cannot start now, the first that applies: `not_before` (in the future), `env_file_unavailable` ([Selection](#selection)), `queue_paused` (queue cap 0), `global_paused` (global cap 0); else `null` (a full queue is not reported) |
| `force_requested_by` | actor or null | current attempt: set from a committed `cancel --force` request; on a terminal job exactly when the reason is `forced` |
| `override_hooks` | array of strings | hook identities whose exit 13 counts as allow on the next start that runs hooks ([`release`](#hold--release)); empty otherwise |
| `shim_lost` | boolean | the shim died while the job process lives |
| `created_at` | timestamp | `add` |
| `enqueued_at` | timestamp | `add`; reset whenever the job returns to `queued` |
| `status_changed_at` | timestamp | the job's last status change (e.g. held since, running since); `created_at` until the first |
| `claimed_at` | timestamp or null | current attempt: when it was claimed |
| `ended_at` | timestamp or null | when the job became terminal |
| `actor` | actor | who added the job |
| `decision_ids` | array of strings | the `--decision-id` values given to `add` |
| `exit` | object or null | current attempt: [exit record](#exit-record) |
| `leftovers` | integer or null | current attempt: [leftovers](#leftovers); `null` when no probe was made |
| `resolved_path` | string or null | current attempt: absolute path `argv[0]` resolved to |
| `resolved_by` | actor or null | who resolved the job; set exactly when `reason` is `resolved` |
| `note` | string or null | the `resolve` note (**untrusted**, at most 4096 bytes) |

### Attempt object

[`show`](#show) lists every attempt oldest first; hook payloads carry the attempt concerned. An attempt's status is never `queued`; it follows the job until the attempt ends (`held` for an escalation or exit 13; a later `resolve` or `cancel` of that hold sets the attempt's status and reason to the job's, while a [`retry`](#retry) of an `exit_13` hold leaves it `held`).

| Field | Type | Meaning |
|---|---|---|
| `attempt` | integer | the attempt number, from 1 |
| `status` | string | `starting`, `running`, `held`, `done`, `failed` or `canceled` |
| `reason` | string or null | [end reason](#end-reasons) |
| `hold_reason` | string or null | `escalated` or `exit_13` while the attempt is `held` |
| `claimed_at` | timestamp | when a pass claimed the job |
| `running_at` | timestamp or null | when the job process started |
| `ended_at` | timestamp or null | when the attempt ended (including a hold) |
| `exit` | object or null | [exit record](#exit-record) |
| `leftovers` | integer or null | [leftovers](#leftovers) |
| `resolved_path` | string or null | the absolute path `argv[0]` resolved to: set whenever resolution succeeded (also for `spawn_failed` when exec failed); `null` for `vetoed`, `cwd_missing`, `missing_env`, and when resolution failed or was not reached |
| `effective` | object or null | the effective `timeout`, `max_retries` and `kill_grace` the shim recorded at start; `null` until recorded |
| `escalated_hooks` | array of strings | identities of the `pre-start` hooks that escalated this attempt |
| `vetoed_by` | string or null | identity of the vetoing hook; set exactly when the reason is `vetoed` |
| `hook_messages` | array of objects | one `{hook_id, outcome, message}` per `pre-start` hook that vetoed (`veto`) or escalated (`escalate`) this attempt (`message`: its stderr, masked and sanitized, at most 4096 bytes; **untrusted**) |
| `decision_ids` | array of strings | decision ids printed by this attempt's hooks |
| `force_requested_by` | actor or null | set from a committed `cancel --force` request; on a terminal attempt exactly when the reason is `forced` |
| `resolved_by` | actor or null | the actor of `resolve` |
| `note` | string or null | the `resolve` note (**untrusted**) |
| `jobsctl_version` | string | version of the jobsctl that claimed the attempt |

## Order

### Order within a queue

The `queued` jobs of one queue are ordered by:

1. **Pinned first.** `pin` puts a job into the pinned group, `unpin` takes it out. Within the pinned group the rules below apply as well.
2. **Priority, highest first.** An integer from -1000 to 1000, default 0; set by `add --priority` or [`priority`](#pin--unpin--priority).
3. **Position.** Each job has a sortable position key, assigned at the end of its group at `add` (FIFO), at the top with `add --top` or `retry --top`. [`move`](#move) rewrites only the moved job's position (and priority, below). `pin`, `unpin` and `priority` keep the position key, so the job lands by age within its new group or band.

Held jobs keep their place in the same position space. `rank` is the computed 1-based place of a queued job in its queue (pinned group first); it is `null` for jobs in any other status.

**`move <id> --before <id2>` / `--after <id2>`** places the job directly before or after `id2`, which must be a queued or held job in the same queue (else exit 20, `other_queue` or `not_queued`). The moved job takes `id2`'s `priority` and `pinned` flag, so it lands exactly where it was put. Moving a job relative to itself exits 0 with `changed: false`. **`--top` / `--bottom`** move the job to the top or bottom of its own priority band within its own group (pinned or not), among queued and held positions. `move` of a held job changes its stored place for when it is released; `move` of a job in any other status exits 20 (`not_queued`).

### Selection

A queued job is **ready** when its `not_before` is `null` or has passed, and, if it has `--pass-env` names, `env_file` is available or not configured at all ([Canonical environment](#canonical-environment); with `env_file: null` the job is claimed and fails `missing_env` at start). A job that is not ready keeps its place; jobs behind it may start. `blocked_by` ([Job object](#job-object)) tells why a queued job cannot start.

A pass claims jobs one at a time in a single `BEGIN IMMEDIATE` transaction, repeating until no job can start:

1. A queue is **eligible** when it has a ready job, its running count (`starting` + `running`) is below its cap (or the cap is `null`), and the global running count is below `global_cap` (or it is `null`).
2. Among eligible queues, the one with the highest `queues.<name>.priority` (default 0) wins; ties go to the queue whose head job (its first ready job in rank order) has the earliest `enqueued_at`, then the lowest job id.
3. That queue's head job becomes `starting`; a new attempt is created, recording the claiming pass's identity and a random `claim_id`.

`enqueued_at` is set at `add` and reset whenever a job returns to `queued` (release, retry, automatic retry). `run --max-starts N` stops after N claims.

## Capacity

`starting` and `running` jobs count as **running**; `queued` jobs (including those waiting for `not_before`) as **queued**; `held` and terminal jobs do not count. For a queue:

- `free = max(0, cap - running - queued)`, `null` for a `null` cap;
- the global entry uses `global_cap`, the total running count, and as **queued** the sum over queues of `min(ready, max(0, cap - running))` (all `ready` for a `null` cap), where `ready` counts the queue's queued jobs whose `not_before` is not in the future (jobs skipped for an unavailable `env_file` still count); `free = max(0, global_cap - running - queued)`, `null` for a `null` `global_cap`;
- `effective_free = min(free, global free)`; a `null` limit contributes nothing, `effective_free` is `null` when neither limit applies, and `effective_free ≤ free` whenever `free` is not `null`.

Lowering a cap below the running count never stops running jobs; it only blocks new starts. Callers deciding whether to enqueue (e.g. "start new work only while there is room") use `effective_free`.

## Runner

### Pass

`jobsctl run` performs one **pass**, with cwd set to jobsctl's state directory:

1. **Lease.** Take the `runner` lease ([Leases](#leases)). If another live process holds it, exit 0 with `already_running: true` and the holder's PID and lease start (`holder`), and do nothing. A live holder that has not renewed the lease for 5 × its expiry is logged as `runner_stuck` (warning, once per holder).
2. **Identity checks** ([Reconcile](#reconcile)): check the identity of every `starting` and `running` job's shim and job process and handle a new boot.
3. **Claim** jobs as in [Selection](#selection), recording the `pass_requested` counter ([Start triggers](#start-triggers)).
4. **Spawn** one shim per claimed job ([Shim](#shim)) and, in a follow-up transaction right after the spawn, record the shim's identity (PID, start, boot id) on the attempt. A shim that cannot be spawned returns its job to `queued` with its position and `enqueued_at` kept and its attempt row deleted (logged `shim_spawn_failed`, listed in `warnings`), as a compare-and-set on `starting` with its `claim_id` (a job canceled meanwhile keeps its attempt). The 3rd consecutive failure for a job ends it `failed` (`spawn_failed`), which is final (never retried automatically), and keeps that attempt row (`exit` null, `resolved_path` null). The count resets when a shim confirms its identity, at `retry` and at `release`; a `claim_orphaned` requeue does not count.
5. **Delivery.** If jobs with no live `job:<id>` lease holder have undelivered hook events, or any job has undelivered `async` events, **request delivery**: increment a delivery counter and check the `deliver` lease in one transaction, and spawn one detached `jobsctl _deliver` ([Internal processes](#internal-processes)) only if no live process holds that lease. `_deliver` delivers synchronous events under the per-job leases ([Delivery](#delivery)), skipping jobs whose `job:<id>` lease has a live holder, runs the `async` events of every job ([Hook processes](#hook-processes)), and re-reads the counter and releases the `deliver` lease in one transaction, starting over if the counter changed.
6. **Retention** ([`prune`](#prune)): delete terminal jobs whose `ended_at` is older than `retention` in batches of 100 jobs per transaction, for at most 10 seconds per pass (a fixed budget, not configurable); also delete the records of queues that are not configured and have no jobs, and, whatever `retention`, `diag/` files older than 7 days.
7. **Checkpoint** the WAL (`wal_checkpoint(TRUNCATE)`), then re-read the `pass_requested` counter and release the lease in one `BEGIN IMMEDIATE` transaction; if the counter changed since step 3, go back to step 3 (claim, spawn, delivery, retention) instead of releasing.

A pass reads config at its start, so a cap change applies at the next pass. A pass that cannot open or lock the database within `busy_timeout` exits 1 (`busy`) and changes nothing; the next pass retries, and a triggered pass that exits `busy` before taking the lease spawns one new pass after a 2-second back-off.

A pass reaps its exited shim children by their PIDs ([Internal processes](#internal-processes)): shims are the pass's children until they exit, although they run in their own sessions. A shim child that exited without confirming its identity makes its attempt `claim_orphaned` (requeued, [Reconcile](#reconcile)).

`run --loop` repeats passes in the foreground every `run_interval` (or `--interval`) until it receives SIGTERM or SIGINT, finishing the current pass first, reaps its own shim and pass children at every iteration ([Internal processes](#internal-processes)) and re-execs itself after an upgrade ([Upgrades](#upgrades)).

### Start triggers

With `auto_run: true` (the default), these request a pass after they commit, so work starts without waiting for cron:

- [`add`](#add) (unless `--no-start`);
- a shim recording a job's end (right after its final commit, before delivering its post hooks);
- [`cancel`](#cancel) of a `starting` job;
- [`release`](#hold--release) and [`retry`](#retry) when they changed something.

A request increments the `pass_requested` counter and checks the `runner` lease in one transaction, and spawns a detached `jobsctl run` ([Internal processes](#internal-processes)) only if no live process holds the lease. The holder's final counter comparison and its lease release are one `BEGIN IMMEDIATE` transaction ([Pass](#pass) step 7), so requests are coalesced and none is lost. A lease holder that does not claim ([`reconcile`](#reconcile-1)) re-checks the counter the same way at release and spawns a pass if it changed. A periodic `jobsctl run` (cron, [`cron install`](#cron-install--cron-uninstall), or `run --loop`) remains the safety net for reboots and lost shims. `auto_run: false` disables every trigger.

### Internal processes

jobsctl starts these internal commands; none is part of the stable interface:

| Command | Started by | Does |
|---|---|---|
| `jobsctl run` | [start triggers](#start-triggers) | one pass |
| `jobsctl _shim` | a pass | supervises one attempt ([Shim](#shim)) |
| `jobsctl _deliver` | a pass, or a process that leaves `async` hook events ([request delivery](#pass), step 5) | delivers undelivered hook events and is the parent of `async` hooks ([Delivery](#delivery), [Hook processes](#hook-processes)) |
| `jobsctl _kill <id>` | `cancel --force`, [Reconcile](#reconcile) | runs the [signal sequence](#signal-sequence) for a `shim_lost` job and finalizes it |

Each is started as `[<python>, "-I", "-m", "jobsctl", <internal command>, …]`, detached in a new session, with cwd jobsctl's state directory, stdin `/dev/null`, all other file descriptors closed, the [canonical environment](#canonical-environment) (which never holds `env_file` values; a process that needs them reads the file itself), and `--config PATH` when the spawning process had one (made absolute first; [Global flags](#global-flags)). Its stdout and stderr go to its own diagnostics file, mode 0600, created on first write only: `shims/<id>.log` for a shim, `diag/<kind>-<pid>.log` for the others (every pass deletes `diag/` files older than 7 days). Diagnostics are rate-limited (at most 1 MiB per file) and never contain environment values or `env_file` lines. On Linux every jobsctl process that loads `env_file` values (every command, pass, shim and internal process) first calls `prctl(PR_SET_DUMPABLE, 0)`, so other same-user processes cannot read its memory or environment; the flag resets on exec, so jobs and hooks are unaffected.

**Reaping.** No jobsctl process calls `waitpid(-1, …)` or `waitid(P_ALL, …)`, or otherwise reaps "any child": each reaps only PIDs it spawned and recorded, by PID. The shim reaps its job leader only after the signal sequence and the leftover probe, and the leftovers adopted as subreaper by their exact PIDs ([Leftovers](#leftovers)); `_deliver` and CLI commands reap their own hook leaders only after their group work ([Hook processes](#hook-processes)); long-lived processes (`run --loop`, a waiting `cancel`) reap the shims, passes and `_kill` processes they spawned at every iteration (a re-exec'd `run --loop` by the shim PIDs recorded on the attempts).

## Shim

The shim is the internal command `jobsctl _shim <job id> <claim id>`, spawned with `[<python>, "-I", "-m", "jobsctl", "_shim", <id>, <claim_id>]` (plus `--config PATH` when its pass had one) as described in [Internal processes](#internal-processes) (new session, stdout and stderr to `<state dir>/shims/<id>.log`: the shim's own diagnostics, never job output). It imports every module it needs at start, so an upgrade of jobsctl while it runs does not affect its code ([Upgrades](#upgrades) covers the database). It is not part of the stable interface.

### Start sequence

1. Ignore SIGHUP, SIGINT and SIGPIPE; install the SIGTERM handler ([Signal sequence](#signal-sequence)); on Linux, become a child subreaper (`PR_SET_CHILD_SUBREAPER`) and call `prctl(PR_SET_DUMPABLE, 0)`.
2. Check the database schema version (older than this shim knows: exit at once without writing, [Upgrades](#upgrades)). **Confirm** its identity: a compare-and-set that the attempt's `claim_id` is the one it was given and that the shim identity the pass recorded is its own. If the `claim_id` matches but no shim identity is recorded yet, poll every 100 ms while the claiming pass (`claimed_by`) is alive; exit if a different identity is recorded, the `claim_id` differs, or the claiming pass died without recording one. Every later write of the shim is a compare-and-set on `claim_id`. Then take the `job:<id>` lease, held for the shim's whole life; while a live process holds it, the shim waits, without a deadline, and writes nothing (the holder releases it when its delivery ends; reconcile sees the waiting shim as alive). Holding the lease, it first delivers every undelivered synchronous event of its job, in order ([Delivery](#delivery)). Then it reads config once and keeps it for its whole life (a config that cannot be loaded: `failed`, `spawn_failed`, logged `config_invalid`) and records the effective `timeout`, `max_retries` and `kill_grace` on the attempt (`effective`).
3. Run the `pre-start` hooks ([Hooks](#hooks)), checking the job's row at least every second while a hook runs: if it became `canceled`, kill the running hook's process group ([Hook processes](#hook-processes)), run no more hooks, deliver its job's undelivered events (including the `post-cancel`) and exit. A veto ends the job `canceled` (`vetoed`, recording `vetoed_by` and `hook_messages`); an escalation without a veto ends it `held` (`escalated`), recording which hooks escalated and their messages. Hooks listed in the job's `override_hooks` are run and their decision ids recorded, but their exit 13 counts as allow; once the hooks have run, `override_hooks` is cleared. Decision ids printed by hooks are recorded on the attempt.
4. Check the cwd exists and is a directory (else `failed`, `cwd_missing`).
5. Build the [job environment](#job-environment) (a `--pass-env` variable not available: `failed`, `missing_env`).
6. Resolve `argv[0]` against the job environment's `PATH` unless it contains `/`; record the resolved absolute path (failure, a result that is not absolute, or a non-executable file: `failed`, `spawn_failed`, exit kind `spawn_failed`, `resolved_path` null).
7. Start the job in one `BEGIN IMMEDIATE` transaction: re-check that the job is `starting` (and the `claim_id`), spawn it (fork and exec only) in a **new process group** whose id is the job's PID, cwd as recorded, stdin `/dev/null`, stdout and stderr on **one** pipe read by the shim, record its identity ([Process identity](#process-identity)), set `running`, commit. An exec that fails (e.g. a missing `#!` interpreter) ends the job `failed` (`spawn_failed`, exit kind `spawn_failed`, `resolved_path` kept). This is the only process spawn inside a transaction; a `cancel` that loses the race sees `running`.
8. Read output ([Output capture](#output-capture)), flush the tail every `tail_flush_interval`, enforce the [timeout](#timeouts). At every tail flush, also read the attempt's `force_requested_by` and start the [signal sequence](#signal-sequence) when it is set.
9. When the job process exits (observed with `waitid(WEXITED|WNOWAIT)`, leaving the leader unreaped): drain the pipe for at most 2 seconds (never until end of file: a leftover process can hold the pipe open forever), count [leftovers](#leftovers), write the result (exit record, final tail, leftovers) to `shims/<id>.<attempt>.result` (mode 0600), then commit the final transition, the final tail and the hook events in one transaction, retrying with backoff for up to 10 minutes. The final transaction reads `force_requested_by`. Then request a pass ([Start triggers](#start-triggers)), reap the leader and delete the result file.
10. Deliver its job's undelivered events, the post hooks included ([Delivery](#delivery)), and exit 0.

**Still starting?** After confirming its identity, after taking the `job:<id>` lease and before each `pre-start` hook, the shim re-reads its job. If the job is no longer `starting` with its `claim_id` (e.g. it was canceled), the shim runs no further hooks, delivers its job's undelivered events if it holds the lease (otherwise the holder or `_deliver` does) and exits.

### Canonical environment

Every jobsctl process that starts other processes (every command, pass, shim and internal process) builds a **canonical environment** at its start:

1. The variables named in `env.inherit` (exact names, or a prefix ending in `*`), plus always `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR` and `TMPDIR` when present, taken from the process's own environment.
2. Every `JOBSCTL_*` variable is removed.
3. If config `env.set.PATH` is set, it replaces the inherited `PATH`; then the `env.path_prepend` directories are put in front of `PATH`, in order.
4. `PATH` is cleaned: empty, relative and non-existent entries and temporary ones (under `/tmp` or `/run/user`, such as per-shell version-manager directories) are dropped (`path_entry_dropped`, logged once per change of state).

The canonical environment never contains `env_file` values: they are added only to hook environments ([Hooks](#hooks)) and, through `--pass-env`, to jobs ([Job environment](#job-environment)). Internal processes are spawned with the canonical environment ([Internal processes](#internal-processes)) and rebuild it the same way. Inherited values (step 1) come from the process that started the chain: a cron pass, or the command that triggered a pass, whose values then travel through every later trigger. Only `env_file`, `env.set` and `env.path_prepend` are the same whichever process started the chain. Setting `env.set.PATH` is recommended: it gives jobs, hooks and jobsctl's own processes a `PATH` that does not depend on the trigger ([Residual risks](#residual-risks)).

**`env_file`** (optional config) is a file of `NAME=value` lines; blank lines and `#` comments are allowed; there is no quoting, no expansion and no `export`. One trailing `\r` is stripped from each line; a line is split at its first `=`; `NAME` must match the `--env` name grammar ([Job environment](#job-environment)); for a duplicate name the last line wins. A line with an invalid name, no `=` or a NUL byte is ignored (warning `env_file_line_invalid`, line number only, never content). Masked values are the stripped values.

`env_file` is for secrets only: every value in it is treated as a secret (masked, and refused by `add` in argv and `--env`). Put non-secret fixed values in `env.set`.

Lines naming `PATH`, `HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `JOBSCTL_*`, `LD_*`, `DYLD_*`, `PYTHON*`, `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS` or `IFS` are not added to hook environments (warning `env_file_line_ignored`, line number only), so `env_file` cannot change the `PATH`, loader or shell startup of a hook (hooks run via `/bin/sh -c`, [Hook processes](#hook-processes)); `--pass-env` can still pass any name the file defines to a job.

`env_file` is a trust anchor: its values reach every hook and, through `--pass-env`, jobs, so whoever can write it runs code as the user ([Residual risks](#residual-risks)). Keep it where no isolated session can write; the recommended location is `$XDG_CONFIG_HOME/jobsctl/env`. It is **available** only if, opened with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, `fstat` shows a regular file of at most 1 MiB owned by the user and not readable or writable by group or others, and every ancestor directory, opened without following symlinks from `/`, is owned by the user or root, not writable by group or others, and not a symlink. Otherwise it is **unavailable**: jobs with `--pass-env` names are not ready ([Selection](#selection); `blocked_by: env_file_unavailable`), `env_file_unsafe` is logged once per change of state, and `run --json` reports it in `warnings`. `env_file: null` means not configured, which is not unavailable. Its values are never logged or stored.

### Job environment

A job's environment is built from scratch at start sequence step 5, never inherited wholesale:

1. **Base:** the canonical environment restricted to the names in `env.inherit` (`env_file` values reach a job only through `--pass-env`). Default `env.inherit`: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TZ`, `TERM`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR`, `TMPDIR`. `TMUX`, `TMUX_PANE` and `SSH_AUTH_SOCK` are not inherited unless listed.
2. `env.set`: fixed, non-secret values from config.
3. `env.path_prepend`: directories put in front of `PATH`, in order.
4. The job's own `--env VAR=value` entries (stored in `state.db`; non-secret only).
5. The job's `--pass-env VAR` entries: the value comes only from `env_file`, read at this moment and never stored. A variable that is undefined, or an `env_file` that is unavailable or not configured at this check, ends the job `failed` (`missing_env`, naming the variable in the log); an empty value is passed as empty.
6. `JOBSCTL_JOB_ID=<id>` and `JOBSCTL_ATTEMPT=<n>`.

`add --pass-env X` where `X` is not defined in the current `env_file` (or there is none) succeeds with the warning `pass_env_undefined` (in `add --json` `warnings` and the log). `add` refuses secrets in stored fields ([`add`](#add)): an `--env` name listed in `mask_env` or defined in `env_file` (use `--pass-env`), and any argv element or `--env` value that contains a masked value. Names in `--env` and `--pass-env` must match `^[A-Za-z_][A-Za-z0-9_]{0,127}$` and must not start with `JOBSCTL_`. A job has at most 256 `--env` and 256 `--pass-env` entries, and an `--env` value is at most 128 KiB.

### Output capture

The shim reads the combined stdout/stderr pipe into a ring buffer and keeps a **tail** per attempt:

- Size: the smaller of `tail_bytes` (default 16384) and `tail_lines` (default 200) after [masking and sanitizing](#masking-and-sanitizing), per queue overridable, at most 262144 bytes.
- The raw window the shim keeps is `tail_bytes` + the longest masked value + 4096 bytes; a secret cut at the window's start by dropped output is discarded by step 1 of [masking](#masking-and-sanitizing).
- While the job runs, the masked and sanitized tail is written to `state.db` every `tail_flush_interval` (default 5 seconds), so [`show`](#show) shows recent output of a running job. A live flush withholds everything after the last newline, and at least the longest masked value minus one byte at the end, before masking; only the final tail after the drain includes the last partial line.
- `bytes_seen` counts every byte read; `truncated` is `true` when earlier output was dropped.
- Full output is not stored.

If the shim dies, a job that keeps writing gets SIGPIPE (or EPIPE) on its next write (spike B4); a job that writes nothing keeps running ([Liveness and recovery](#liveness-and-recovery)).

### Masking and sanitizing

Applied to the kept raw window every time a tail is produced, in this order (the algorithm verified in spike B5):

1. If output was dropped before the window, discard the window up to and including its first newline; if the window has no newline, discard its first (longest masked value + 256) bytes.
2. Decode as UTF-8 with `backslashreplace` (invalid bytes become `\xNN`).
3. Remove terminal escape sequences: CSI, OSC (including titles, clipboard writes and hyperlinks), DCS, SOS/PM/APC, and their 8-bit forms; keep only the last segment of `\r`-rewritten lines.
4. **Mask:** replace every occurrence of the **masked values** with `***`, longest values first; a multi-line value is masked line by line. The masked values are the values of the variables in `mask_env` (in the canonical environment of the masking process) and of every variable defined in `env_file` (as the masking process read it); a shim adds the `--pass-env` values it read at start sequence step 5 (the union of both reads). Values shorter than 8 characters are not masked (masking them would destroy ordinary output) and produce a `short_secret_not_masked` warning in the log at start.
5. Escape the remaining control characters (C0 except tab and newline, DEL, C1), bidirectional overrides and invisible format characters as `\uNNNN`.
6. Cut to the tail size on a line boundary.

Masking is best-effort: it does not catch encoded forms (base64, hex, URL encoding), secrets broken by newlines, slices of a secret, or secrets a job obtains itself ([Residual risks](#residual-risks)). The stored tail is **untrusted**; [`show`](#show) sanitizes it again before printing, and the `--json` field is marked untrusted. Hook stdout and stderr go through the same masking and sanitizing before they are stored or logged ([Hooks](#hooks)).

### Signal sequence

When the shim receives SIGTERM, or finds a committed force request at a tail flush ([Start sequence](#start-sequence) step 8; the SIGTERM from `cancel --force` is only a wake-up), it reads its attempt:

- **`running`:** with a committed `force_requested_by` (from [`cancel --force`](#cancel)) it runs this sequence and ends the job `canceled` (`forced`); without one (e.g. shutdown or logout) it runs the sequence and ends the job `failed` (`terminated`, actor `auto:jobsctl`).
- **`starting`, before its `pre-start` hooks** (start sequence step 2: waiting for its identity to be recorded or for the `job:<id>` lease, or delivering earlier events): it exits without writing. [Reconcile](#reconcile) then ends a confirmed shim's job `failed` (`lost`, `shim_dead`; eligible for automatic retry) and requeues an unconfirmed one's (`claim_orphaned`).
- **`starting`, from its `pre-start` hooks on:** a force request cannot exist (`--force` on a `starting` job is a plain cancel). The shim kills the running hook's process group ([Hook processes](#hook-processes)) and ends the job `failed` (`terminated`, `exit` null), which is eligible for [automatic retry](#automatic-retry).
- **After the job process exited** (start sequence steps 9 and 10): the end does not change; the shim records the signal in its diagnostics file and skips its remaining hook deliveries, which stay in the outbox for `_deliver`.

When the [timeout](#timeouts) expires the shim runs the sequence and ends the job `failed` (`timeout`). The end reason holds whatever the job's exit status. Each step waits the job's effective `kill_grace` (default 30 seconds; the shim's snapshot) for the job process to exit before the next:

1. SIGTERM to the job's exact PID (identity verified; on Linux through `pidfd_open`, an identity check and `pidfd_send_signal`).
2. If the job's PID is still alive, or processes remain in the job's process group: SIGTERM to the job's process group.
3. SIGKILL to the job's process group (and to the job's PID if it left the group).

The shim keeps the job's group leader unreaped until the sequence and the leftover probe are done, so the process group id stays reserved and verifiable on every platform. Group signals are sent only to the process group the shim created for the job, and only while it is verified to be that group. Processes other than the job's own PID that left the group (e.g. a tmux server, which daemonizes into its own session) are never signaled.

**Exact signals.** On Linux every signal that a jobsctl process (a shim, `_kill`, `cancel`, the firing process of a hook) sends to an exact PID (a shim, a job, a hook's leader) goes through `pidfd_open`, then an identity check of the pidfd's process ([Process identity](#process-identity)), then `pidfd_send_signal`. Where pidfds are unavailable (macOS; on Linux, an architecture outside the allow list of the system-call fallback, `ENOSYS`, `EPERM` from a seccomp filter, WSL1), jobsctl re-checks the identity immediately before `kill` or `killpg` and accepts the remaining window ([Residual risks](#residual-risks)).

**Shim loss.** `cancel --force` first applies the [Reconcile](#reconcile) rules to its job. For a job with `shim_lost: true`, it commits the request (audited) and spawns a detached `jobsctl _kill <id>` ([Internal processes](#internal-processes)). `_kill` takes the `job:<id>` lease and proceeds only if the job is still `running` with a committed force request and no live shim; otherwise it delivers the job's pending events and exits. It runs this whole sequence, then finalizes the job `canceled` (`forced`, exit kind `unknown`, `leftovers` as in [Leftovers](#leftovers)) with its `post-cancel` event. `_kill` is not the job's parent: it sends a group signal only after verifying that the group's leader is the recorded job process, on Linux 6.9 or later through `pidfd_send_signal` with `PIDFD_SIGNAL_PROCESS_GROUP` on the leader's pidfd; elsewhere a short window between the check and the signal remains ([Residual risks](#residual-risks)). If the leader is gone, nothing is signaled. `cancel` waits for `_kill` as for a live shim. A `running` job with a committed force request and no live `job:<id>` lease holder (its shim or `_kill` died) gets a new `_kill` from [Reconcile](#reconcile) (`force_resumed`) or from a repeated `cancel --force` ([`cancel`](#cancel)).

### Leftovers

When a job exits on its own and processes remain in its process group (e.g. a `sleep &` it started), they are **not** signaled. The attempt records `leftovers: N` (also after a signal sequence) and a `leftovers` warning is logged. On Linux, processes reparented to the shim as subreaper (found in `/proc` by their parent PID, excluding the job leader and the shim's hook leaders) are also counted and reaped by their exact PIDs ([Internal processes](#internal-processes)); on macOS only the process group is probed. With exit kind `unknown` (no exit status observed), leftovers are counted only where the group can still be probed: by `_kill` after a forced cancel of a `shim_lost` job, and by `job_gone` when the process group id is verifiable; otherwise `leftovers` is `null`.

### Timeouts

A job's timeout is `add --timeout` (stored in `overrides.timeout`; `none` overrides a queue timeout), else `queues.<name>.timeout`, else none (the default). A running job's timeout and `kill_grace` are those its shim recorded at start (`effective`). The clock starts when the job enters `running` (not during `pre-start` hooks, which have their own timeouts). On expiry the shim runs the [signal sequence](#signal-sequence) and the job ends `failed` (`timeout`). An automatic kill on timeout is allowed because the timeout is an explicit, configured bound; cancelling a running job otherwise needs a human ([`cancel`](#cancel)). The timeout of a job with `shim_lost: true` is no longer enforced.

### Automatic retry

Off by default. With an effective `max_retries` above 0 (`queues.<name>.max_retries` or `add --max-retries`), an attempt that fails with reason `exit`, `signaled`, `timeout`, `terminated`, `lost`, `spawn_failed` or `missing_env` is recorded `failed` in `attempts`, and the job returns at once to `queued` with `not_before = now + retry_delay` (default 60 seconds) at the bottom of its priority band, as long as the number of automatic retries since the last `retry` or `release` is below `max_retries`. Otherwise the job ends `failed`. Every process that decides (shim, `_kill`, reconcile) uses the `max_retries` the attempt recorded (`effective`), or the current config when the attempt recorded none. The 3rd consecutive shim-spawn failure ([Pass](#pass) step 4) is never retried automatically.

A job waiting for its retry is `queued`: it keeps its unique key and its `override_hooks` ([`release`](#hold--release)), [Selection](#selection) skips it until `not_before`, `cancel` and `hold` work as usual (a hold keeps `not_before`), and [`wait`](#wait) keeps waiting. `post-fail` fires for every failed attempt; when the job was requeued, the payload's `job` is `queued`. Held, canceled and resolved jobs are never retried automatically.

## Liveness and recovery

### Process identity

A process identity is `(boot_id, pid, start)`:

- **Linux:** `boot_id` from `/proc/sys/kernel/random/boot_id`; `start` is field 22 (start time in clock ticks since boot) of `/proc/<pid>/stat`, parsed after the **last** `)` because the command name can contain spaces and parentheses (spike A3).
- **macOS:** `boot_id` from `sysctl kern.bootsessionuuid`; `start` from `proc_pidinfo(PROC_PIDTBSDINFO)` (seconds and microseconds) through `libproc`; never `ps` output.

A process is alive only if a process with that PID exists in the current boot with the same start value. A zombie counts as not alive, except a job leader the shim keeps unreaped ([Signal sequence](#signal-sequence)). Wall-clock time is never used for identity.

### Leases

The leases are `runner` ([Pass](#pass)), `deliver` (the `_deliver` process, [Pass](#pass) step 5) and one `job:<id>` per job ([Delivery](#delivery)). Each is a row holding the holder's identity and an expiry 60 seconds ahead. Holders renew every 15 seconds and before and after each hook. A lease whose holder is alive (identity check) is never taken over; a lease is free when its holder is not alive or its boot id differs, and expiry frees it only when the identity check is inconclusive. Taking a lease is one compare-and-set; a process waiting for a lease does not hold it.

### Reconcile

[`reconcile`](#reconcile-1) and every pass apply these rules, in order; each rule applies to the jobs that match it at that point, so a job can match several (e.g. `shim_lost`, then `force_resumed`):

| Finding | Condition | Action | Result |
|---|---|---|---|
| `result_file_finalized` | a `starting` or `running` attempt's shim is not alive and its result file exists | `finalized` | the transition the result file records, with its hook; if `force_requested_by` (re-read) is set, `canceled` (`forced`), `post-cancel` |
| `boot_changed` | boot id differs from the one recorded for a `starting` or `running` attempt | `requeued`, `failed_lost` or `canceled_forced` | a `starting` attempt whose shim never confirmed its identity: `queued`, attempt deleted (as `claim_orphaned`); otherwise `failed` (`lost`; `exit` null from `starting`, kind `unknown` from `running`), `post-fail`; with a committed force request `canceled` (`forced`), `post-cancel` |
| `claim_orphaned` | `starting`, the shim never confirmed its identity, and either no shim identity is recorded and the claiming pass is not alive, or the recorded shim is not alive (also seen by a pass reaping its own shim child) | `requeued` | `queued`, attempt deleted (while the claiming pass or the recorded shim is alive, the job is left alone) |
| `shim_dead` | `starting`: the shim confirmed its identity and is not alive | `failed_lost` | `failed` (`lost`, `exit` null), `post-fail` |
| `job_gone` | `running`: shim not alive, job process not alive (also with `shim_lost: true`) | `failed_lost` or `canceled_forced` | `failed` (`lost`, exit kind `unknown`; the tail is the last flushed one; [leftovers](#leftovers) counted when the group id is verifiable), `post-fail`; with a committed force request `canceled` (`forced`), `post-cancel` |
| `shim_lost` | `running`: shim not alive, job process alive, not yet marked | `marked_shim_lost` | stays `running`, `shim_lost: true` (keeps its slot), logged once per attempt; jobsctl never kills it without a committed force request |
| `force_resumed` | `running` with a committed force request and no live `job:<id>` lease holder (its shim or `_kill` died) | `kill_started` | a detached `_kill` runs the [signal sequence](#signal-sequence) and finalizes the job `canceled` (`forced`), `post-cancel` |
| `hooks_redelivered` | undelivered hook events of a job whose `job:<id>` lease holder is not alive | `redelivered` | delivered in order ([Delivery](#delivery)) |
| `pruned` | a terminal job whose `ended_at` is older than `retention` | `deleted` | deleted ([`prune`](#prune)) |

Finding names: `result_file_finalized`, `boot_changed`, `claim_orphaned`, `shim_dead`, `job_gone`, `shim_lost`, `force_resumed`, `hooks_redelivered`, `pruned`.

Action names: `finalized`, `failed_lost`, `canceled_forced`, `requeued`, `marked_shim_lost`, `kill_started`, `redelivered`, `deleted`.

A `failed_lost` failure is subject to [automatic retry](#automatic-retry). Reconcile itself signals nothing and fires no hooks (hooks are run by the processes in [Delivery](#delivery)); job processes are signaled only by a shim or by a `_kill` started for a committed force request.

### Upgrades

The database records its schema version (`user_version`). Migrations are **additive only**: new tables and new columns with defaults, never a drop, a rename or a change of meaning, so code of the previous version keeps working against a migrated database. A change that cannot be made additively ships only in a new major version, whose release notes give a drain step: stop `run --loop` and the cron entry, then wait until `jobsctl list --status starting --status running` is empty.

CLI commands migrate the database when they open it (under `BEGIN IMMEDIATE`). Internal processes (`_shim`, `_deliver`, `_kill`) never migrate: one that finds a schema older than it knows exits before writing. A shim does so before confirming its identity ([Start sequence](#start-sequence) step 2); that exit does not count toward the shim-spawn failure limit ([Pass](#pass) step 4), the next CLI command migrates, and the next pass requeues the job (`claim_orphaned`). Long-lived processes (`run --loop`, a waiting `cancel` or `wait`, `browse`) check the schema version before each iteration or transaction. `run --loop` re-execs itself when the schema is newer than it knows or when the installed jobsctl version differs from its own (read again before each iteration); `wait`, a waiting `cancel` and `browse` exit 12 with the error `version_skew` ("jobsctl was upgraded underneath this process; run it again").

## Commands

All commands support `--json`, `--config <path>`, `--verbose` and `--quiet` ([Global flags](#global-flags)). Every mutating command (`add`, `move`, `pin`, `unpin`, `priority`, `hold`, `release`, `resolve`, `cancel`, `retry`, `label add`, `label remove`, `prune`) accepts `--actor <actor>`, a repeatable `--decision-id <id>`, `--dry-run` (prints what would change and changes nothing, waits for nothing and signals nothing; `job` is the projected state after the change; usage and human-only checks still apply) and `--yes` (accepted and ignored: no command asks for confirmation, and `--yes` never substitutes for a [human-only flag](#human-only-flags)'s check). Mutating commands print `{"changed": <bool>, "dry_run": <bool>, "job": <job>}` (`schemas/cli/change.json`) unless stated otherwise; a command that finds nothing to change exits 0 with `changed: false`.

Checks run in this order: usage (exit 2), then not found or ambiguous (10, 21), then state refusals (20), then the [human-only](#human-only-flags) check (13), which applies only when the flag would take effect.

A decision id matches `^[A-Za-z0-9._:@/-]{1,128}$`. A command's decision ids are recorded in its log records (`transition`, `reorder` or `label`, [Audit and log](#audit-and-log)) and, for audited commands, in the audit record.

**Human output** sanitizes every string field (job fields, output tails, notes, hook messages) and every echoed input in error messages with the sanitizer of [Masking and sanitizing](#masking-and-sanitizing) before printing, and so does the [TUI](#tui). Human `add` prints only the job id on stdout; all other human output is unstable and not for parsing.

### Human-only flags

`cancel --force` and `release --override-escalation` are accepted only when stdin is a TTY or `--actor human:<user>` is given. Otherwise the command exits 13 (`needs_human`) without acting. Both are audited ([Audit and log](#audit-and-log)). The check is advisory: a trusted process can claim a human actor ([Residual risks](#residual-risks)).

**Actor default.** `human:<me.name>` when stdin is a TTY, otherwise `auto:unknown`. `me.name` defaults to the OS user name. An actor matches `^(human|auto):[A-Za-z0-9._@-]{1,64}$`. The actor `auto:jobsctl` is reserved for jobsctl's own transitions and retention prunes: `--actor auto:jobsctl` exits 2 (`usage`). Actors are claims, not proof ([Residual risks](#residual-risks)).

### Selectors

`cancel` and `list` accept selectors: `--queue Q`, repeatable `--label k=v` (AND) and `--unique-key K`. For `cancel`, selectors match non-terminal jobs only; no match exits 0 with `jobs: []`; several matches exit 21 (`ambiguous`, listing them in `candidates`) unless `--all` is given. An id together with selectors, and `--all` without a selector, exit 2. `not_found` applies to ids and `show --unique-key`, never to selectors.

### `add`

`jobsctl add [options] -- <argv...>`

| Flag | Meaning |
|---|---|
| `--queue Q` | queue (default `default`) |
| `--unique-key K` | [unique key](#unique-keys) |
| `--priority N` | -1000..1000, default 0 |
| `--top` | position at the top of its priority band |
| `--pin` | add pinned |
| `--cwd PATH` | working directory, default the caller's cwd; must be absolute after normalization (`.`/`..` resolved lexically; symlinks kept) |
| `--env VAR=value` | repeatable; stored, non-secret ([Job environment](#job-environment)) |
| `--pass-env VAR` | repeatable; value read from `env_file` at start, never stored |
| `--label k=v` | repeatable |
| `--timeout DURATION\|none` | [timeout](#timeouts); `none` overrides a queue timeout |
| `--max-retries N` | 0..100, overrides the queue's [automatic retry](#automatic-retry) count |
| `--kill-grace DURATION` | overrides the queue's `kill_grace` ([Signal sequence](#signal-sequence)) |
| `--no-start` | do not trigger a pass ([Start triggers](#start-triggers)) |
| `--wait` | after adding, behave as [`wait`](#wait) on the job, with `--wait-timeout DURATION` as wait's `--timeout` |

Usage errors (exit 2): empty argv, a NUL byte in any argument, more than 4096 arguments or 1 MiB of argv in total, a non-absolute `--cwd`, an invalid name, value or duration, more than 32 labels, more than 256 `--env` or 256 `--pass-env` entries, an `--env` value over 128 KiB, `--dry-run` with `--wait`, `--wait-timeout` without `--wait`. `add` does not check that the command or cwd exists; that happens at start. Exit 10 (`unknown_queue`) only with `strict_queues: true`.

**Secrets** (exit 2, `usage`; the message names the argument index or the variable, never a value): an argv element or `--env` value that contains a [masked value](#masking-and-sanitizing) of at least 8 characters (`details.code`: `secret_in_argv` with `details.index`, or `secret_in_env` with `details.variable`), and an `--env` name listed in `mask_env` or defined in `env_file` (`secret_in_env`; use `--pass-env`). This is defense in depth; keeping secrets out of argv stays the submitter's contract.

Output: `schemas/cli/add.json`, `{"created": <bool>, "queue_created": <bool>, "dry_run": <bool>, "waited": <bool>, "warnings": [<warning>...], "job": <job>}`. Warnings: `pass_env_undefined` (with `variable`), `existing_job_held` (with `job_id`). With `--dry-run`, nothing is written (not even the queue's first use): `job` is the existing job on a unique-key hit, else `null` (a job would be created). With `--wait` (`waited: true`), `job` is the job as [`show`](#show) prints it, after the wait; the exit code is wait's after a successful add, add's own otherwise. Ctrl-C during the wait prints the current job (as for exit 22) and exits 130.

### `list`

`jobsctl list [--queue Q] [--status S]... [--label k=v]... [--unique-key K] [--all] [--limit N]`

Lists jobs: by queue (sorted by name), then queued jobs by `rank`, then `starting`, `running` and `held` jobs by `enqueued_at` then id, then terminal jobs by `ended_at` (newest first) then id. Repeated `--status` values combine with OR. Without `--all` or `--status`, terminal jobs that ended more than 24 hours ago are omitted (a view filter: tracking which failed jobs were seen is the caller's concern, e.g. with a label). `--limit` has no default. Jobs in `list` have no `output` or `attempts`. Output: `schemas/cli/list.json`, `{"jobs": [<job>...], "truncated": <bool>}` (`truncated`: `--limit` cut the list).

### `show`

`jobsctl show (<id> | --unique-key K) [--attempt N] [--no-output]`

Shows one job with its `attempts` (oldest first; empty before the first claim) and the `output` tail of the current attempt (or of `--attempt N`; an attempt that does not exist exits 10). `--unique-key K` selects the non-terminal job holding K, else the most recently created job with K; none: exit 10. Output: `schemas/cli/show.json`.

### `wait`

`jobsctl wait <id> [--timeout DURATION]`

Blocks until the job is terminal or `held`, checking about once a second, then prints the job like `show` and exits:

| Exit | Job |
|---|---|
| 0 | `done` |
| 12 | `failed` |
| 13 | `held` |
| 20 | `canceled` (`reason` in the JSON says which) |
| 22 | `wait`'s own `--timeout` expired first (the job is printed in its current status) |
| 130 | interrupted by Ctrl-C (the job is printed in its current status) |

A job waiting for an automatic retry is `queued`, so `wait` keeps waiting. Exit 22 (`wait_timeout`) is jobsctl's only tool-specific exit code. Output: `schemas/cli/show.json` (exits 0, 12, 13, 20, 22, 130). If jobsctl is upgraded to a newer schema while `wait` waits, it exits 12 with the error `version_skew` instead ([Upgrades](#upgrades), [Errors](#errors)).

### `move`

`jobsctl move <id> (--before <id2> | --after <id2> | --top | --bottom)`

[Order within a queue](#order-within-a-queue). Refusals: `not_queued` (the job, or `id2`, is neither queued nor held), `other_queue`.

### `pin` / `unpin` / `priority`

`jobsctl pin <id>`, `jobsctl unpin <id>`, `jobsctl priority <id> <N>`. Allowed on queued and held jobs (a held job keeps them for its release); otherwise exit 20 (`not_queued`). The job keeps its position key, so it lands by age within its new group or band ([Order within a queue](#order-within-a-queue)). Each change is logged as a `reorder` event, like `move`.

### `hold` / `release`

`jobsctl hold <id>`: a queued job becomes `held` (`manual`), keeping its `not_before`. A starting or running job: exit 20 (`live`; jobsctl cannot pause a process). A held or terminal job: no-op.

`jobsctl release <id> [--override-escalation]`: [Hold reasons](#hold-reasons); the job returns to `queued` with `not_before` cleared. A queued job: no-op (`changed: false`), with or without `--override-escalation` (the flag takes no effect, so no human-only check). Refusals: `not_held` (starting, running or terminal), `not_releasable` (an `exit_13` hold without `--override-escalation`), `not_escalated` (`--override-escalation` on a manual or `exit_13` hold), `override_required` (an `escalated` hold without `--override-escalation`). `--override-escalation` is a [human-only flag](#human-only-flags); it sets the job's `override_hooks` to the attempt's `escalated_hooks`. They are kept until a start has actually run the `pre-start` hooks, also across automatic retries (a start that returns to `queued` before running them keeps them); only [`retry`](#retry) and a terminal end (e.g. a `cancel`) clear them otherwise.

### `resolve`

`jobsctl resolve <id> [--as done|failed|canceled] [--note-file PATH|-]`

Closes a held job for good into the given terminal status (default `done`) with reason `resolved`; the note (untrusted, at most 4096 bytes, else exit 2; read with `O_NONBLOCK`, at most 4097 bytes, from a regular file or stdin) and the actor are recorded on the job and on its current attempt, if any (`resolved_by`, `note`). A job already terminal with reason `resolved` and the same status: exit 0, `changed: false`. Refusal: `not_held` (with `details.status`).

To continue an `exit_13` job under the same id, [`retry`](#retry) it instead: no terminal hook fires.

### `cancel`

`jobsctl cancel (<id> | <selectors> [--all]) [--force] [--wait-timeout DURATION]`

| Job status | Result |
|---|---|
| `queued`, `held` | `canceled` (`canceled`) |
| `starting` | `canceled` (`canceled`), committed at once; the shim stops its running `pre-start` hook's process group and does not start the job. `--force` has no effect |
| `running` | refused (exit 20, `live`) without `--force`. With `--force` ([human-only](#human-only-flags)): the [Reconcile](#reconcile) rules are applied to the job first; then `cancel` commits `force_requested_by` (audited) and sends SIGTERM to the verified shim, which runs the [signal sequence](#signal-sequence) (for a `shim_lost` job, a `_kill` that `cancel` spawns runs it); `cancel` waits until the job is terminal, at most `--wait-timeout` (default 2 × the job's effective `kill_grace` from the shim's snapshot + 10 seconds; `0` returns at once with exit 0; on expiry, or Ctrl-C while it waits, exit 12, `cancel_pending`, the job keeps its force request); then `canceled` (`forced`). A repeated `cancel --force` on a job with a committed request writes no new audit record: it re-sends SIGTERM to the verified live shim, or spawns a new `_kill` when no live process holds the `job:<id>` lease, then waits |
| terminal | no-op (`changed: false`) |

`--force` on a job that is not `running` is ignored (a plain cancel, no human-only check). With selectors, `--force` is refused (exit 2), except with `--unique-key` as the only selector, which matches at most one job. Force-cancel other running jobs by id, one per command. `--wait-timeout` without `--force` exits 2. `changed` is `true` when the command committed a cancel or a force request (the job may then still be `running`). With `--dry-run`, `cancel --force` evaluates the Reconcile rules without applying them; `job` is the job a reconcile would finalize in its finalized state, otherwise the committed state (`running` with `force_requested_by` set; `changed: false` when a request is already committed). With `--all`, every cancelable match is canceled and refused ones are listed in `refused`; the exit is 20 only if nothing was canceled and something was refused. Output: `schemas/cli/cancel.json`, `{"changed": <bool>, "dry_run": <bool>, "jobs": [<job>...], "refused": [{"job_id", "reason"}...]}`; with selectors and `--all` it is printed also on that exit 20, while every other non-zero exit prints the error ([Errors](#errors)).

### `retry`

`jobsctl retry <id> [--top] [--attempt N] [--timeout DURATION|none] [--max-retries N] [--kill-grace DURATION]`

A terminal job, or a job held for `exit_13`, returns to `queued` in one transaction; the next claim creates attempt `attempt` + 1, and the previous attempts' status, reason, exit, timestamps and tail stay in `attempts` (an `exit_13` hold's attempt stays `held`; no terminal hook fires). The job keeps its id, so `JOBSCTL_JOB_ID` stays the same and a resume-aware job command finds what its earlier attempt started. It goes to the bottom of its priority band, or the top with `--top`, with `enqueued_at` reset; the automatic retry count starts again and `override_hooks` are cleared. `--timeout`, `--max-retries` and `--kill-grace` replace the corresponding `overrides`, with the grammar of [`add`](#add). `retry` records the job's `attempt` as `retried_from_attempt` and is audited (`retry`, with the changed `overrides` in `inputs`).

`--attempt N` means "retry the job as it was when its last attempt was N" and makes the command idempotent: whatever the job's status, it exits 0 with `changed: false` (and changes no override) when the job's `attempt` is above N, or when both its `retried_from_attempt` and its `attempt` are N (that retry already happened and nothing has run since). A caller that wants another retry after that omits `--attempt`. Refusals: `not_terminal` (a queued, starting or running job, including one waiting for an automatic retry, and a manual or escalated hold, which are released instead), `duplicate_key` (with `details.job_id` of the job now holding its key), `attempt_mismatch` (N is above the job's last attempt number).

### `label add` / `label remove`

`jobsctl label add <id> k=v...`, `jobsctl label remove <id> k=v...`. Any status. Each change is logged as a `label` event. Output: `schemas/cli/change.json`.

### `capacity`

`jobsctl capacity [--queue Q]`

Reports [Capacity](#capacity) for one queue (known or not) or for every known queue. Always exits 0 when it answers. Output: `schemas/cli/capacity.json`, `{"queues": [{"queue", "known", "cap", "running", "queued", "free", "effective_free"}...], "global": {"cap", "running", "queued", "free"}}`. An unknown queue is reported with `known: false` and `default_cap`.

### `queues`

`jobsctl queues`: every known queue with its effective settings (`cap`, `priority`, `timeout`, `max_retries`, `retry_delay`, `kill_grace`, `tail_bytes`, `tail_lines`), whether it is configured, `first_seen`, and its counts by status. Output: `schemas/cli/queues.json`, `{"queues": [<queue>...], "unmatched_selectors": [{"hook", "hook_id", "queue"}...]}`; `unmatched_selectors` lists hook `queues` and `exclude_queues` entries naming a queue that is neither configured nor known.

### `run`

`jobsctl run [--loop] [--interval DURATION] [--max-starts N]`: [Pass](#pass). Output: `schemas/cli/run.json`, `{"already_running": <bool>, "holder": {"pid", "since"}|null, "started": [<id>...], "reconciled": [<finding>...], "warnings": [<warning>...]}` (with `--loop`, one JSON line per pass; `holder` is the live lease holder when `already_running`). Warnings: `env_file_unsafe`, `shim_spawn_failed` (with `job_id`).

### `reconcile`

`jobsctl reconcile [--dry-run]`: the identity checks, delivery and retention of a pass ([Reconcile](#reconcile)) without claiming jobs. It takes the `runner` lease; if a live process holds it, it exits 0 with `already_running: true` and `holder`. At release it re-checks the `pass_requested` counter and spawns a pass if it changed ([Start triggers](#start-triggers)). Output: `schemas/cli/reconcile.json`, `{"already_running": <bool>, "holder": {"pid", "since"}|null, "dry_run": <bool>, "findings": [{"job_id", "finding", "action"}...]}`.

### `prune`

`jobsctl prune --older-than DURATION [--dry-run]`: deletes terminal jobs whose `ended_at` is before the cutoff, with their attempts and tails, in batches; their ids stay reserved ([Identifiers](#identifiers)). A job with undelivered hook events is never deleted. Every pass applies config `retention` (default `30d`; `null` disables) the same way, as actor `auto:jobsctl`. Audited. Output: `schemas/cli/prune.json`, `{"dry_run": <bool>, "pruned": [<id>...]}`.

### `export`

`jobsctl export (--audit | --log) [--since TIMESTAMP|DURATION] [--after-seq N]`: prints the records of `audit.jsonl` or `log.jsonl` as JSON lines ([Audit and log](#audit-and-log)). `--since` takes an RFC 3339 timestamp (inclusive) or a duration back from now; `--after-seq N` prints records whose `seq` is above N, reading only the current file. The records follow `schemas/audit.json` and `schemas/log.json`; `--json` has no effect.

### `cron install` / `cron uninstall`

`jobsctl cron install [--dry-run]` writes the managed crontab block `# jobsctl managed` with one entry, `* * * * * PATH='<path>' [XDG_CONFIG_HOME='<dir>' XDG_STATE_HOME='<dir>'] '<absolute path of jobsctl>' run --quiet`, and replaces an existing block. `<path>` is the installing process's `PATH` with `~/.local/bin` added, `~` expanded, and entries that are empty, relative, do not exist, are temporary (under `/tmp` or `/run/user`, such as a per-shell version-manager directory) or contain a control character dropped with a warning `path_entry_dropped`; the `XDG_*` variables are written only when set at install, and one containing a control character is left out with the same warning (naming the variable in `variable` instead of `path`). Every value is single-quoted (a `'` written as `'\''`), and `%` is escaped as `\%`. `cron uninstall` removes the block. No separate `reconcile` entry is needed: every pass reconciles. Output: `schemas/cli/cron.json`, `{"changed": <bool>, "dry_run": <bool>, "block": <string|null>, "warnings": [<warning>...]}` (`block: null` after `cron uninstall`).

### `browse`

`jobsctl browse [--focus <id>] [--result-file PATH]`: the TUI ([TUI](#tui)). Needs the `tui` extra (`uv tool install 'jobsctl[tui] @ git+...'`); without Textual it exits 2 with that install hint.

### Durations

A duration is `<integer><unit>` with unit `s`, `m`, `h` or `d` (e.g. `30s`, `10m`, `2h`, `30d`); zero is invalid, except `cancel --wait-timeout 0`. Config durations use the same grammar. In JSON output durations are integer seconds. `~` at the start of `env_file` and `env.path_prepend` entries expands to the user's home directory.

### Timestamps

Every timestamp is RFC 3339 in UTC with `Z` and millisecond precision (e.g. `2026-10-31T12:00:00.000Z`).

## Global flags

| Flag | Meaning |
|---|---|
| `--json` | machine output on stdout (versioned, `schemas/cli/`) |
| `--config PATH` | config file instead of `~/.config/jobsctl/config.yaml` (drop-ins are read next to it); made absolute and passed on to the passes a command triggers and to every [internal process](#internal-processes) they start. Config is per invocation while the state directory is shared ([Config](#config)) |
| `--verbose`, `--quiet` | more or less human output on stderr |
| `--version` | print the version |

## Exit codes

| Exit | Meaning in jobsctl |
|---|---|
| 0 | success, including no-ops, `already_running` and `add` on a held unique key |
| 1 | internal error, `busy` |
| 2 | usage, `config_invalid`, `tui_missing` |
| 10 | `not_found`, `unknown_queue` (only with `strict_queues`) |
| 12 | `wait`: the job `failed`; `cancel --force`: `cancel_pending`; `wait`, `cancel`, `browse`: `version_skew` |
| 13 | `needs_human`; `wait`: the job is `held` |
| 20 | `refused` (with a `reason`); `wait`: the job is `canceled` |
| 21 | `ambiguous` |
| 22 | `wait_timeout` |
| 130 | `wait`, `add --wait`: interrupted by Ctrl-C (the job is printed); `interrupted`: `hold`, `cancel` or `resolve` got SIGINT while running a synchronous hook ([Hook processes](#hook-processes)) |

jobsctl never exits 11: a `pre-start` veto ends the job (`canceled`, `vetoed`) inside its shim, not the command that triggered it.

**Refusal reasons** (exit 20): `live`, `not_queued`, `not_held`, `not_terminal`, `not_releasable`, `not_escalated`, `override_required`, `duplicate_key`, `other_queue`, `attempt_mismatch`.

## Errors

With `--json`, every non-zero exit prints `{"error": {...}}` on stdout (`schemas/cli/error.json`), with two exceptions. The `wait` outcomes (12, 13, 20, 22 and 130) print the job: `wait` prints `show.json`, `add --wait` prints `add.json`. `cancel` with selectors and `--all` that exits 20 (nothing canceled, something refused) prints `cancel.json`; every other non-zero exit of `cancel` prints the error (21 with `candidates`, 12 `cancel_pending` with `details.job_id`, 12 `version_skew`). A `wait` that exits 12 with `version_skew` prints the error, not the job. Human mode writes the message to stderr.

| Field | Present |
|---|---|
| `code` | always |
| `message` | always |
| `reason` | with `refused` |
| `candidates` | with `ambiguous`: the matching job ids |
| `details` | optional, code-specific (e.g. `job_id` for `duplicate_key` and `cancel_pending`, `status` for `not_held` from `resolve`, `limit` for the label limit, `code` with `index` or `variable` for a secret refused by `add`) |

| Code | Exit | Commands |
|---|---|---|
| `usage` | 2 | all |
| `config_invalid` | 2 | all |
| `tui_missing` | 2 | `browse` |
| `not_found` | 10 | every command that takes an id, `show --unique-key`, `show --attempt` |
| `unknown_queue` | 10 | `add` |
| `cancel_pending` | 12 | `cancel --force` |
| `version_skew` | 12 | `wait`, `cancel`, `browse` |
| `interrupted` | 130 | `hold`, `cancel`, `resolve` |
| `needs_human` | 13 | `cancel --force`, `release --override-escalation` |
| `refused` | 20 | `move`, `pin`, `unpin`, `priority`, `hold`, `release`, `resolve`, `cancel`, `retry` |
| `ambiguous` | 21 | every command that takes an id; `cancel` selectors |
| `busy` | 1 | all that open the database |
| `internal` | 1 | all |

## Hooks

| Hook | When | Can veto |
|---|---|---|
| `pre-start` | after the claim, before the job starts ([Start sequence](#start-sequence)) | yes: veto → `canceled` (`vetoed`); exit 13 → `held` (`escalated`) |
| `post-start` | the job entered `running` | no |
| `post-finish` | the job ended `done` | no |
| `post-fail` | an attempt failed (including `lost`, `timeout`, `terminated`), or `resolve --as failed` | no |
| `post-held` | the job became `held` | no |
| `post-cancel` | the job ended `canceled` (including `vetoed`) | no |

The shim runs the `pre-start` hooks; [Delivery](#delivery) defines who runs the post hooks.

Hooks are configured under `hooks.<name>` as a list of `{name, cmd, timeout, async, include_output, queues, exclude_queues, labels}` (design guide §8): `cmd` runs via `/bin/sh -c` ([Hook processes](#hook-processes)) and is never expanded with job data; `name` (optional, `^[a-z0-9][a-z0-9_-]{0,63}$`) gives the hook its **identity**, which is `name` if set, else `cmd`; `timeout` is a duration, default `30s`; `async: true` (post hooks only) runs it without blocking delivery of the job's other events ([Hook processes](#hook-processes)); `include_output: true` (post hooks only, default `false`) puts the output tail into its payload; `queues` (default `null`: every queue), `exclude_queues` and `labels` (pairs, AND) select which jobs fire it. A `queues` or `exclude_queues` entry naming a queue that is neither configured nor known logs the warning `unknown_queue_selector` (once per change of state; never `config_invalid`, also with `strict_queues: true`). There is no per-invocation `--hook` flag (hooks run in the shim, not in the submitting command).

Pre-hook outcomes follow design guide §8: hooks run in order; exit 0 continues; exit 13 escalates and the rest still run; any other non-zero exit or a timeout vetoes and stops. A hook's stdout and stderr are captured in memory up to 64 KiB each and are not stored; stdout is parsed for `decision_id`, and the stderr of a vetoing or escalating `pre-start` hook is kept on the attempt (`hook_messages`, at most 4096 bytes), masked and sanitized ([Masking and sanitizing](#masking-and-sanitizing)). Every hook run is logged as a `hook_ran` event (`hook`, `hook_id`, `outcome`: `allow`, `escalate`, `veto`, `failed` or `timeout`, `exit`, `decision_ids`, and `stderr` only for a failure, timeout, veto or escalation: at most 2 KiB of masked, sanitized stderr). A `decision_id` (string or list) in a hook's JSON stdout is recorded on the attempt and in the log; a value that does not match the decision-id grammar ([Commands](#commands)) is dropped with the warning `invalid_decision_id`.

**Environment.** A hook gets the [canonical environment](#canonical-environment) of the process that fires it plus the `env_file` values (except the [ignored names](#canonical-environment)), minus the variables in `hooks_strip_env`, plus `JOBSCTL_HOOK=<hook>`, `JOBSCTL_ID=<job id>` and `JOBSCTL_OBJECT_TYPE=job`. Data comes only from stdin.

### Hook processes

A hook runs as `/bin/sh -c <cmd>` in a new process group, with cwd jobsctl's state directory and its stdout and stderr captured. The process that fires it is its parent and waits for it with `waitid(WEXITED|WNOWAIT)`, keeping the hook's leader unreaped until every group signal and the group probe are done; it never sends a group signal after reaping the leader, and it reaps only that leader, by its PID ([Reaping](#internal-processes)). On timeout, or when a `starting` job is canceled or its shim gets SIGTERM, it sends SIGTERM to the hook's process group, waits 5 seconds, then sends SIGKILL to the group.

A CLI command (`hold`, `cancel`, `resolve`) that receives SIGINT, SIGHUP or SIGTERM while it runs a synchronous hook stops that hook the same way (SIGTERM to its group, 5 seconds, SIGKILL, the leader held unreaped), leaves the event undelivered (`_deliver` delivers it later, [Delivery](#delivery)) and exits 130 (`interrupted`) for SIGINT, else 1 (`internal`).

**Async hooks.** A lease holder never runs an `async` hook itself: it leaves the event in the outbox (marked async) and [requests delivery](#pass) (step 5: the `deliver` lease with the counter re-check). `_deliver` ([Internal processes](#internal-processes)) is the parent of at most 8 concurrent async hook processes across all jobs, at most one per job at a time, in outbox order per job; it captures and masks their output like a synchronous hook's, enforces their timeouts under the rule above, and marks an async event delivered only when its hook process has ended (exit or timeout), so async delivery stays at least once. `_deliver` runs async events whether or not the job's `job:<id>` lease has a live holder.

### Payload

Every hook gets one JSON document on stdin (`schemas/hooks/<hook>.json`, all built on `schemas/hooks/payload.json`):

| Field | Meaning |
|---|---|
| `tool` | `"jobsctl"` |
| `hook` | the hook name |
| `event_id` | unique per event; the same on redelivery. `pre-start` is not an outbox event: its `event_id` is `<claim_id>:pre-start:<hook index>` |
| `object_type` | `"job"` |
| `object_id` | the job id |
| `repo` | always `null` (jobsctl has no repo notion; policies match on `labels`) |
| `labels` | the job's labels |
| `previous_status` | the status before this transition |
| `job` | the [job object](#job-object) after the transition, with `env` reduced to variable names (`env_names`) |
| `attempt` | the [attempt object](#attempt-object) the event concerns (for `post-fail`, the failed attempt); `null` when the transition involved no attempt (a manual hold, a cancel or resolve of a queued or manually held job) |
| `output_available` | `post-finish`, `post-fail`, `post-held`, `post-cancel`: `true` whenever the job process of the attempt ran and a tail exists |
| `output_tail` | `post-finish`, `post-fail`, `post-held`, `post-cancel`: the final masked, sanitized tail for a hook with `include_output: true` when `output_available`, else `null`; **untrusted**. Notification templates should leave it out |

### Delivery

A transition and its post hook event are committed in the same transaction (an outbox); `pre-start` hooks are not outbox events (the shim runs them, [Start sequence](#start-sequence) step 3). Only the holder of the job's `job:<id>` lease delivers the job's synchronous events, oldest first, and every holder delivers its job's undelivered synchronous events before it releases the lease; `async` events are always run by `_deliver` ([Hook processes](#hook-processes)):

- the shim while it lives; a new shim first delivers the events left by earlier attempts, before its `pre-start` hooks ([Start sequence](#start-sequence) step 2);
- a command that delivers synchronously (`hold`, `cancel`, `resolve`) takes the lease only while delivering; if a live process holds it, the command leaves the events to that holder;
- `_kill` for the job it finalizes;
- `_deliver`, started by a pass or by a process that leaves `async` events, for events of jobs whose lease has no live holder ([Reconcile](#reconcile) `hooks_redelivered`), and for every `async` event.

Delivery is **at least once and in order per job** (a job's synchronous events in outbox order, and its `async` events in outbox order): the same `event_id` is used on redelivery. A failing or timed-out post hook is logged (`hook_ran`, outcome `failed` or `timeout`) and never retried. A job's synchronous hooks never run concurrently, nor do its `async` hooks; an `async` hook may run while one of the job's synchronous hooks runs ([Hook processes](#hook-processes)). A redelivered event uses the redelivering process's config; a shim uses the config it read at start.

## Config

`~/.config/jobsctl/config.yaml` (respecting `XDG_CONFIG_HOME`) plus `config.d/*.yaml` drop-ins in lexical order (maps merged, hook lists appended); validated at every command (`config_invalid`, naming the key and, for `env.*` keys, never the value; a pass logs it once per change of state). Config comes only from files: there are no `JOBSCTL_<KEY>` variables. Precedence: flags > per-queue settings (`queues.<name>`) > global config > built-in defaults. Schema: `schemas/config.json`; every key with its default: `config.example.yaml`.

Config is per invocation, while the state directory is shared: a job added with `--config X` starts under the config of whichever process claims it (a cron pass, or a pass triggered by another command, uses the default config). For a separate queue system, use `--config X` together with a matching `XDG_STATE_HOME`.

| Key | Default | Meaning |
|---|---|---|
| `default_cap` | `1` | cap of a queue with no configured `cap`; `null` = unbounded |
| `global_cap` | `null` | total running jobs across queues; `null` = none |
| `strict_queues` | `false` | `add` to an unknown queue exits 10; affects `add` only |
| `auto_run` | `true` | [Start triggers](#start-triggers) |
| `run_interval` | `10s` | `run --loop` interval |
| `kill_grace` | `30s` | per step of the [signal sequence](#signal-sequence) |
| `tail_bytes` | `16384` | [Output capture](#output-capture) (max 262144) |
| `tail_lines` | `200` | |
| `tail_flush_interval` | `5s` | |
| `mask_env` | `[]` | variable names whose values are masked in tails and hook output; `--env` refuses them |
| `retention` | `30d` | terminal jobs whose `ended_at` is older than this are pruned by each pass; `null` keeps them |
| `env.inherit` | see [Job environment](#job-environment) | names or `PREFIX*` |
| `env.set` | `{}` | fixed non-secret values for jobs; `PATH` set here also replaces the inherited `PATH` of hooks and jobsctl's own processes (recommended, [Canonical environment](#canonical-environment)) |
| `env.path_prepend` | `[]` | absolute directories (`~/` allowed) put in front of `PATH`, for jobs, hooks and jobsctl's own processes |
| `env_file` | `null` | path of a `NAME=value` file of secrets, a trust anchor ([Canonical environment](#canonical-environment)); `~/` allowed |
| `hooks` | `{}` | [Hooks](#hooks) |
| `hooks_strip_env` | `[]` | variables removed from hook environments |
| `busy_timeout` | `5s` | SQLite busy timeout |
| `me.name` | OS user name | for the default actor |
| `queues.<name>.cap` | `default_cap` | integer ≥ 0 or `null` (0 holds the queue: nothing starts) |
| `queues.<name>.priority` | `0` | [Selection](#selection), -1000..1000 |
| `queues.<name>.timeout` | `null` | [Timeouts](#timeouts) |
| `queues.<name>.max_retries` | `0` | [Automatic retry](#automatic-retry), 0..100 |
| `queues.<name>.retry_delay` | `60s` | |
| `queues.<name>.kill_grace` | global value | |
| `queues.<name>.tail_bytes`, `tail_lines` | global values | |

## Environment

| Variable | Where | Meaning |
|---|---|---|
| `JOBSCTL_JOB_ID` | job | the job id (same across attempts) |
| `JOBSCTL_ATTEMPT` | job | the attempt number, from 1 |
| `JOBSCTL_HOOK`, `JOBSCTL_ID`, `JOBSCTL_OBJECT_TYPE` | hooks | design guide §8 |

jobsctl reads no `JOBSCTL_*` variable and removes every one from the [canonical environment](#canonical-environment); the `JOBSCTL_` prefix is forbidden only for the names given with `--env` and `--pass-env`.

## Audit and log

`~/.local/state/jobsctl/` (respecting `XDG_STATE_HOME`; mode 0700) holds `state.db` (private, 0600), `shims/` (0700, shim diagnostics and result files, removed with their jobs), `diag/` (0700, diagnostics of the other [internal processes](#internal-processes), deleted after 7 days) and the public, append-only files:

- **`audit.jsonl`** (`schemas/audit.json`): `seq`, `ts`, `actor`, `decision_id` (list), `target` (job id; `null` for `prune`), `action`, `inputs` (`argv_sha256`, `attempt`, `unique_key`, `queue`, `overrides`), `result` (the resulting status; `deleted` for `prune`), for: `cancel --force`, `cancel` of a held job, `release --override-escalation`, `resolve`, `retry` and `prune` (one record per pruned batch, with `inputs` `ids`, `older_than` in seconds and `trigger`: `command` or `retention`). Actions: `cancel_force`, `cancel_held`, `release_override`, `resolve`, `retry`, `prune`.
- **`log.jsonl`** (`schemas/log.json`): every status transition (`event: transition`, with seq, job_id, queue, from, to, reason, hold_reason, attempt, actor, decision_ids, attempt_status, attempt_reason, retry_pending), `reorder` (move, pin, unpin, priority), `label` (label add or remove), `queue_created`, `shim_spawned`, `shim_spawn_failed`, `shim_lost`, `leftovers`, `short_secret_not_masked`, `env_file_unsafe`, `env_file_line_ignored`, `env_file_line_invalid`, `path_entry_dropped`, `pass_env_undefined`, `missing_env`, `unique_key_reused` (with `argv_sha256`), `hook_ran` (see [Hooks](#hooks)), `invalid_decision_id`, `unknown_queue_selector`, `config_invalid`, `runner_stuck`, `reconcile` (with `finding` and `action`), `pass` summaries (only for a pass that changed something).

An audit record's `inputs` carry `overrides` only for a `retry` whose flags changed the job's `overrides`, holding them after the change. A `transition` record carries the command's `decision_ids` and the job's `attempt`; whenever the transition changes an attempt's status (an attempt ends, or a `resolve` or `cancel` of an `escalated` or `exit_13` hold), also `attempt_status`, `attempt_reason` and `retry_pending` (so an attempt that failed and was requeued for an automatic retry shows its reason). `reorder` and `label` records carry `job_id`, `queue`, `actor` and `decision_ids`. State warnings (`env_file_unsafe`, `env_file_line_ignored`, `env_file_line_invalid`, `path_entry_dropped`, `unknown_queue_selector`, `config_invalid` from passes) are logged once per change of state, not at every load.

`argv_sha256` is SHA-256 over each argv element in UTF-8 followed by a NUL byte. Every record carries `seq` from a per-file counter in `state.db`: the writer takes an exclusive `flock` on the file, increments the counter in its own short transaction, appends the line and releases the lock, so records appear in file order and `seq` has gaps only after a crash; the counter survives external rotation. No process appends to either file inside a database write transaction: it logs after the commit. Levels: `info`, `warn`, `error`. jobsctl does not rotate either file; audit records are kept forever (architecture §14).

Neither file contains a `--pass-env` value, an `env_file` value, argv, raw hook output or output tails; `--env` values (non-secret by contract) are not logged either, only their names. Other secret values are masked; masking is best-effort ([Residual risks](#residual-risks)).

## TUI

`jobsctl browse` (Textual, optional extra):

- **Views:** the queues with their capacity; the selected queue's jobs in rank order, then `starting`, `running`, `held` and recent terminal jobs; a detail pane with the job, its attempts and the live, sanitized tail.
- **Keys:** `j`/`k` and the arrow keys navigate, `K`/`J` move the job before/after its neighbor, `t`/`b` top/bottom, `p` pin/unpin, `+`/`-` priority up/down by 1, `h` hold, `e` release (an escalated hold asks for confirmation, then passes `--override-escalation --actor human:<me.name>`), `v` resolve (asks for `--as` and an optional note), `c` cancel (a running job asks for confirmation, then passes `--force --wait-timeout 0 --actor human:<me.name>` and shows the job as canceling while `force_requested_by` is set), `R` retry, `r` refresh, `/` filter, `enter` details, `?` help, `q` quit.
- **`--focus <id>`** opens on that job; **`--result-file PATH`** writes `{"intent": "back"|"open", "selected": [<job id>...]}` on exit (`schemas/cli/browse-result.json`): `back` returns to the caller, `open` means the user asked the caller to open the selected jobs (e.g. in the caller's own view of them). Without the flag nothing is written.

Every TUI action is a CLI command.

## Platform notes

- **Linux and WSL:** identity from `/proc` ([Process identity](#process-identity)); the shim is a child subreaper; exact signals use pidfds where available ([Signal sequence](#signal-sequence)), and a `shim_lost` group signal uses `PIDFD_SIGNAL_PROCESS_GROUP` on Linux 6.9 or later ([Signal sequence](#signal-sequence)). Shutting down the WSL VM ends every shim and job; the next pass sees a new boot id and marks their jobs `lost`. A shutdown or a logout with `KillUserProcesses` sends SIGTERM to the shims, so their jobs end `failed` (`terminated`). Cron runs only if the distribution runs `cron`.
- **macOS:** identity through `libproc` and `sysctl` (`kern.bootsessionuuid`); no subreaper, so [leftovers](#leftovers) are counted only by probing the job's process group; no pidfds, so signals to processes jobsctl did not spawn are checked immediately before sending, and `PR_SET_DUMPABLE` does not exist.
- **Cron:** cron's environment is minimal and its `python3` may be older than 3.12; [`cron install`](#cron-install--cron-uninstall) writes the absolute path of `jobsctl`, an explicit `PATH` and the `XDG_*` directories set at install.
- **Requirements:** Python ≥ 3.12; SQLite ≥ 3.35.

## Residual risks

- **Untrusted argv.** jobsctl cannot tell whether a submitter put untrusted text in argv; the contract is the submitter's.
- **Masking limits.** Masking is best-effort. Only values of `mask_env`, `--pass-env` and `env_file` variables are masked; secrets a job obtains itself (a token command, a login store, a keyring) are not. Values shorter than 8 characters, encoded forms and partial values are not masked either. Tails and hook output are untrusted and should not be forwarded to notifications. `add` refuses known secret values in argv and `--env`, but cannot catch others.
- **Advisory human gate.** Any process with a TTY, or one that passes `--actor human:<user>`, can force-cancel or override an escalation. Trusted agents run as the user and can do anything the user can. The `actor` of an audit record (e.g. `cancel_force`, `release_override`) is a claim, not proof of a human.
- **Trust anchors.** `state.db`, `shims/`, `diag/`, the config (with its drop-ins and any `--config` file) and `env_file` direct jobsctl: whoever can write them can aim jobsctl's signals at any of the user's processes and run code as the user. They must be outside every path an isolated session can write; keep `--config` and `env_file` under user-private directories.
- **Signal windows.** On Linux before 6.9, the group signal `_kill` sends to a `shim_lost` job has a short window between verifying the leader and signaling the group, in which the group id could be reused. On macOS, and on Linux where pidfds are unavailable (an architecture outside the system-call fallback's allow list, `ENOSYS`, `EPERM` from a seccomp filter, WSL1), every signal to a process jobsctl did not spawn (a shim signaled by `cancel`, a `shim_lost` job's leader) has such a window.
- **Trigger `PATH`.** Without `env.set.PATH`, the `PATH` inherited from the process that started the trigger chain reaches hooks, jobs and jobsctl's own processes. An absolute entry inside an agent-writable directory (e.g. a worktree's `node_modules/.bin` or `.venv/bin`) is kept by the cleaning step, so a sandboxed agent could plant a command there that an unsandboxed hook then runs with the `env_file` values ([Canonical environment](#canonical-environment)).
- **Exit 143.** A job that traps SIGTERM, or a shell reporting a killed child, is indistinguishable from a real exit with that status ([Exit record](#exit-record)).
- **Orphans after a shim death.** A job whose shim died keeps running until it exits or a human force-cancels it, and its timeout is no longer enforced; a job that writes output after its shim died is killed by SIGPIPE ([Output capture](#output-capture)).
- **Durability.** `synchronous=NORMAL` can lose the last committed transitions on an operating-system crash or power loss (not on a process crash); the next pass reconciles from process identities.
