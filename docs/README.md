# PKM Brain Documentation

**Status:** authoritative docs index
**Last verified:** 2026-07-11 against public release `0.1.1` code snapshot `b3ba211`

Current requirements are organized by product feature, not implementation stream. Start with Product Foundation, then read the owning feature spec for the behavior being changed.

## Canonical Specs

| Feature | Authority |
|---|---|
| Product boundaries, persistence, privacy, invariants | [Product Foundation](specs/product-foundation.md) |
| Capture, ingest, extraction, facts, entities, routing, Wiki | [Capture And Knowledge](specs/capture-and-knowledge.md) |
| Search, context packets, embeddings, memory, telemetry, evals | [Retrieval And Memory](specs/retrieval-and-memory.md) |
| CoS actions/policy, relations, Queue, autonomy, review volume | [Curation And Review](specs/curation-and-review.md) |
| Daemon, scheduler, native/browser UI, Settings, packaging | [App And Operations](specs/app-and-operations.md) |
| Primary/Secondary sync, role mobility, profiles | [Sync And Topology](specs/sync-and-topology.md) |

## Engineering And Operations

- [Architecture Code Guide](architecture-code-guide.md): where behavior lives in Python/Swift.
- [Sync Acceptance Runbook](runbooks/sync-acceptance.md): real-machine validation.
- [Project Audit - 2026-07-10](audits/project-audit-2026-07-10.md): current risks and evidence.
- [Project Implementation Plan](plans/project-implementation-plan.md): canonical roadmap, prioritized releases, work packages, dependencies, and acceptance hits.
- [Project Improvement Plan](plans/project-improvement-plan.md): compatibility pointer retained for old links.

## History

- [Implementation Stream History](archive/implementation-stream-history.md): compact timeline and durable outcomes.
- Old top-level spec/plan paths remain as compatibility pointers so external links do not break.
- Full pre-consolidation phase logs remain in git history.

Consolidation map:

| Former stream documents | Current authority |
|---|---|
| V0.1 broad product spec | Product Foundation plus Capture/Retrieval |
| extraction payload, entity layer, email | Capture And Knowledge |
| retrieval contract, embeddings | Retrieval And Memory |
| Chief-of-Staff, fact review volume, regeneration | Curation And Review |
| browser UI v2, macOS app | App And Operations |
| sync spec/plan, topology/role mobility | Sync And Topology |
| July 7 audit | July 10 audit and improvement plan |

## Ownership Rule

Update one owning feature spec when behavior changes:

- current behavior and invariants stay in the feature spec;
- code navigation stays in the architecture guide;
- operational steps stay in runbooks;
- measured problems stay in audits;
- unimplemented sequencing stays in plans;
- completed phase chronology moves to the history summary or git history.

Do not append a new top-level implementation-stream spec when an existing feature spec owns the behavior.

## Verification Stamp

Current-state docs must include:

```text
**Status:** ...
**Last verified:** YYYY-MM-DD against commit <hash>
```

If the claim includes uncommitted behavior, say so explicitly. Prefer stable symbol names over line-only references. Re-stamp a materially changed current-state doc after checking the owning code/tests.
