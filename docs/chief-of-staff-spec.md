# Chief-of-Staff Autonomous Wiki — Canonical Spec (optimum)

**Status:** canonical implementation spec. Supersedes earlier implementation and architecture drafts, which have been removed.
**Date:** 2026-06-24

This is the single source of truth for the Chief-of-Staff autonomous wiki direction.

---

## 1. Goal & shape

Make the CoS layer an **autonomous local knowledge curator**: track facts from evidence, maintain coherent source-backed pages, and avoid human review of routine changes. Human attention is reserved for policy, sampled audits, and irreducible factual uncertainty.

```
Trust layers (information flows downhill only):
EVIDENCE → FACTS → PAGES        (immutable → claims → rebuildable projections)

Mutation path (orthogonal — the audit trail, not a layer):
Proposed Action → Policy → Apply → Audit → Revert/Demote if needed
```

**All mutation flows through one action system.** That is the shift from a review workflow to an autonomous substrate.

---

## 2. Primitives (9)

1. **Evidence** — immutable docs/chunks/source paths/offsets. Fact provenance stored as chunk/span refs, not just doc IDs. Evidence quotes are rebuildable caches.
2. **Fact** — one atomic cited claim with address, provenance, status, and **three** confidences (extraction/routing/truth).
3. **Address** — `page_hint`, `entity_key`, `section_hint`. Extraction assigns; gardener may change via a recorded, reversible, audited action.
4. **Page Contract** — durable spec of what a page is for; the gardener's convergence target (anti-oscillation).
5. **Action** — the universal mutation record; carries a ledger-level inverse. Sits **under** `wiki_curation_runs` (a run groups actions).
6. **Policy** — versioned, ordered rules over action *features* deciding autonomy (L0–L3). Every action records the policy version that dispositioned it.
7. **Page** — rebuildable: deterministic canonical body + non-canonical LLM synthesis block.
8. **Audit** — samples applied actions, tracks model/policy/critic error rates, triggers guarded revert or policy demotion. `cos_actions` is both write ledger and telemetry stream.
9. **Escalation** — `open_questions`, human-facing residue **only**; references `cos_actions.id`. Never the automation substrate.

**Autonomy levels:** L0 deterministic auto · L1 proposer + independent critic, auto if critic agrees · L2 apply + mandatory sampled audit · L3 human residue.

---

## 3. Current code baseline (gap)

**Built:** evidence layer (`service.py`/`indexes.py`); enriched `facts`; `open_questions`, `wiki_curation_runs`, `wiki_page_snapshots`; `cos_actions`; versioned `cos_policy`; `page_contracts`; `wiki_page_syntheses`; shared retrieval FTS; fact lineage target validation; `upsert_candidate_facts`, `resolve_fact_groups`, `curate_managed_pages`/`render_managed_page`; correction/revert; role-aware JSON LLM calls; fact/chunk/wiki retrieval with eval fixtures; UI views for facts/actions/policy/contracts/audit. Legacy `wiki_change_*` tables remain as archived audit/migration compatibility data, but UI/CLI/MCP/nightly no longer create, apply, absorb, or search legacy wiki proposals. Migration max = **16**.

**Not built:** fully eval-gated autonomous topology writes beyond conservative policy, production LLM extraction beyond shadow/gated modes, and a physical table-drop migration for archived `wiki_change_*` history.

---

## 4. Data model (migrations 009+)

Conventions: `TEXT` PKs via `new_id(prefix)`, JSON-in-`TEXT`, ISO-8601 `TEXT` timestamps, `INTEGER` bools, sequential `MIGRATIONS` tuples.

### 009 — enrich `facts`
```sql
ALTER TABLE facts ADD COLUMN source_spans TEXT NOT NULL DEFAULT '[]';   -- [{chunk_id,start,end}]
ALTER TABLE facts ADD COLUMN evidence_quote TEXT;                        -- rebuildable cache
ALTER TABLE facts ADD COLUMN extraction_method TEXT NOT NULL DEFAULT 'legacy'; -- legacy|llm|manual
ALTER TABLE facts ADD COLUMN extractor_model TEXT;
ALTER TABLE facts ADD COLUMN effective_at TEXT;                          -- when claim became true
ALTER TABLE facts ADD COLUMN extraction_confidence REAL;
ALTER TABLE facts ADD COLUMN routing_confidence REAL;
ALTER TABLE facts ADD COLUMN truth_confidence REAL;
CREATE INDEX IF NOT EXISTS idx_facts_truth ON facts(status, truth_confidence);
```
Backfill: `truth_confidence=confidence`, `extraction_method='legacy'`, spans empty. Keep `confidence` as deprecated alias of `truth_confidence`.

### 010 — `cos_actions` (the spine)
```sql
CREATE TABLE IF NOT EXISTS cos_actions (
  id TEXT PRIMARY KEY,
  run_id TEXT,                                  -- FK wiki_curation_runs.id
  action_type TEXT NOT NULL,                    -- see action types below
  status TEXT NOT NULL,                         -- proposed|auto_applied|needs_human|approved|
                                                -- rejected|applied|reverted|failed|superseded|timeout_resolved
  target_fact_ids TEXT NOT NULL DEFAULT '[]',
  target_page_paths TEXT NOT NULL DEFAULT '[]',
  target_contract_ids TEXT NOT NULL DEFAULT '[]',
  action_features TEXT NOT NULL DEFAULT '{}',   -- affected_fact_count, affected_page_count, blast_radius,
                                                -- confidence(s), reversible, candidate_signal, similarity, eval_gate
  proposed_by TEXT, critic_by TEXT, critic_decision TEXT,  -- agree|disagree|abstain
  confidence REAL, risk_tier TEXT,
  policy_id TEXT, policy_version INTEGER, policy_decision TEXT, autonomy_level TEXT,
  inverse_action_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  applied_state_hash TEXT,                      -- post-apply fingerprint of targets (guarded revert)
  audit_status TEXT NOT NULL DEFAULT 'unaudited', -- unaudited|sampled_ok|sampled_bad|na
  created_at TEXT NOT NULL, applied_at TEXT, reverted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cos_actions_status ON cos_actions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_cos_actions_run ON cos_actions(run_id);
CREATE INDEX IF NOT EXISTS idx_cos_actions_audit ON cos_actions(audit_status, applied_at);
```
**Action types:** `fact_upsert`, `fact_merge`, `fact_supersede`, `resolve_conflict`, `display_contested`, `page_merge`, `page_split`, `rehome_fact`, `rename_page`, `canonicalize_page`, `archive_page`, `edit_contract`, `synthesize_page`.

### 011 — `cos_policy` (versioned ordered rules)
```sql
CREATE TABLE IF NOT EXISTS cos_policy (
  id TEXT PRIMARY KEY,
  version INTEGER NOT NULL,                      -- active = rows at MAX active version
  priority INTEGER NOT NULL,                     -- first match wins
  match_action_types TEXT NOT NULL DEFAULT '["*"]',
  match_predicate TEXT NOT NULL DEFAULT '{}',    -- thresholds over action_features
  autonomy_level TEXT NOT NULL,                  -- L0|L1|L2|L3
  critic_required INTEGER NOT NULL DEFAULT 0,
  timeout_allowed INTEGER NOT NULL DEFAULT 0,
  timeout_after_seconds INTEGER,
  audit_sample_rate REAL NOT NULL DEFAULT 0.0,
  demotion_threshold REAL,
  auto_revert_signals TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cos_policy_active ON cos_policy(active, version, priority);
```
**Seed v1 (conservative):** L0 only for deterministic no-op/canonicalization; L3 for truth resolution; no truth timeouts; no source-authority winner selection. Policy edits are human-owned and logged.

### 012 — `page_contracts`
```sql
CREATE TABLE IF NOT EXISTS page_contracts (
  id TEXT PRIMARY KEY, page_hint TEXT NOT NULL,
  canonical_entity TEXT, page_scope TEXT, retrieval_purpose TEXT,
  what_belongs_here TEXT, what_does_not_belong_here TEXT,
  freshness_policy TEXT, related_pages TEXT NOT NULL DEFAULT '[]',
  version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_page_contracts_page ON page_contracts(page_hint, status);
```
Non-unique index (versioning may need transient multiple rows). Contract edits go through `edit_contract` actions. A fact fitting no contract → proposed new contract/page or escalation, never forced placement.

### 013 — `wiki_page_syntheses`
```sql
CREATE TABLE IF NOT EXISTS wiki_page_syntheses (
  id TEXT PRIMARY KEY, page_hint TEXT NOT NULL,
  synthesis_markdown TEXT NOT NULL, fact_ids TEXT NOT NULL DEFAULT '[]',
  fact_hash TEXT,                               -- hash of active fact set → deterministic staleness
  model TEXT, prompt_version TEXT,
  generated_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wiki_page_syntheses_page ON wiki_page_syntheses(page_hint, generated_at);
```
Derived artifact; never evidence/fact. Stale when `fact_hash` of current active facts ≠ stored, or `prompt_version` changed.

### 014 — extend `open_questions` (escalation-only)
```sql
ALTER TABLE open_questions ADD COLUMN action_id TEXT;            -- FK cos_actions.id
ALTER TABLE open_questions ADD COLUMN recommended_action TEXT NOT NULL DEFAULT '{}';
ALTER TABLE open_questions ADD COLUMN auto_resolve_after TEXT;
ALTER TABLE open_questions ADD COLUMN risk_tier TEXT;
ALTER TABLE open_questions ADD COLUMN resolver TEXT;
ALTER TABLE open_questions ADD COLUMN decided_by TEXT;           -- human|llm_critic|timeout_default
```
Status gains `needs_human|auto_resolved|timeout_resolved`. Rows created only for L3 residue, always referencing an action.

### 015 — fact indexes
- Shared `retrieval_fts` rows with `kind='fact'` index facts for lexical retrieval and reuse the same fusion path as chunks.
- Generalize `indexes.py` from single `TABLE_NAME="chunks"` to multi-collection; add LanceDB `facts` table: `{fact_id, vector, statement, page_hint, status, truth_confidence}`. Rebuildable.

### 016 — lineage at fact grain
- Allow `context_lineage_events.target_type='fact'`; record exposure/usefulness per fact.

---

## 5. Components

### 5.1 LLM layer (`llm.py`) — extend
- `complete_json(prompt, schema=None) -> dict` with bounded JSON repair/retry.
- **Role mapping**: `extractor`, `gardener`, `resolver`, `critic`, `synthesizer`, `auditor`. Critic must be configurable to a *different* provider/model/prompt than the proposer (separation of duties).
- Shared **candidate-card builder** (compact cleaned previews) reused by extraction/gardener/synthesis/audit.

### 5.2 Extraction (`extraction.py`) — active, gated
Select recent/changed docs that pass source-type policy → partition each document into full-coverage chunk windows → send one source window plus lightweight routing hints to `extractor` → `extractor` emits atomic facts with `statement`, `chunk_id`, exact `evidence_quote`, `page_hint`, `section_hint`, `entity_key` candidate, `effective_at?`, and three confidences → deterministic validation locates the quote in the cited chunk and derives `source_spans` → emit `fact_upsert` **actions**. Shadow first. Replaces `propose_from_sources` and (eventually) `compile_semantic_wiki` as the source→knowledge path.

Payload/harness contract: `docs/extraction-payload-spec.md`. The extractor does not receive the existing fact table. It is bulk within one window, but validation and retry are per fact. `agent_session_log` is skipped by default unless extraction config opts it in.

### 5.3 Action engine (`cos_actions.py`) — new (universal write path)
`propose_action`, `decide_action`, `apply_action`, `revert_action`, `record_action_audit`. **All** mutations route here: fact upsert/merge/supersede, conflict display, page rehome/merge/split/rename/canonicalize/archive, contract edit, synthesis regen.
- `apply_action`: perform deterministic mutation → re-run `resolve_fact_groups` + `curate_managed_pages` for affected pages → snapshot pages → enqueue targeted reindex of affected `fact_ids` + page embeddings → store `applied_state_hash` → set status/`applied_at`.
- **Guarded revert:** store the target state hash at apply time and the inverse ledger state; `revert_action` refuses (and escalates) if target state has drifted since apply, else applies `inverse_action_json` at the **ledger** layer (restore routing/status per fact), then re-projects + reindexes. Markdown snapshot is secondary.
- Inverse example (`rehome_fact`/`page_merge`): `{"restore_fact_routing":[{"fact_id","old_page_hint","old_entity_key","old_section_hint"}]}`.

Current action-boundary rule:
- Ledger mutations must be represented as `cos_actions`: fact upsert, fact merge/supersede, conflict display/resolution, manual correction, fact rehome, contract edit, page topology changes, page archive, and derived synthesis creation/staling.
- One-time wiki-fact imports, reconciliation, canonical fact rerouting, and manual corrections are not exceptions: they propose/apply `fact_upsert`, `fact_merge`, `fact_supersede`, `display_contested`, or `rehome_fact` actions before durable fact rows change.
- Deterministic projections are not independently trusted facts: managed page rendering, fact retrieval reindexing, citation snapshots, and derived page/index rebuilds. They should run after applied ledger actions and remain rebuildable from the ledger.
- `cos_actions.ACTION_TYPE_SPECS` is the implementation support map. Implemented action types apply or revert with an inverse payload; declared-but-unimplemented action types fail explicitly instead of falling through.

### 5.4 Policy engine (`cos_policy.py`) — new
Load active version → evaluate ordered rules over `action_features` (first match) → record `policy_id/version/decision/autonomy_level` on the action → route to {auto-apply | critic-then-apply | escalate | block}. Enforce **eval gates** (a rule can't promote past its eval threshold). Auto-demotion creates a new policy version. Declarative — no hard-coded `merge_two_singletons` action variants; use features.

### 5.5 Fact resolution (`resolve_fact_groups`) — keep, route via actions
Keep deterministic merge/supersede; emit `fact_merge`/`fact_supersede`/`resolve_conflict`/`display_contested` actions. Truth conflict: if displayable as contested → `display_contested`; if a winner must be picked and no explicit confirmation/source-trust model exists → escalate (L3). **Never timeout into a chosen winner.**

### 5.6 Page contracts (`contracts.py`) — new
Generate initial contracts for managed pages; validate fact/page conformance; gardener proposes `edit_contract`; contracts are the routing convergence target. Shown in UI (they're policy-like).

### 5.7 Page-gardener (`gardener.py`) — new (Stage 2b)
Deterministic candidate generation (near-dup page_hints; embedding-similar pages → MERGE; intra-page multi-cluster → SPLIT; singleton near larger → REHOME) — bounded, never scan-all. `gardener` LLM judges candidates **against contracts** → emits `page_merge`/`page_split`/`rehome_fact`/`rename_page`/`archive_page`/`edit_contract`. Guardrails: no fact loss; provenance preserved; blast caps; cooldown (no re-propose without new evidence); hysteresis (repeated apply/revert cycles demote policy).

### 5.8 Two-zone rendering (`render_managed_page`) — extend
Canonical body: deterministic active facts, stable section order, `fact_ids`/`source_ids` in frontmatter. Synthesis block: `synthesizer` from active facts only, cites fact IDs, stored in `wiki_page_syntheses`, inserted as a clearly-labeled derived block, down-weighted in retrieval, never source evidence. Regenerate when `fact_hash`/`prompt_version` changes.

### 5.9 Retrieval (`service.py`, `indexes.py`) — extend
Engine unchanged (FTS5 + LanceDB + RRF + rerank + source weighting + bounded packet). Add:
- This section is complemented by `docs/chief-of-staff-retrieval-contract.md` and `docs/chief-of-staff-retrieval-tuning.md`, which define verdict/calibration behavior and current tuning notes.
- Facts in the candidate stream; return facts directly when best **only after** a fact-specific relevance gate. Fact retrieval is dynamic 0..N, never fixed top-k leakage.
- Fact ranking normalizes raw FTS/vector/reranker signals onto a comparable positive relevance score and applies a calibrated `FACT_SCORE_FLOOR`; raw SQLite BM25 values are not compared directly with chunk/page scores.
- Return only active facts as authoritative. Conflicted facts may return only as contested pairs. Exclude inactive, superseded, and low-`truth_confidence` facts from authoritative retrieval.
- **Granularity dedup:** prefer fact > page > chunk; suppress a chunk whose `source_spans.chunk_id` is already covered by a returned fact; descend to chunk only for verification.
- Fact citation snapshots with chain `fact → source_spans → chunks → documents`.
- Retrieval verdict/confidence must incorporate facts, managed pages, chunk quality, source grounding, and noise. A high lexical match from raw/session traces alone must not produce high confidence for broad/meta queries.
- Negative controls must return `no_strong_match` with zero returned facts; near misses belong in a separate non-authoritative tier, not the main context packet.
- Synthesis indexed `derived=true`, down-weighted, never sole citation.
- Record fact-level lineage; useful feedback can raise `truth_confidence`/propose `confirmed_by_user`.

### 5.10 Audit/control loop (`cos_audit.py`) — new
Nightly/UI: sample applied actions per `audit_sample_rate` (risk-weighted with stable sampling); `auditor`/`critic` score → `sampled_ok|sampled_bad`. Stub mode records missing audits when no auditor provider is configured. Configured mode records auditor metadata and demotes only when the audited bad-rate for a policy group exceeds that rule's `demotion_threshold`; on configured bad feedback, guarded revert may be requested explicitly.

### 5.11 UI (`ui_server.py`) — extend
Show: current policy version + rules; recent/auto-applied actions; audit failures; human residue; page contracts; fact provenance/spans; contested facts; synthesis status. Hide/deprecate legacy packet review as the primary workflow.

---

## 6. Eval harness (`evals.py`, `retrieval_fixtures.py`, `brain eval`) — build first
Suites + metrics: **extraction** (span coverage for non-legacy extracted facts), **routing** (coverage vs page/entity assignment), **topology** (golden deterministic merge/rehome/split/contract candidate precision/recall/F1), **conflict** (precision; hard sub-metric: **zero false truth-resolutions**), and **retrieval** (golden historical queries + negative controls; verdict accuracy, provenance-aware source-hit, fact precision, confidence calibration/ECE, noise rate, and negative-control pass). CLI `brain eval run [--suite ...]`. Gates policy promotion (recorded as a version bump); retrieval thresholds ratchet upward as fixtures are curated.

Retrieval negative controls are fixed golden fixtures, but their synthetic absent-topic strings are eval artifacts, not knowledge. Agent-session indexing must redact those exact strings from captured logs/reports so discussing or running the eval cannot turn a negative control into future evidence.

---

## 7. Build plan (14 phases; nothing auto-applies before P10)

| P | Phase | Key acceptance |
|---|---|---|
| 0 | Preserve current state | repo boots; tests pass; legacy backlog still drainable; no deletions |
| 1 | Eval harness + LLM JSON/role layer | `brain eval run` scores extraction/routing/topology/conflict/retrieval suites on fixtures; `complete_json` tested with fake providers; roles configurable |
| 2 | Fact provenance enrichment (009) | old + new facts readable; CoS UI works; backfill correct |
| 3 | Action ledger + guarded revert (010) | apply/revert round-trips (upsert, rehome); out-of-order revert refuses+escalates; packet absorption still runs |
| 4 | Policy engine (011) | first-match/version/predicate tests; default never auto-applies risky changes |
| 5 | Extraction in shadow | no auto-writes; extraction eval ≥ threshold; spans + routing_confidence in debug |
| 6 | Page contracts (012) | contracts for top pages; routing/gardener read them; edits are actions |
| 7 | Two-zone rendering (013) | synthesis cites fact IDs; deleting synthesis leaves canonical page valid; retrieval never cites synthesis alone |
| 8 | Fact retrieval (015/016) | queries return direct facts only above a fact relevance floor; dynamic-k can return zero facts; inactive/low-confidence facts not authoritative; contested pairs; reindex-on-action verified; retrieval eval fact leak controls pass |
| 9 | Gardener in shadow | no writes; topology eval scores candidates; no re-propose of rejected without new evidence |
| 10 | L0/L1 topology auto-apply | only low-risk topology promoted (eval-gated); blast caps enforced; policy version on every auto action; truth stays manual/display |
| 11 | Critic + audit + demotion + auto-revert | critic disagreement escalates; audit marks bad; demotion bumps policy version; revert refuses on drift |
| 12 | Timeout-into-uncertainty (014) | tests prove truth never times out into a winner; stale residue doesn't block page regen |
| 13 | Legacy retirement | Active UI/CLI/MCP/nightly legacy proposal creation/review/absorption paths are retired after backlog drain; `wiki_change_*` tables remain archived until a later explicit table-drop migration |

(Migrations land in the phase that first needs them; `run_migrations` applies all pending idempotently.)

---

## 8. Non-negotiable invariants
- Evidence is immutable. Pages never become facts. Synthesis never becomes evidence.
- Every mutation is a `cos_action`; every auto action records its policy version.
- New extracted facts include source spans.
- Truth conflicts never timeout into guessed truth (only into displayed uncertainty).
- Reverts are guarded at the ledger level.
- Human review is residue, not routine approval.

---

## 9. Resolved implementation decision
Facts FTS uses the shared retrieval table with `kind='fact'`, implemented in migrations 015/016 and reused by fusion/rerank. There is no separate active `facts_fts` table.

## 10. Key code touchpoints
`migrations.py` (009–016; max=16) · NEW `cos_actions.py`/`cos_policy.py`/`extraction.py`/`contracts.py`/`gardener.py`/`cos_audit.py` · `llm.py` (`complete_json`+roles, :32) · `indexes.py` (multi-collection, :12) · `service.py` (fact retrieval/dedup; `MANAGED_WIKI_BOOST`; `retrieve_context`) · `wiki_facts.py` (route mutators through actions; `resolve_fact_groups`, `render_managed_page`, `revert_wiki_page_snapshot`) · `automation.py` (nightly stages) · `ui_server.py` · `evals.py` + `retrieval_fixtures.py` + `cli.py`.
