# PKM Brain Documentation

**Status:** authoritative docs index
**Last verified:** 2026-07-14 against the current Gmail operational mirror and completed encrypted 90-day Gmail archive; owner content review and promotion remain pending

Current requirements are organized by product feature, not implementation stream. Start with Product Foundation, then read the owning feature spec for the behavior being changed.

## Canonical Specs

| Feature | Authority |
|---|---|
| Product boundaries, persistence, privacy, invariants | [Product Foundation](specs/product-foundation.md) |
| Capture, ingest, extraction, facts, entities, routing, Wiki | [Capture And Knowledge](specs/capture-and-knowledge.md) |
| Search, context packets, embeddings, memory, telemetry, evals | [Retrieval And Memory](specs/retrieval-and-memory.md) |
| Knowledge Curation actions/policy, relations, Queue, autonomy, review volume | [Curation And Review](specs/curation-and-review.md) |
| Operational items, reconciliation, briefings, guarded external execution | [Chief Of Staff Operations](specs/chief-of-staff-operations.md) |
| Daemon, scheduler, native/browser UI, system Ops, Settings, packaging | [App And Operations](specs/app-and-operations.md) |
| Primary/Secondary sync, role mobility, profiles | [Sync And Topology](specs/sync-and-topology.md) |

## Engineering And Operations

- [Architecture Code Guide](architecture-code-guide.md): where behavior lives in Python/Swift.
- [Email Ingestion](email-ingestion-spec.md): the sanitized operational mirror and separate encrypted Gmail history archive.
- [Live Chief-of-Staff Shadow Trial](runbooks/chief-of-staff-shadow-trial.md): authorize the two read-only Google grants, run Today, inspect/label results, and check archive progress.
- [Retrospective Shadow Replay](runbooks/chief-of-staff-shadow-replay.md): score private or synthetic frozen fixtures without provider calls.
- [Sync Acceptance Runbook](runbooks/sync-acceptance.md): real-machine validation.
- [Project Audit - 2026-07-10](audits/project-audit-2026-07-10.md): current risks and evidence.
- [Project Implementation Plan](plans/project-implementation-plan.md): canonical roadmap, prioritized releases, work packages, dependencies, and acceptance hits.
- [Chief-of-Staff Operations Implementation Plan](plans/chief-of-staff-operations-implementation-plan.md): Calendar-first operational rollout, reconciliation gates, and guarded-execution sequence.
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
| Autonomous-wiki Chief-of-Staff, fact review volume, regeneration | Curation And Review (the Knowledge Curation foundation) |
| browser UI v2, macOS app | App And Operations |
| sync spec/plan, topology/role mobility | Sync And Topology |
| July 7 audit | July 10 audit and improvement plan |

The operational Chief-of-Staff mission is now owned by [Chief Of Staff Operations](specs/chief-of-staff-operations.md). Historical `cos_*` names refer to the implemented Knowledge Curation system until an all-or-nothing compatibility migration renames them; they are not the operational item or external-execution model.

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
