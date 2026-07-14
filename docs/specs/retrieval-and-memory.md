# Retrieval And Memory

**Status:** canonical living feature spec
**Last verified:** 2026-07-13 against the current knowledge retrieval implementation; operational retrieval below is the additive target contract
**Owns:** knowledge search, operational retrieval, context packets, ranking/calibration, embeddings, retrieval telemetry, evals, and reviewed memories

## Retrieval Contract

Knowledge and operational retrieval are separate typed paths over related evidence. They may appear together in a user-facing briefing, but their ranks, trust, lifecycle, and telemetry are never flattened into one undifferentiated result list.

Every knowledge context retrieval returns an explicit verdict:

- `found`: strong evidence matches the task;
- `partial`: useful but incomplete or weakly scoped evidence exists;
- `no_strong_match`: Brain should not imply that it knows the answer.

The packet keeps evidence layers distinct:

- active reviewed memories;
- proposed/candidate memories, labeled lower trust;
- active facts;
- managed wiki pages;
- supporting source chunks;
- retrieval reasons, scores, and debug details when requested.

Negative results must remain honest: a `no_strong_match` packet may expose diagnostics, but it must not present unrelated facts as an answer.

The existing `search_knowledge` and `retrieve_context` behavior remains the Knowledge Curation contract. Operational sections are introduced through an explicit operational query/briefing surface; they do not silently change existing MCP packet semantics.

## Search Stack

The current hybrid path combines:

1. SQLite FTS5/BM25 over chunks and knowledge surfaces;
2. LanceDB vectors over chunks;
3. layer-specific score floors, source weights, and managed-page boosts;
4. reciprocal/fused ranking and packet budgets;
5. provenance-aware chunk suppression where a selected fact already exposes the same evidence.

Source-aware penalties keep agent-session traces from dominating ordinary knowledge queries. Agent-intent queries may relax those penalties.

Facts, pages, memories, and chunks retain separate caps and trust treatment. A high lexical score in one noisy layer is not sufficient by itself to claim `found`.

## Operational Retrieval

Operational retrieval reads canonical item state and event history from `ops.sqlite`, then resolves cited source or knowledge IDs through the service layer. It does not copy source bodies into the operational database, attach the two SQLite files in UI code, or treat semantic similarity as proof that an item is still active.

An operational result carries enough state to evaluate current relevance:

- stable item ID and type;
- current state and most recent transition;
- `starts_at`, `due_at`, `expires_at`, and operator timezone where applicable;
- `last_observed_at`, last reconciled source change, and staleness classification;
- confidence and whether a proposed cross-thread link remains ambiguous;
- stable evidence references and any related fact, entity, project, or goal references;
- why the item was included, suppressed, updated, or considered stale.

Operational selection is freshness-first:

1. Filter by legal active states and the requested time horizon.
2. Apply `resolved|cancelled|expired` lifecycle transitions plus recurrence and reschedule changes before ranking.
3. Prefer explicit due/start time, recent source change, waiting duration, severity, and operator feedback over text similarity.
4. Use semantic relevance only after current-state and freshness gates.
5. Suppress terminal items and reconciled duplicates from active briefing sections; route stale active items to an explicit needs-reconciliation section rather than present them as fresh.

A briefing must never present an old active detection as current merely because it ranks well lexically. Low-confidence ambiguity remains labeled and may be shown as a possible attention item; it is not silently adjudicated by the Knowledge Curation critic or resolver.

The two evidence flows can inform one another only through explicit references:

- Operational Chief-of-Staff detection may cite raw documents/chunks and may read facts/pages as background.
- Facts and pages never become an operational commitment, deadline, or completion signal by themselves.
- Operational observations and feedback never become durable facts without re-entering the Knowledge Curation proposal and ledger path.
- Operational exposure, urgency, dismissal, or completion never changes fact confidence, knowledge popularity, or source authority.

V1 briefing retrieval is read-only and primary-local. A secondary may not reconcile or mutate operational items; absent an approved read-only operational replica, it reports operational context unavailable rather than serving a locally divergent ledger.

The owning item, reconciliation, and Today presentation contract is [Chief-of-Staff Operations](chief-of-staff-operations.md). This spec owns how those items are selected and exposed without weakening knowledge retrieval semantics.

## Embedding Provider

Provider selection is resolved once per Brain home:

```yaml
embedding:
  provider: hash
  model: BAAI/bge-small-en-v1.5
  query_instruction: ""
```

Precedence is environment, then `config/local/config.yaml`, then the deterministic `hash` default.

Rules:

- hash remains the code/test default and requires no optional runtime;
- sentence-transformer support is installed through the `embeddings` extra;
- ingest and query paths share one provider identity;
- query embedding may apply a provider-specific instruction while passage embedding does not;
- model download is explicit and never initiated by a scheduled job;
- provider load failure degrades to FTS-only with a visible reason;
- a configured sentence-transformer is never silently replaced by hash.

The verified live home uses `sentence-transformer:BAAI/bge-small-en-v1.5`. The July 5 eval retained a 1.0 negative-control pass rate, improved source-hit from 0.825 to 0.877, and improved the seeded semantic probes from 1/5 to 5/5 vector hits.

## Vector-Space Integrity

One index contains exactly one vector space. A sidecar stamp records provider, model, dimension, and build time.

Every vector write verifies active config against the stamp. A mismatch blocks writes. A mismatch or unavailable provider disables vector reads for that request and reports the reason while preserving BM25 retrieval.

Provider changes require a full rebuild. Missing rows under the same stamp may be backfilled. Indexes are node-local derived artifacts and are not synced.

The current vector collection contains chunks only. Fact vectors, if added, must be a separately stamped collection rather than reviving the removed mixed helper path.

## Selection And Calibration

Ranking follows these principles:

- active reviewed memory, active source-backed facts, and managed pages outrank raw traces at comparable evidence strength;
- facts below their relevance floor are absent, not fixed top-k filler;
- source-type caps limit repetitive session traces;
- broad/noisy result sets lower confidence;
- source hit includes evidence reachable through selected fact/page provenance, not only a duplicate returned chunk;
- superseded facts are not authoritative results;
- conflicted facts remain visibly contested;
- selection reasons must be sufficient to answer "why is this here?"

The retrieval eval is the ratchet. Fixtures include positive source/page cases, paraphrase probes, and synthetic negative controls whose exact text is prevented from becoming indexed evidence through captured eval logs.

## Retrieval Popularity

Popularity is an impact signal for review ordering:

- fact popularity is `COUNT(DISTINCT retrieval_event_id)` over fact exposure lineage;
- entity popularity is the distinct union of exposure events across linked facts;
- Queue popularity is the distinct union for every referenced fact/entity.

Popularity may sort entities, facts, and review items before pagination. It must never:

- increase truth or extraction confidence;
- make a fact authoritative;
- change a policy decision;
- be counted more than once for the same retrieval event.

The native UI displays the count and last exposure time where available.

Popularity counts production retrieval use, not evaluation activity. Retrieval evals and sync acceptance probes call retrieval with telemetry disabled, and automated tests run against isolated temporary homes unless a test explicitly exercises telemetry. New eval or acceptance flows must preserve that boundary.

Operational retrieval records its own briefing/item exposure and correction signals in `ops.sqlite`. Those events support operational trust metrics such as duplicate-active rate, stale-active rate, false closure, briefing churn, and missed-item feedback. They do not contribute to knowledge popularity or retrieval-event lineage in `brain.sqlite` unless a separate knowledge retrieval actually occurred.

Legacy retrieval evals were not labeled separately and can be identified only by the exact golden queries they executed under caller `retrieve_context`. `brain eval purge-retrieval-telemetry` is dry-run by default. Apply mode deletes only lineage attached to those exact legacy events and relabels the retained retrieval rows as `eval:retrieval_legacy`, preserving audit history while removing their popularity contribution. Real retrievals with other queries and their lineage remain untouched; rerunning the cleanup is idempotent.

## Telemetry And Retention

Retrieval events, context lineage, and snapshots make selection auditable. They are operational telemetry, not durable knowledge.

Compaction must preserve:

- recent detailed events needed for debugging;
- lineage required by active snapshots, feedback, or audit;
- aggregate exposure counts needed for popularity;
- enough data to reproduce eval and policy incidents.

Write-time limits bound retrieval queries to 4,000 characters, selected/candidate ID lists to 200 entries, stored debug payloads to 64 KB, and citation snapshots to 8 ordinary or 24 debug entries with bounded text. Automation summaries are bounded by depth, list/dictionary cardinality, string size, and a 256 KB serialized ceiling.

Nightly maintenance strips detailed retrieval payloads older than 90 days and automation summaries older than 180 days while retaining event identity and exposure lineage. The same operation is available dry-run-first and reports eligible rows, estimated payload bytes, database sizes, and optional vacuum impact. These controls bound future growth; historical file-size reclamation still requires applying compaction and, where appropriate, an explicit vacuum.

## Reviewed Memory

Memories are typed claims with scope, content, sources, confidence, status, and lifecycle timestamps.

Supported states and flows:

- agents or deterministic jobs may propose;
- a local human may approve, reject, archive, or inspect;
- active memories may be returned at higher trust;
- proposed memories may be returned separately as candidates;
- memory audit checks malformed state, broken sources, duplicates, stale entries, unresolved conflicts, and superseded items;
- Markdown export/import is portable, while SQLite remains local canonical state.

MCP cannot approve memories or mutate wiki knowledge. It may propose memory, retrieve by status/type/scope, and record context feedback.

## Agent Interface

The current stable Knowledge Curation MCP surface is intentionally small:

- `search_knowledge(query, limit)`
- `retrieve_context(task, project)`
- `get_project_context(project)`
- `get_memories(scope, memory_type, status)`
- `propose_memory(memory_type, scope, content, sources, confidence)`
- `record_context_feedback(target_type, target_id, useful, note)`
- `write_agent_session(summary, files_touched, commands_run, outcome, unresolved_issues)`

Normal app-managed registration points to the `brain-mcp` shim. Direct `brain mcp` remains a development path.

An operational interface must use explicitly typed commands or packet sections and must remain read-only until its writer, reconciliation, and primary-role gates pass. External action planning and execution are not implicit powers of `retrieve_context`; they use the separate guarded-execution boundary defined in [Product Foundation](product-foundation.md).

## Evals

`brain eval run` supports extraction, routing, topology, conflict, relations, and retrieval suites.

Retrieval gates include:

- verdict accuracy;
- source-hit rate;
- fact precision;
- confidence calibration error;
- session-trace noise rate;
- 100% negative-control pass for provider/index promotion.

Before an Operational Chief-of-Staff briefing can become the default Today surface, chronological replay and reviewed suppressed-source samples must measure:

- high-severity item precision and recall;
- update/reschedule/cancellation linkage accuracy;
- duplicate-active, stale-active, premature-closure, and resolved-item-resurrection rates;
- briefing churn and operator correction rate;
- evidence-reference completeness and time-to-reconcile.

Detection-only precision is not sufficient. Shadow predictions become a labeled set only when surfaced and suppressed samples receive human or deterministic outcome labels.

Fixture loading accepts packaged built-ins plus local `evals/golden_queries.yaml` cases. Local cases are not dead configuration.

## Current Gaps

The following are experiments, not implied current behavior:

- fact vectors as a second stamped collection;
- query expansion;
- neighbor/context expansion;
- cross-encoder reranking;
- semantic entity/gardener candidate generation;
- email retrieval fixtures and source weights;
- `ops.sqlite` item retrieval, freshness ranking, briefing packets, and operational feedback telemetry.

Each must be evaluated independently and kept only if it improves the suite without weakening negative controls, source grounding, latency, or storage bounds.

## Acceptance

- FTS retrieval works with no embedding extra or model.
- A provider/index mismatch never executes vector search.
- Switching providers requires an explicit rebuild and produces a new valid stamp.
- Negative controls return `no_strong_match` and no authoritative facts.
- Returned facts/pages can be traced to source evidence.
- Popularity sorting is distinct-event based and advisory only.
- Eval and acceptance retrievals create no production retrieval or exposure-lineage rows.
- Proposed and active memories remain visibly separate.
- Telemetry compaction preserves required lineage and aggregate popularity.
- Operational queries exclude `resolved|dismissed|cancelled|expired` items and reconciled duplicates from active briefing sections and never present stale active items as current without a staleness warning.
- Every surfaced operational item exposes current-state timing and stable evidence references.
- Operational exposure and feedback do not change knowledge popularity, confidence, or authority.
- A secondary without an approved operational replica reports operational context unavailable and performs no operational write.

Verification:

```bash
uv run brain doctor --home <test-home>
uv run brain index doctor --home <test-home>
uv run brain eval run --suite retrieval --home <test-home>
uv run pytest tests/test_core.py tests/test_evals.py \
  tests/test_memory_export.py tests/test_service_json.py -q
```
