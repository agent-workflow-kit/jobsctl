# jobsctl

Generic job queue with concurrency caps, ordering and held jobs.

## Status

Designed: interface 0.1.0-design ([docs/interface.md](docs/interface.md)); not yet implemented.

## Layer

L1 (primitive). Fully standalone; it does not interpret what jobs do and knows nothing about its callers.

## What it does

- `jobsctl add -- <argv...>` stores a job (an argv array, never a shell string) in a named queue and prints its id (`jb-…`; the only stable human output). Queues are created on first use with `default_cap`; `--unique-key` returns the existing non-terminal job instead of adding a duplicate ([Unique keys](docs/interface.md#unique-keys)).
- Jobs are ordered by pin, priority and position; `move`, `pin` and `priority` reorder them ([Order](docs/interface.md#order)). Per-queue caps and an optional `global_cap` bound how many run at once; `capacity` reports free slots ([Capacity](docs/interface.md#capacity)).
- `jobsctl run` is one pass: it reconciles, claims jobs up to the caps and starts each under a detached per-job shim, so jobs outlive the command that started them. Passes are triggered automatically after changes and by cron or `run --loop` ([Runner](docs/interface.md#runner)).
- The shim runs `pre-start` hooks, builds an allowlisted environment, starts the job in its own process group, keeps a masked, sanitized output tail and records the exit. Exit 0 → `done`, 13 → `held`, anything else → `failed` ([Status transitions](docs/interface.md#status-transitions)).
- Held jobs are requeued with `release` (pre-start escalations only with `--override-escalation`) or closed for good with `resolve`; `retry` requeues a terminal job, or one held because its command exited 13, as a new attempt with the same id ([Hold reasons](docs/interface.md#hold-reasons)). Optional automatic retries keep a failed job `queued` until its retry delay passes ([Automatic retry](docs/interface.md#automatic-retry)).

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/jobsctl
# with the TUI (jobsctl browse):
uv tool install 'jobsctl[tui] @ git+https://github.com/agent-workflow-kit/jobsctl'
```

For cron, run `jobsctl cron install`: it writes the absolute path of `jobsctl`, an explicit `PATH` and the `XDG_*` directories set at install, because cron's environment is minimal and its `python3` may be too old.

## Quick start

```sh
jobsctl add --queue builds --label kind=build -- make test   # prints the job id, e.g. jb-k3m2q7xa
jobsctl list
jobsctl show jb-k3m2 --json         # any unique prefix of 4+ characters
jobsctl wait jb-k3m2                # exits 0 done, 12 failed, 13 held, 20 canceled
jobsctl capacity --queue builds --json
jobsctl cron install                # safety-net pass every minute
```

## Commands

| Command | Purpose |
|---|---|
| `add [options] -- <argv...>` | enqueue a job ([`add`](docs/interface.md#add)) |
| `list`, `show`, `wait` | inspect jobs; `wait` blocks until the job is terminal or held |
| `move`, `pin`, `unpin`, `priority` | reorder queued (and held) jobs |
| `hold`, `release`, `resolve` | pause queued jobs, requeue or close held jobs |
| `cancel`, `retry` | cancel jobs (`--force` for running jobs, human-only); requeue a terminal job or an exit-13 hold |
| `label add`, `label remove` | change a job's labels |
| `capacity`, `queues` | free slots; every known queue with its effective settings |
| `run`, `reconcile`, `prune` | one pass (or `--loop`); recovery without claiming; delete old terminal jobs |
| `export` | print `audit.jsonl` or `log.jsonl` records |
| `cron install`, `cron uninstall` | manage the crontab entry for `run` |
| `browse` | TUI (needs the `tui` extra) |

Every mutating command accepts `--actor`, a repeatable `--decision-id`, `--dry-run` and `--yes` (a no-op: nothing asks for confirmation). Global flags: `--json`, `--config PATH`, `--verbose`, `--quiet`, `--version`. `--json` output follows [schemas/cli/](schemas/cli). Full rules: [Commands](docs/interface.md#commands).

## Exit codes

0 success (including no-ops); 1 internal error or database busy; 2 usage, invalid config or missing TUI extra; 10 not found or unknown queue (`strict_queues`); 12 `wait`: job failed, `cancel --force`: still pending, or `wait`, `cancel`, `browse`: jobsctl was upgraded underneath it (`version_skew`, run it again); 13 needs a human, or `wait`: job held; 20 refused (with a reason), or `wait`: job canceled; 21 ambiguous id or selector; 22 `wait` timed out; 130 `wait` or `add --wait` interrupted, or a command interrupted while it ran a hook (`interrupted`). jobsctl never exits 11. Details: [Exit codes](docs/interface.md#exit-codes), [Errors](docs/interface.md#errors).

## Hooks

`pre-start` (veto cancels the job, exit 13 holds it), `post-start`, `post-finish`, `post-fail`, `post-held` and `post-cancel`, configured under `hooks.<name>` and run by the shim (or the command that made the transition; `async: true` hooks by a background delivery process, at most 8 at once), at least once and in order per job. Payloads are in [schemas/hooks/](schemas/hooks); a hook gets the output tail only with `include_output: true`, and tails are untrusted. Hook output is not stored or logged; only a vetoing or escalating `pre-start` hook's stderr is kept on the attempt. Details: [Hooks](docs/interface.md#hooks).

## Config and environment

- Config: `~/.config/jobsctl/config.yaml` plus `config.d/*.yaml`; every key with its default in [config.example.yaml](config.example.yaml); schema [schemas/config.json](schemas/config.json); rules in [Config](docs/interface.md#config).
- Config comes only from files; there are no `JOBSCTL_<KEY>` variables. `--config PATH` is passed on to the passes and internal processes a command starts, but the state directory is shared: for a separate queue system, also set `XDG_STATE_HOME`.
- Passes, shims and hooks rebuild one canonical environment from an allowlist (`env.inherit`) and fixed config values; hooks also get the optional owner-only `env_file` ([Canonical environment](docs/interface.md#canonical-environment)). Inherited values such as `PATH` come from whatever started the chain (cron or your shell). Set `env.set.PATH` (recommended): it then replaces the inherited `PATH` for jobs, hooks and jobsctl itself, so a `PATH` entry from an agent's worktree (e.g. `node_modules/.bin`) cannot reach your hooks.
- `env_file` holds secrets only: every value in it is masked and refused in argv and `--env`; put non-secret fixed values in `env.set`. Its values go only to hooks and, through `--pass-env`, to jobs, never into jobsctl's own environment. It is a trust anchor (whoever can write it runs code as you): keep it at `~/.config/jobsctl/env` or another directory no agent session can write; jobsctl checks its owner and mode and those of every parent directory.
- A job gets `JOBSCTL_JOB_ID` (the same across attempts) and `JOBSCTL_ATTEMPT`; its environment is built from the allowlist, not inherited wholesale. Pass secrets with `--pass-env`, whose values come only from `env_file` (read at start, never stored), never with `--env` or in argv: `add` refuses known secret values there ([Job environment](docs/interface.md#job-environment)).
- Hooks get `JOBSCTL_HOOK`, `JOBSCTL_ID` and `JOBSCTL_OBJECT_TYPE`.
- State, audit and log: `~/.local/state/jobsctl/` ([Audit and log](docs/interface.md#audit-and-log)).

## Dependencies

- None in the architecture (Uses: none).
- Python packages: `pyyaml`, `pydantic`; `textual` only in the optional `tui` extra.

## Platforms

Linux and WSL (shutting down the WSL VM ends every job; the next pass marks them `lost`; a shutdown or logout that signals the shims ends their jobs `terminated`). macOS best-effort (process identity through `libproc`; no subreaper). Python 3.12 or newer, SQLite 3.35 or newer. Cron needs the absolute path that `cron install` writes. Details: [Platform notes](docs/interface.md#platform-notes).

## Documentation

- [Interface](docs/interface.md) (normative), [design](docs/design.md)
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
