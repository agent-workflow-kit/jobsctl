# CLAUDE.md: jobsctl

jobsctl is a awkit L1 primitive: generic job queue with concurrency caps, ordering and held jobs. It must work
fully standalone (no harness, bus, shared library or shared contracts package).

**Naming.** Tools have no shared prefix; see design guide §1 naming rules.

## Read first

- `../awkit-docs/architecture.md`: this tool's §5.1 entry and its §20 entry (constraints and open
  topics), plus the cross-cutting sections listed in `docs/design.md`.
- `../awkit-docs/design-guide.md`: the conventions every awkit tool follows.
- `docs/design.md`: this tool's design doc (decisions and open questions).

## Rules

- Open source: never add confidential information (company names, hostnames, ticket project
  keys, repo or org names, people's names, private skills). Organization specifics belong in
  the user's config.
- Never import sibling repos. Read their docs and schemas by path only; copying a sibling's
  schema files into `tests/` pinned to a version is allowed.
- No upward awareness: this tool never knows about, or behaves differently for, the tools that
  call it.
- Interface first: write `docs/interface.md` and `schemas/` before implementation.
- Semver. A breaking interface change is, before 1.0, a `feat` commit whose title names the break (never `!` before 1.0), and from 1.0 on a `!` commit; the title becomes the changelog line (details go in the PR body). Either way, update `awkit-docs/interface-registry.md`. Never hand-edit CHANGELOG.md or the version.
- Run `make verify` before every commit.
- All subprocess and HTTP calls go through `src/jobsctl/gateway.py`.
- Never run destructive git commands (reset, checkout --, clean, restore, stash drop,
  force-push). Never kill processes by pattern; only signal an exact PID this tool started
  (verified by its recorded identity), the process group the shim created for a job
  (docs/interface.md, Signal sequence), or a hook's process group (docs/interface.md, Hook
  processes).
- Minimize dependencies; justify each one in `docs/design.md`.

## Workflow

1. Start with a design session that resolves the open topics in `docs/design.md` and records
   the decisions there.
2. Write `docs/interface.md` and `schemas/`.
3. Implement test-first.

## Repository workflow

- Work in a git worktree on a branch named `<type>/<short-slug>`, never in the main checkout (design guide section 17).
- PR titles are Conventional Commit headers; they become the squash commit, the version bump and the changelog line (architecture section 18.3). Do not use `!` before 1.0 (a breaking change before then is a `feat` whose title names the break).
- Never use `--no-verify`. Check PR titles, bodies and comments with `guard text` where the private-content guard is installed (architecture section 18.7).
- Never merge a PR without the maintainer's explicit instruction for that PR; merges go through `awkit-ci`'s `scripts/pr-merge` (architecture section 18.6).
- Never hand-edit `CHANGELOG.md` or the version in `pyproject.toml`; the release job owns them.
