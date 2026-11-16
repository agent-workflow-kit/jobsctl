# jobsctl

Generic job queue with concurrency caps, ordering and held jobs.

jobsctl is an awkit L1 primitive: a command-line tool that stores jobs (an argv array plus a working directory, an environment description, a queue and labels), starts them under per-queue and global concurrency caps, and keeps their status, exit record and masked output tail. It is fully standalone: no daemon is required (a short-lived `jobsctl run` pass starts work, triggered by your commands, by cron, or by `run --loop`), and it does not know who calls it.

**What it is:**

- A local, single-user queue on top of one SQLite file. Jobs run detached, so they outlive the command that started them.
- Caps (per queue and global), priority, pinning and manual ordering, holds, retries, timeouts and hooks.
- An audit trail and a structured log, with secrets masked.

**What it is not:**

- Not a scheduler of times (it has no "run at 3 am"; use cron to call `add`).
- Not a distributed system: one machine, one user, one state directory.
- Not a shell: a job is an argv array, never a shell string. Run `sh -c '...'` yourself if you need one.
- Not a secret store. It keeps secrets out of its own records on a best-effort basis ([Secrets](#secrets)).

This README is a guide. The normative rules, exact JSON shapes and every edge case are in [docs/interface.md](docs/interface.md); the guide links into it instead of repeating it.

## Status

Implemented and released (see [CHANGELOG.md](CHANGELOG.md)) except the `browse` TUI ([#29](https://github.com/agent-workflow-kit/jobsctl/issues/29)) and macOS support ([#28](https://github.com/agent-workflow-kit/jobsctl/issues/28)). Today `jobsctl browse` exits 1 with `browse: not implemented yet`; the `tui` extra does not exist yet. Everything the TUI would do is available as a CLI command.

Platforms: Linux and WSL (Python 3.12 or newer, SQLite 3.35 or newer). macOS is best-effort and not yet verified. Details: [Platform notes](docs/interface.md#platform-notes).

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/jobsctl
```

For development, clone the repository and run `uv sync`, then `uv run jobsctl ...`.

## Quick start

Everything below was run against a private, empty state directory. Ids differ on every run.

```sh
# Optional: ~/.config/jobsctl/config.yaml (every key and default: config.example.yaml)
cat > ~/.config/jobsctl/config.yaml <<'EOF'
default_cap: 1
queues:
  builds:
    cap: 2
EOF

jobsctl add --queue builds --label kind=build --unique-key build:main -- sleep 1
```

```text
jb-jp4rym5f
```

`add` prints only the job id on stdout (the one stable human output). With `auto_run: true` (the default) it also starts a pass, so the job is already starting:

```sh
jobsctl list
```

```text
jb-jp4rym5f running queue=builds -- sleep 1
```

```sh
jobsctl capacity
```

```text
builds: cap=2 running=1 queued=0 free=1 effective_free=1
global: cap=None running=1 queued=0 free=None
```

```sh
jobsctl wait jb-jp4r      # any unique prefix of 4 or more characters; blocks until done, failed, held or canceled
echo $?
```

```text
jb-jp4rym5f: done (exit)
0
```

`show jb-jp4r` prints the job with its attempts and output tail; `show jb-jp4r --json` is the machine form. `list` hides terminal jobs that ended more than 24 hours ago; use `--all` or `--status` to see them. Set `auto_run: false` (or pass `--no-start`) if you want nothing to start until you run `jobsctl run` yourself.

To keep work flowing after reboots and lost shims, add the cron safety net once: `jobsctl cron install` ([Cron](#running-passes-auto_run-cron-run---loop)).

## Concepts in one minute

- **Job:** argv + cwd + env + queue + labels + optional unique key. Id `jb-` plus 8 characters; any unique prefix of 4 or more works as input ([Identifiers](docs/interface.md#identifiers)).
- **Queue:** a name, created on first use with `default_cap`. The cap bounds `starting` + `running` jobs in that queue; `global_cap` bounds all of them ([Queues](docs/interface.md#queues), [Capacity](docs/interface.md#capacity)).
- **Status:** `queued`, `starting`, `running`, `held` are live; `done`, `failed`, `canceled` are terminal, each with a `reason` ([Statuses and reasons](docs/interface.md#statuses-and-reasons)).
- **Attempt:** each claim of a job is one attempt; `retry` and automatic retries add attempts to the same job id.
- **Held:** paused for a human or the submitter. The hold reason (`manual`, `escalated`, `exit_13`) decides how it ends ([Hold reasons](docs/interface.md#hold-reasons)).
- **Pass:** one `jobsctl run`: reconcile, claim up to the caps, start a detached shim per job ([Runner](docs/interface.md#runner)).

## Submitter guide

This is for tools (and people scripting) that put work into jobsctl and read results back. Rely on `--json` output, exit codes and the schemas in [schemas/cli/](schemas/cli); human output other than the id printed by `add` is not stable.

### Submit idempotently

Name each piece of work with `--unique-key`. Keys are unique across all queues among non-terminal jobs, so a second `add` while the first is queued, running or held returns the existing job and creates nothing:

```sh
jobsctl add --json --queue builds --unique-key build:main -- sleep 1
```

A repeat call gives `"created": false` and the existing job, with exit 0. If the existing job is `held`, the result also carries the warning `existing_job_held`. A terminal job does not block a new one with the same key. The new call's argv, labels and settings are ignored on a hit, so a caller that changes its mind about the argv must cancel the old job first. Details: [Unique keys](docs/interface.md#unique-keys).

Find the job again later with `jobsctl show --unique-key build:main --json` (exit 10 if there is none).

### Labels

`--label key=value` (repeatable) on `add`, `label add` and `label remove`. jobsctl assigns labels no meaning; use them to find your own jobs (`list --label kind=build`, `cancel --label ... --all`) and to select hooks. A key is lowercase letters, digits, `_`, `.`, `-`; a job has at most 32 labels ([Labels](docs/interface.md#labels)).

### Keep untrusted text out of argv

A job's argv must never contain agent-generated or otherwise untrusted text. Pass such text in a file that the command reads. jobsctl cannot check this; it is your contract ([Jobs](docs/interface.md#jobs)).

### Check capacity before submitting

```sh
jobsctl capacity --queue builds --json
```

```json
{"queues": [{"queue": "builds", "known": true, "cap": 2, "running": 1, "queued": 0, "free": 1, "effective_free": 1}], "global": {"cap": null, "running": 1, "queued": 0, "free": null}}
```

Start new work only while `effective_free` is greater than 0 (or `null`, which means unbounded). `effective_free` is the smaller of the queue's free slots and the global free slots, and it counts queued jobs against the cap, so a caller that submits one job per free slot does not overfill the queue. `capacity` exits 0 whenever it answers, also for an unknown queue (`"known": false`, with `default_cap` as its cap). Details: [`capacity`](docs/interface.md#capacity-1).

### Wait and read the result

```sh
jobsctl wait jb-jp4r --timeout 30m --json    # prints the job (same shape as show --json)
echo $?
```

`wait` blocks until the job is terminal or held, and exits:

| Exit | Meaning |
|---|---|
| 0 | `done` |
| 12 | `failed` |
| 13 | `held` (any hold reason) |
| 20 | `canceled` (the `reason` says which) |
| 22 | `wait`'s own `--timeout` expired; the job is printed in its current status |
| 130 | interrupted (Ctrl-C, SIGTERM, SIGHUP); the job is printed; the job is not changed |
| 10 | no such job (or it was pruned while you waited) |

A job waiting for an automatic retry is `queued`, so `wait` keeps waiting. Interrupting or timing out a wait never changes the job. `wait` is read-only: it opens the database read-only, so it never creates state and, after a jobsctl upgrade with a database migration, exits 1 until any writing command has migrated the database. If jobsctl is upgraded while a `wait` runs, it exits 12 with the error code `version_skew`: run it again. The `--json` output of a failing wait is the job, except for `version_skew`, which prints the error envelope. `add --wait [--wait-timeout DURATION]` does the same in one command (exit 22 when the wait timed out; the job is already committed). Details: [`wait`](docs/interface.md#wait).

`show --json` carries `status`, `reason`, `hold_reason`, `exit` (`kind`, `code`, `signal`), `attempts` (oldest first) and `output` (the tail of the current attempt). The output tail is untrusted text: it is masked and sanitized, but it can still contain anything the job printed, so never put it in argv or forward it unreviewed ([Output capture](docs/interface.md#output-capture)). `show --no-output` omits it.

### Exit codes

The same table applies to every command; `wait` adds the job outcomes (Details: [Exit codes](docs/interface.md#exit-codes), [Errors](docs/interface.md#errors)).

| Exit | Meaning |
|---|---|
| 0 | success, including no-ops, `already_running`, and `add` on a held unique key |
| 1 | internal error, or database busy |
| 2 | usage error, invalid config, `browse` without the TUI extra (the TUI is not implemented yet) |
| 10 | not found, or unknown queue (only with `strict_queues`) |
| 12 | `wait`: job failed; `cancel --force`: still pending; `wait`/`add --wait`/`cancel`: `version_skew` |
| 13 | needs a human (`cancel --force`, `release --override-escalation`); `wait`: job held |
| 20 | refused, with a `reason`; `wait`: job canceled |
| 21 | ambiguous id prefix or selector |
| 22 | `wait` timed out |
| 130 | `wait` or `add --wait` interrupted, or a command interrupted while it ran a hook |

jobsctl never exits 11. With `--json`, every non-zero exit other than the `wait` outcomes prints `{"error": {"code", "message", ...}}` on stdout; see [Errors](docs/interface.md#errors) for the codes and refusal reasons. Examples (real output):

```text
$ jobsctl wait jb-zzzz
jobsctl: error: no job matches 'jb-zzzz'
[exit 10]
$ jobsctl release jb-iv3guk5r
jobsctl: error: jb-iv3guk5r was escalated by a pre-start hook; release it with --override-escalation
[exit 20]
```

### Failures, retry and resolve

Terminal and held outcomes are ended by their reason, not retried silently:

- `retry <id>` requeues a `done`, `failed` or `canceled` job, or a job held because its own command exited 13, as a new attempt under the same id (so `JOBSCTL_JOB_ID` stays the same and a resume-aware command can find its earlier work). It goes to the bottom of its priority band (`--top` for the top). To make a retry idempotent, pass `--attempt N`, the attempt number you saw fail: a repeat call with the same N is a no-op (`changed: false`) once the retry has happened. Refused with `not_terminal` for queued, running and manually or escalation-held jobs, and with `duplicate_key` if another live job now holds the unique key ([`retry`](docs/interface.md#retry)).
- `resolve <id> [--as done|failed|canceled] [--note-file PATH|-]` closes a held job for good, with the reason `resolved` and an optional note. This is how a submitter finishes an exit-13 hold it has dealt with another way.
- Exit 13 from the job's own command is the convention for "I need a decision". The job becomes `held` (`exit_13`); `release` is refused for it (exit 20). The submitter chooses `retry` (continue) or `resolve` (end).

```text
$ jobsctl wait jb-l3kz7as7
jb-l3kz7as7: held (exit_13)
need human
[exit 13]
$ jobsctl release jb-l3kz7as7
jobsctl: error: jb-l3kz7as7 was held for exit 13; retry it to continue the work, or resolve it
[exit 20]
$ jobsctl retry jb-l3kz7as7
jb-l3kz7as7: queued
```

Optional automatic retries: a queue with `max_retries` above 0 keeps a failed job `queued` with a `not_before` delay (`retry_delay`, default 60 s) instead of `failed`. An automatic retry only starts when a pass runs after the delay, so it needs `auto_run` triggers, cron or `run --loop` ([Automatic retry](docs/interface.md#automatic-retry)).

### Cancel, hold, release, order

- `cancel <id>` cancels a `queued`, `starting` or `held` job. A running job is refused (exit 20, reason `live`). Selectors (`--queue`, `--label`, `--unique-key`) cancel several: more than one match is exit 21 unless you add `--all`.
- `cancel <id> --force` stops a running job with the signal sequence (SIGTERM to the job, then its process group, then SIGKILL, each after `kill_grace`). It is a human-only action: it needs a TTY on stdin or `--actor human:<user>`, otherwise it exits 13 without acting. Workflow tools should not claim a human actor; ask the person.

```text
$ jobsctl cancel jb-xhnk3ecp
jobsctl: error: jb-xhnk3ecp is running; cancel --force stops it
[exit 20]
$ jobsctl cancel jb-ysq3sj5t --force
jobsctl: error: cancel --force needs a human: run it from a terminal or pass --actor human:<user>
[exit 13]
$ jobsctl cancel jb-xhnk3ecp --force --actor human:ops
jb-xhnk3ecp: canceled
```

- `hold <id>` pauses a queued job (a starting or running job is refused: jobsctl cannot pause a process). `release <id>` puts it back at its previous rank.
- `move <id> --before|--after <id2>` or `--top|--bottom`, `pin`, `unpin`, `priority <id> <N>` reorder queued and held jobs. Order is: pinned first, then priority (highest first), then position ([Order](docs/interface.md#order)).
- Every mutating command accepts `--dry-run`, `--actor`, a repeatable `--decision-id` (recorded in the log and audit) and `--yes` (a no-op; nothing asks for confirmation).

### Hooks and decision ids

Hooks are shell commands configured by the operator (see [Hooks](#hooks-and-the-pre-start-gate)). A hook receives a JSON payload on stdin and may print `{"decision_id": "..."}` as its stdout; those ids are recorded on the attempt and in the log, and appear in `show --json` under `attempts[].decision_ids`. A submitter does not attach hooks to a job: hooks run in the shim and are selected by queue and labels, which is why consistent labels matter. Payload shapes: [schemas/hooks/](schemas/hooks); rules: [Hooks](docs/interface.md#hooks).

## Operator guide

### Config

`~/.config/jobsctl/config.yaml` (respecting `XDG_CONFIG_HOME`), plus drop-ins `config.d/*.yaml` read in lexical order (maps are merged, `hooks` lists appended, other lists replaced). Every key with its default is in [config.example.yaml](config.example.yaml); the schema is [schemas/config.json](schemas/config.json); the rules are in [Config](docs/interface.md#config). Points worth knowing:

- Config comes only from files: there are no `JOBSCTL_<KEY>` environment variables.
- Precedence: flags, then `queues.<name>`, then the top-level file, then built-in defaults.
- A bad config exits 2 (`config_invalid`) and names the key, never a secret value. YAML is strict (`yes`/`on` are strings, no anchors, no duplicate keys).
- Config is per invocation but the state directory is shared. A job added with `--config X` starts under the config of whichever pass claims it (usually cron's default). For a genuinely separate queue system, use `--config X` together with its own `XDG_STATE_HOME`.
- Set `env.set.PATH` (recommended). Without it, the `PATH` of whatever started the chain (cron, a shell, an agent session) reaches hooks and jobs, including any writable directory on it ([Canonical environment](docs/interface.md#canonical-environment)).

### Queues and caps

```yaml
default_cap: 1          # a queue not listed below gets this cap
global_cap: 4           # optional: total starting + running jobs
queues:
  builds:
    cap: 2
    priority: 10        # between queues: highest first
    timeout: 2h         # job timeout (none by default)
  reviews:
    cap: 3
    max_retries: 2
    retry_delay: 60s
  paused:
    cap: 0              # holds the queue: nothing starts
```

`jobsctl queues` lists every known queue with its effective settings and counts. Lowering a cap never stops running jobs; it only blocks new starts, and takes effect at the next pass. `strict_queues: true` makes `add` to an unlisted queue exit 10 (it catches typos; otherwise the first use logs a `queue_created` warning).

### Running passes: auto_run, cron, `run --loop`

With `auto_run: true` a pass is requested after `add`, after any job ends, after `release`, `retry` and the cancel of a starting job, so work starts at once. A pass is coalesced: if one is running, the request makes it go around again. Three things can still leave work idle, so you also want a periodic pass: a reboot, a lost shim, and automatic-retry delays.

Cron:

```sh
jobsctl cron install --dry-run     # prints the block, writes nothing
jobsctl cron install
jobsctl cron uninstall
```

`cron install` adds a two-line block to your crontab:

```text
# jobsctl managed
* * * * * PATH='...' XDG_CONFIG_HOME='...' XDG_STATE_HOME='...' '/path/to/jobsctl' run --quiet
```

The entry runs one `jobsctl run` pass every minute. It uses the absolute path of `jobsctl`, an explicit `PATH` (your current `PATH` with `~/.local/bin` added and non-absolute, missing or temporary entries dropped, with a warning for each), and the `XDG_CONFIG_HOME` / `XDG_STATE_HOME` set when you installed, because cron's environment is minimal and its `python3` may be too old. The entry carries no `--config`, so cron uses the default config. Everything else in your crontab is kept byte for byte, and installing twice changes nothing. A pass reconciles too, so there is no second entry. If you move or reinstall jobsctl (for example `uv tool upgrade`), run `cron install` again to refresh the path. Cron only runs if your distribution runs the `cron` service (on WSL, start it). Details: [`cron install`](docs/interface.md#cron-install--cron-uninstall).

`jobsctl run --loop [--interval 10s]` is the foreground alternative (a service manager, a tmux pane): one pass every `run_interval` until SIGTERM or SIGINT. If jobsctl is upgraded underneath it, the loop re-executes itself with the new code between passes, keeping its PID and lease, so there is never a second loop ([Upgrades](docs/interface.md#upgrades)). Only one pass runs at a time; a second `jobsctl run` while one is running exits 0 with `already_running: true`.

### Output, timeouts and kill grace

Each attempt keeps a masked, sanitized tail (default last 16 KiB or 200 lines, whichever is smaller; written to the database every `tail_flush_interval`). Full output is not stored; have the job write its own log file if you need it. A queue or job timeout (`timeout`, `add --timeout DURATION|none`) starts counting when the job enters `running` and ends it `failed` (`timeout`) after the signal sequence. `kill_grace` (default 30 s) is the wait per signal step ([Timeouts](docs/interface.md#timeouts), [Signal sequence](docs/interface.md#signal-sequence)).

### Hooks and the pre-start gate

Hooks are lists under `hooks.<name>` in config; names are `pre-start`, `post-start`, `post-finish`, `post-fail`, `post-held` and `post-cancel`:

```yaml
hooks:
  pre-start:
    - name: gate
      cmd: "gatectl check start-work"   # any command; run via /bin/sh -c
      timeout: 10s
      queues: [builds]                   # null/absent: every queue
  post-fail:
    - name: notify
      cmd: "notice-route send --stdin-json"
      async: true
      exclude_queues: [respond]
```

A hook gets one JSON payload on stdin and runs with the canonical environment plus `env_file` values and `JOBSCTL_HOOK`, `JOBSCTL_ID`, `JOBSCTL_OBJECT_TYPE`. Post hooks are delivered at least once and in order per job; a failing post hook is logged and not retried. Hook output is not stored; only the stderr of a vetoing or escalating `pre-start` hook is kept on the attempt. Treat `output_tail` as untrusted (`include_output: true` is opt-in).

The **pre-start gate** runs in the shim, after the claim and before the job starts. In order, per hook:

| Hook outcome | Effect on the job |
|---|---|
| exit 0 | continue; the next hook runs |
| exit 13 | escalate: the job becomes `held` with reason `escalated` (the remaining hooks still run) |
| any other non-zero exit | veto: the job is `canceled` with reason `vetoed`; no more hooks run |
| timeout, killed by a signal, or cannot start | fails closed, as a veto |

So a pre-start hook's `timeout` matters: a slow policy check that times out cancels the job. The default is 30 s. A veto is not an exit code of any jobsctl command (jobsctl never exits 11); it shows as the job's final status.

```text
$ jobsctl show jb-6aw43ps4
jb-6aw43ps4 canceled(vetoed) queue=gated -- echo hi
...
attempt 1: canceled(vetoed) claimed 2026-11-16T14:41:30.238Z
  hook gate veto: denied by policy\n
```

**Escalation override.** An escalated job waits for a person. `release` alone is refused; only `release --override-escalation` gets it past, and that flag is human-only (a TTY, or `--actor human:<user>`). The override applies to the hooks that escalated that attempt: on the next start they are still run, but their exit 13 counts as allow. Every other hook runs normally, and a veto still vetoes. It is audited (`release_override`).

```text
$ jobsctl release jb-iv3guk5r
jobsctl: error: jb-iv3guk5r was escalated by a pre-start hook; release it with --override-escalation
[exit 20]
$ jobsctl release jb-iv3guk5r --override-escalation
jobsctl: error: --override-escalation needs a human: run it from a terminal or pass --actor human:<user>
[exit 13]
$ jobsctl release jb-iv3guk5r --override-escalation --actor human:ops
jb-iv3guk5r: released
```

Or end it instead: `resolve --as canceled` / `cancel`. Details: [Hooks](docs/interface.md#hooks), [Hold reasons](docs/interface.md#hold-reasons).

### Secrets

The rule for everyone: a secret never goes in argv, `--env`, a label, the queue, the unique key, the cwd, the actor, a decision id, or a variable name. Put it in the **`env_file`** and give the job the name with `--pass-env`:

```yaml
# config.yaml
env_file: ~/.config/jobsctl/env
mask_env: [DEPLOY_KEY]      # extra names to mask (values come from the environment)
```

```sh
# ~/.config/jobsctl/env: NAME=value lines, mode 0600, secrets only
API_TOKEN=...
```

```sh
jobsctl add --pass-env API_TOKEN -- sh -c 'echo token is $API_TOKEN'
jobsctl show jb-zc5g
```

```text
output:
  | token is ***
```

- `--pass-env` values are read from `env_file` when the job starts and never stored. Hooks also get the `env_file` values; jobs get only the names they ask for.
- Every value in `env_file` and `mask_env` is masked as `***` in output tails and hook output, and `add` refuses it where it would be stored or logged:

```text
$ jobsctl add -- curl -H <secret> https://example.invalid
jobsctl: error: argv element 2 contains a secret value; pass secrets in a file the command reads
[exit 2]
$ jobsctl --json add --env API_TOKEN=x -- true
{"error": {"code": "usage", "message": "--env API_TOKEN is a secret variable; use --pass-env API_TOKEN", "details": {"code": "secret_in_env", "variable": "API_TOKEN"}}}
[exit 2]
$ jobsctl add --label note=<secret> -- true
jobsctl: error: the value of --label note contains a secret value
[exit 2]
```

- **`env_file` must be trustworthy.** It is a trust anchor: whoever can write it runs code as you. jobsctl uses it only if it is a regular file of at most 1 MiB, owned by you, with no group or other permissions, and every parent directory is owned by you or root and not writable by group or others. Otherwise it is treated as unavailable: jobs with `--pass-env` stay queued with `blocked_by: env_file_unavailable`, `jobsctl run --json` reports `env_file_unsafe` in `warnings`, and **the secret refusals above stop working** (jobsctl cannot know the values; only `mask_env` values are still refused). This also means a file under `/tmp` never qualifies (the sticky world-writable `/tmp` fails the ancestor check). Keep it at `~/.config/jobsctl/env` or another directory no agent session can write.
- Masking is best-effort: values shorter than 8 characters, encoded forms (base64, hex), slices and secrets a job obtains itself are not masked. Treat tails as untrusted. See [Masking and sanitizing](docs/interface.md#masking-and-sanitizing) and [Residual risks](docs/interface.md#residual-risks).
- jobsctl never logs argv, `--pass-env` or `env_file` values, or output tails; `export --log` and `export --audit` are safe to read, but not a place to look up what a job printed.

### Retention, pruning and ids

Each pass deletes terminal jobs whose end is older than `retention` (default `30d`; `null` keeps them) and, whatever `retention` says, diagnostics files older than 7 days. Jobs with undelivered hook events are never deleted. Prune by hand with `jobsctl prune --older-than 7d [--dry-run]`. Pruned ids are never reused. Audit records are kept forever; jobsctl does not rotate `audit.jsonl` or `log.jsonl` ([`prune`](docs/interface.md#prune), [Audit and log](docs/interface.md#audit-and-log)).

### State, logs and audit

State lives under `~/.local/state/jobsctl/` (respecting `XDG_STATE_HOME`; mode 0700):

| Path | What |
|---|---|
| `state.db` | the queue (private, 0600) |
| `audit.jsonl` | one record per human-relevant decision: `cancel --force`, `cancel` of a held job, `release --override-escalation`, `resolve`, `retry`, `prune` |
| `log.jsonl` | every transition, reorder and label change, warnings and hook runs |
| `shims/<id>.log` | the shim's own diagnostics for a job (never job output); removed with the job |
| `diag/<kind>-<pid>.log` | diagnostics of passes and other internal processes; deleted after 7 days |

```sh
jobsctl export --audit                 # JSON lines
jobsctl export --log --since 10m
jobsctl export --log --after-seq 120   # incremental reads
```

```text
{"seq":8,"ts":"2026-11-16T14:42:12.192Z","actor":"human:ops","decision_id":["d-2"],"target":"jb-xhnk3ecp","action":"cancel_force","inputs":{...},"result":"running"}
```

Actors are claims, not proof: with no TTY and no `--actor`, a command's actor is `auto:unknown`. The human gate is advisory ([Residual risks](docs/interface.md#residual-risks)).

### Upgrades

Database migrations are additive, so an old and a new jobsctl can share a database: any normal command migrates it. Running shims keep their code and finish their jobs. Long-lived `run --loop` re-executes itself; `wait` and a waiting `cancel` exit 12 `version_skew` (run them again). Re-run `cron install` if the path of `jobsctl` changed. A future major version that cannot migrate additively ships a drain procedure in its release notes ([Upgrades](docs/interface.md#upgrades)).

### Troubleshooting

Start with `jobsctl list --status starting --status running --status held`, `jobsctl show <id>`, `jobsctl run --json` (look at `warnings`) and `jobsctl reconcile --dry-run --json`.

- **Nothing starts.** Check `jobsctl capacity` (is the queue full or `cap: 0`?) and `show --json` `blocked_by` for the queued job: `not_before` (waiting for a retry delay), `env_file_unavailable`, `queue_paused`, `global_paused`. With `auto_run: false` or `--no-start`, run `jobsctl run`. If `run` says `already_running`, another pass holds the lease.
- **Stuck in `starting`.** A job is `starting` while its shim runs the pre-start hooks, so a slow hook keeps it there up to its timeout. If the shim died, the next pass fixes it: a shim that never confirmed itself is requeued (`claim_orphaned`), a confirmed one that died ends the job `failed` (`lost`). Run `jobsctl reconcile` to apply this at once; read `shims/<id>.log` in the state directory for the shim's diagnostics.
- **`shim_lost`.** The shim died but the job process is alive: the job stays `running` with `shim_lost: true`, keeps its slot, its timeout is no longer enforced, and jobsctl never kills it by itself. A human can `cancel --force` it (a `_kill` process runs the signal sequence).
- **`lost` after a reboot.** A new boot id means every job that was running is gone; the next pass marks it `failed` (`lost`), subject to automatic retry.
- **`version_skew`.** jobsctl was upgraded while `wait` or `cancel` ran. Run the command again.
- **`env_file_unsafe`.** See [Secrets](#secrets): check owner, mode and parent directories.
- **A veto or escalation you did not expect.** `jobsctl show <id>` prints the hook's message under the attempt; `export --log` has a `hook_ran` record for each hook run.
- **Exit 1 `busy`.** The database was locked longer than `busy_timeout`; the next pass retries.
- **Cron does nothing.** Check that the cron service runs and that the installed block is intact (`crontab -l`); reinstall with `cron install`.

## Live tests

The test suite runs without touching your system (`make verify`). The one exception is the opt-in live cron test, which exercises a real cron daemon and therefore your **real crontab**: it refuses to run if a jobsctl block already exists, saves the crontab bytes first and restores them afterwards. Run it only deliberately:

```sh
JOBSCTL_LIVE_CRON=1 uv run pytest -m live tests/test_cron_live.py
```

Never run `cron install` against your real crontab to experiment; use `cron install --dry-run`, or put a fake `crontab` program first on `PATH`.

## Layer and dependencies

L1 (primitive), fully standalone; it does not interpret what jobs do and knows nothing about its callers. Python packages: `pyyaml` and `pydantic`; `textual` only in the future `tui` extra. Platform and requirement notes: [Platform notes](docs/interface.md#platform-notes).

## Documentation

- [Interface](docs/interface.md) (normative), [design](docs/design.md), [plan](docs/plan.md)
- [config.example.yaml](config.example.yaml), [schemas/](schemas)
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT, see LICENSE.
