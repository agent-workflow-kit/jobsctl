# jobsctl

Generic job queue with concurrency caps, ordering and held jobs.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive). Works fully standalone.

## What it does

Stores jobs as argv arrays (no shell) in named queues with priorities, pin/move/reorder, per-queue caps and an optional `global_cap` (used for "max parallel agents"). It does not interpret what jobs do. Jobs run detached under a per-job shim; `run` starts jobs up to the caps and reaps finished shims, as a cron pass or a loop. `add --unique-key` deduplicates against non-terminal jobs. Job exit 0 → done, 13 → held (slot freed), anything else → failed. Held jobs are closed with `resolve`, or (pre-start escalations only) requeued with `release --override-escalation`. Stores exit status and a masked output tail per job.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md):

```text
jobsctl add [--queue Q] [--unique-key K] [--env VAR=value] [--pass-env VAR] [--label k=v] -- <argv...>
jobsctl list | show <id> | move | pin | priority
jobsctl cancel [--force] | retry | hold | release [--override-escalation] | resolve
jobsctl capacity [--queue Q] --json
jobsctl reconcile
jobsctl run            # runner
jobsctl browse         # TUI
```

## Dependencies

- None (Uses: none in the architecture).

## Install

```text
uv tool install git+https://github.com/agent-workflow-kit/jobsctl
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
