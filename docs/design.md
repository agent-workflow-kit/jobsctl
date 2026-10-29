# jobsctl: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Generic job queue with concurrency caps, ordering and held jobs. Layer: L1 (primitive). Works fully standalone.

## Responsibilities

From architecture §5.1 (verbatim):

Generic job queue. Owns the concurrency caps (used for "max parallel agents") and the order of work already enqueued (QR-9); it does not interpret what jobs do.

- Commands: `add [--queue Q] [--unique-key K] [--env VAR=value] [--pass-env VAR] [--label k=v] -- <argv...>`, `list`, `show`, `move`, `pin`, `priority`, `cancel [--force]`, `retry`, `hold`, `release [--override-escalation]`, `resolve`, `capacity [--queue Q] --json`, `reconcile`, `run` (runner), `browse`.
- Uses: none.
- Notes:
  - Jobs are stored as argv arrays (no shell). A job's argv never contains untrusted text; submitters pass such text in files. Jobs run with the runner's environment plus `add --env VAR=value` (non-secret values only; they are stored) and `add --pass-env VAR` (the value is taken by name from the runner's environment when the job starts and is never stored). Each job gets `JOBSCTL_JOB_ID`.
  - Named queues with priorities, pin/move/reorder, per-queue caps plus an optional `global_cap`. The workflow tools submit to the queues `tickets`, `reviews` and `respond` by default (configurable in each submitting tool); `global_cap` bounds the total number of parallel agents. `capacity` reports free slots (cap − running − queued).
  - `add --unique-key <key>` is a no-op (exit 0, returning the existing id) if a non-terminal job has that key; held jobs are non-terminal. Callers use keys such as `review:<pr>@<sha>`, `respond:<pr>`, `ticket:<KEY>`.
  - Statuses: `queued`, `running`, `held`, `done`, `failed`, `canceled`. Job exit → status: 0 → done; 13 → **held** (its slot is freed); any other exit → failed. A `pre-start` hook veto (any non-zero exit other than 13) → canceled; a `pre-start` hook escalation (exit 13) → held. No automatic retry by default.
  - **Held jobs.** `resolve <id>` closes a held job. `release <id> --override-escalation` requeues a job held by a `pre-start` escalation and re-runs `pre-start` with the escalating hook's verdict overridden. A job held because its own command exited 13 (e.g. an agent that needs a human) is never released: its submitter resolves it and continues through `agent-spawn resume` (section 9.3). Job commands that launch agents are therefore resume-aware: they look for an existing agent by its `job=` or correlation labels and resume it instead of launching a second one.
  - **Cancel.** `cancel` of a queued or held job closes it as canceled. `cancel` of a running job is refused (exit 20, `reason: live`) unless a human passes `--force`, because a running job may host a live interactive agent; with `--force` the shim sends SIGTERM to the job's exact PID, which every layer below forwards (section 9.2).
  - Stores exit status and the tail of each job's stdout/stderr (`show <id> --json`). Tails mask the values of the env vars listed in config `mask_env` and are documented as untrusted.
  - `reconcile` detects dead job shims (PID plus process start time) and marks their jobs failed with reason `lost`, firing `post-fail`.
  - Hooks: `pre-start`, `post-start`, `post-finish`, `post-fail`, `post-held`. Supports labels; submitters label jobs with the same correlation labels they give the agent.

## Uses

- None.

## Relevant architecture sections

Read these sections of `../awkit-docs/architecture.md` before designing:

- §3.4: cross-cutting principles (hooks, labels, never destroy work)
- §6: conventions summary
- §9.2: launch, hosting, waiting and jobs (cancellation chain, one submitting layer)
- §9.3: supervision and post-exit policies (when a job ends held)
- §12.4: confirmations, holds and `--yes` (held jobs)
- §13.2: prompt injection: job argv never carries untrusted text
- §13.3: credentials (`mask_env`, `--pass-env`)
- §20: this tool's constraints and open topics

## Constraints (decided)

From architecture §20:

- Jobs run detached under a per-job shim that records exit status and output tail.
- `jobsctl run` only starts jobs up to the caps and reaps finished shims; it can run as a cron pass or as a loop.
- Output tails are masked per `mask_env`.

## Open topics

From architecture §20:

- queue semantics details
- cap configuration
- output tail size

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What are the exact queue semantics: ordering within a queue (priority, pin, move), fairness across queues, and how `retry`, `hold` and `cancel` interact with order?
2. How are caps configured (per-queue caps, `global_cap`, defaults, per-queue creation), and how does `capacity` count held and queued jobs?
3. How large is the stored output tail (lines/bytes, stdout vs stderr), and is it configurable?
