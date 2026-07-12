# Contributing

PKM Brain is a local-first Python project. Contributions should preserve the separation between source code and private runtime data: source lives in this repo, while knowledge workspaces live outside git, normally under `~/brain`.

## Development Setup

```bash
uv sync --dev
uv run pytest
uv run ruff check .
```

## Contribution Guidelines

- Keep changes scoped to the behavior being improved.
- Add or update tests when changing ingestion, retrieval, sync, memory, wiki, MCP, or scheduler behavior.
- Do not commit runtime data, SQLite databases, LanceDB indexes, captured logs, local config, or secrets.
- Prefer deterministic behavior for core capture, ingest, search, retrieval, and sync paths.
- Keep LLM-backed behavior explicit and review-gated where it can alter durable knowledge.

## Docs Conventions

- Living architecture/spec docs should state their status and include `**Last verified:** <date> against commit <hash>` when they make current-state claims.
- Feature authority lives in the six specs indexed by `docs/README.md`. Update the owning feature spec instead of adding a new top-level implementation-stream spec.
- Historical plans should be marked historical near the top, summarized under `docs/archive/` when useful, and point to the current living spec that supersedes them.
- Current-state sections should describe the code that exists now. Future work belongs in explicit TODO, build-plan, or historical-plan sections.
- Audits record evidence and findings. Unimplemented sequencing belongs in `docs/plans/`. Operational procedures belong in `docs/runbooks/`.
- When code behavior changes, update the nearest living spec in the same change or record why no doc update is needed.
- Prefer stable symbol names over line-only references. If a doc describes uncommitted behavior, say so explicitly and re-stamp it after commit.

## Pull Requests

Before opening a pull request, run:

```bash
uv run ruff check .
uv run pytest
```

Include the motivation, behavioral impact, and any migration or privacy considerations in the PR description.
