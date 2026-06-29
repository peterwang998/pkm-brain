# Entity Layer & Resolution — Spec

**Status:** spec for Codex
**Last verified:** 2026-06-27 against working tree atop `73b1e90` (dirty — 38 files uncommitted; commit first per `docs/cos-determinism-and-doc-conventions.md` §3)
**Goal:** give facts a real *identity* layer so dedup / merge / conflict-scoping / routing stop riding on a page-section slug. The extractor already emits a usable entity guess; the system discards it. Fix that first (cheap, high-leverage); defer the rest. Companion to `docs/extraction-payload-spec.md`.

---

## Why now — evidence from the 2026-06-27 shadow comparison

Run: `/Users/Peter/brain-shadow/compare-earned-simple-20260627-083553` (20 hyprnote meetings → 171 accepted facts).

- The system created **111 `entity_key`s for 171 facts (74 singletons, 67%)** — almost no aggregation.
- The **extractor already identified ~66 coherent entities** (distinct `model_entity_key` after trivial case-folding). The model's entity resolution is fine; the system throws it away.
- `entity_key` is **100% derivable from `slug(page):slug(section)`** — it is a *render target*, not an identity. So one entity fragments across every page it appears on:
  - `sierra` → 20 buckets, `peter` → 12, `hightouch` → 10, `unity catalog` → 8, `peter wang` → 5.
- The "inconsistency" is small and mechanical: **5 case/format groups** (`Databricks/databricks`, `Hightouch/hightouch`, `Peter/peter`, `Sierra/sierra`, `Unity Catalog/unity_catalog`) — all fixable by normalization with **zero LLM**. Exactly **one** genuine resolution case: `Peter` vs `Peter Wang`.
- Same root cause drives the **false-positive conflicts** in that run (earned/complex mode flagged 5 "conflicts", all non-contradictions): conflict-checking is scoped by the page-section bucket, so it compares facts that merely sit *near* each other instead of facts *about the same entity*.

**Conclusion:** identity is the keystone. Fixing it improves aggregation, conflict precision, and routing at once — and most of the fix is "use what the model already emits + normalize," not a new subsystem. This is why we do the cheap deterministic slice first and gate the LLM/automation tiers on whether residual error justifies them.

---

## Current state — implemented vs not (verified against the working tree)

### Implemented today
- `entities` + `relations` tables exist (`db.py:69,77`) but are **dead** — zero readers/writers.
- Extractor asks the LLM for an entity guess and **captures it** as `metadata.model_entity_key` (`extraction.py:1089,1112`). ← the signal we need already exists.
- Persisted `entity_key` is **deterministically derived from the page route**: `entity_key_for_change(topic, page_hint, section_hint)` → `slug(topic):slug(stem):slug(section)` (`extraction.py:1090`, `wiki_facts.py:339`).
- `facts.entity_key` column + index `(entity_key, status, observed_at)` (`migrations.py:155,174`); `facts.page_hint`/`section_hint` (`migrations.py:156,157`).
- Fact grouping / resolution / merge all key off `entity_key` (`wiki_facts.py:615,624,631,710,761,795,1049`) — resolution is scoped by **page:section bucket**, not by entity.
- Fallback routing → `unrouted_fact` residue when the model routes to `concepts/extracted-facts.md` (`extraction.py:54,611`).
- Latest migration: `017_create_cos_stage_watermarks` → **next migration is 018**.

### Not implemented (the to-do)
- No `facts.entity_id`; no `fact_entities` table; no `entities` extensions (aliases / status / merged_into / description).
- `model_entity_key` is captured but **never normalized, resolved, or used** for identity.
- No `entities.py` (normalization / candidate generation / resolution).
- No identity↔routing split — `entity_key` still conflates "what it's about" with "where it renders."
- No LLM disambiguation tier; no `entity_merge` / `entity_split` actions; no entity-gardener.
- No closed-vocab page routing / fuzzy page-dup gate (the separate "page drift" fix).

---

## Model decision (unchanged) — D0

**Entity-centric property-graph, not a triple/OWL knowledge graph.** Facts stay natural-language claims with provenance (the atomic, retrievable unit). Add resolved **entity nodes** and link facts to them. Keep relations (typed edges) as an optional, deferred, *derived* layer.

*Justification:* a full triple/OWL store trades statement-slop for triple-slop + a mandatory predicate ontology, loses NL nuance, makes per-claim provenance a second-class citizen, and its monotonic DL reasoning chokes on the contradictions we deliberately hold. Wikidata — the largest practical KG — uses property-graph + per-statement qualifiers/references for exactly these reasons. We borrow the *ideas* (stable ids, SKOS-style aliases, controlled predicates if/when we add edges), not the OWL/RDF/SPARQL/reasoner stack. If interop is ever needed, add an RDF *export view* over the relational model later.

---

## Prioritized sequence of actions

**Build Phase 1, run the shadow ER check, then decide whether 2–4 are worth it.** Phase 1 is the keystone and is cheap (deterministic, offline-safe). Phases 2–4 add LLM/automation and should be justified by Phase-1 *residual* error, not built up front. The page-routing track is independent and can run in parallel.

### Phase 1 — Real identity, cheaply (no new LLM calls) ← the shippable slice

Implements D1 (split identity from routing) + a deterministic-only slice of D2–D4.

**Storage shape and extraction richness are orthogonal — keep them separate.** Build the correct many-to-many **`fact_entities`** link table *now* (no painful single→many migration later; the co-mention signal is preserved), but in Phase 1 **populate the primary entity only** — one row per fact, from the single `model_entity_key` the extractor already emits. Multi-mention extraction and its consumers (co-occurrence disambiguation, graph queries, secondary-mention retrieval) are deferred to Phase 2+ and are then *purely additive*: more rows + new readers, **no migration, no rewrite**. Keep a denormalized **`facts.entity_id`** = the primary entity (a cache of the `is_primary` link, not a second source of truth) so the hot resolution-grouping path stays a simple column `GROUP BY`, mirroring today's `entity_key`.

1. **Migration 018** (`_migration_018_entity_identity`):
   - extend `entities` via `_ensure_column`: `aliases TEXT NOT NULL DEFAULT '[]'` (SKOS altLabels), `status TEXT NOT NULL DEFAULT 'active'` (active|merged|archived), `merged_into TEXT`, `description TEXT`; add indexes `idx_entities_name`, `idx_entities_status`.
   - create **`fact_entities`** (source of truth for fact↔entity links): `id`, `fact_id`, `entity_id`, `is_primary INTEGER NOT NULL DEFAULT 0`, `mention_text`, `mention_span` (`{chunk_id,start,end}`, optional), `resolution_method` (`exact|alias|fuzzy|embedding|llm|human|created`), `confidence REAL`, `created_at`; indexes on `fact_id` and `entity_id`.
   - add `facts.entity_id TEXT` (denormalized **primary** entity; nullable, backfilled on re-extraction). **Keep `facts.entity_key` as the page route** — do not drop.
   - register in `MIGRATIONS` (`migrations.py:609`).
2. **New `entities.py`** — deterministic resolution, offline-safe:
   - `normalize_entity_name()` — case-fold, trim, collapse whitespace/underscores, strip punctuation. (Collapses the 5 case/format groups from the shadow run.)
   - `resolve_entity(mention, type_hint)` — exact/alias match on `entities.name` + `aliases` → **link**; no match → **create provisional**; return `(entity_id, resolution_method)` where method ∈ `exact|alias|created`. Append resolved surface variants to `entities.aliases` so the vocabulary self-learns and future matches get cheaper.
3. **Wire into extraction** (`extraction.py` ~`:1089`): feed the existing `model_entity_key` (+ type hint if available) through `resolve_entity`; write the primary `fact_entities` row (`is_primary=1`) **and** set the `facts.entity_id` denorm. Stop treating the model's string as identity verbatim. Keep deriving `entity_key` (page route) exactly as today.
4. **Re-scope resolution/conflict grouping to the primary `entity_id`** (`wiki_facts.py:615,624,631`): group candidate facts by `facts.entity_id` for dedup / merge / conflict, **not** by `entity_key`. ← this is the change that kills *both* the fragmentation and the false-positive conflicts. (Grouping uses the primary entity only, so the link table never complicates this hot path.)
   - *De-risk:* this touches the resolution path. Ship steps 1–3 first (populate `entity_id`, observe), then flip grouping in a follow-up commit behind the tests below.

*Acceptance (Phase 1):*
- "Peter Wang" across `career/databricks.md` / `career/peter.md` / `career/google.md` resolves to **one** `entity_id` (was 12+ keys); the 5 case/format groups collapse with **zero LLM calls**.
- Resolution groups by entity: the shadow run's false-positive pairs (e.g. "Netflix's process" vs "second-round case study") no longer collide because they are different entities.
- A single entity owns facts across multiple page routes (identity ≠ routing).
- Offline path works with no provider configured (exact/alias/create only).
- Each fact writes one `fact_entities` row (`is_primary=1`) + a matching `facts.entity_id`; the schema accepts >1 row per fact with no further migration.

### Phase 2 — LLM disambiguation for the ambiguous minority (closed-set; deferred)

Implements the LLM tier of D4. Only when Phase-1 resolution returns **multiple candidates** or a known alias-ambiguity (e.g. `Peter` vs `Peter Wang`): prompt the `resolver` with the mention + context + the **closed candidate list** (id, name, description, aliases); it picks one **or** says "new" — it **never free-types an id**. The shadow data says this is rare (~1 case in 171). **Build only if Phase-1 residuals justify it.**

This is also where the extractor graduates from a single `model_entity_key` to a **list of mentions** (writing the secondary `fact_entities` rows). Once secondary mentions exist, **co-occurrence becomes a resolution signal** — e.g. "UC" co-mentioned with "Databricks" resolves to *Unity Catalog*, not *University of California* — which both this tier and the Phase-4 gardener can exploit. This is the concrete payoff of keeping the link table from Phase 1.

### Phase 3 — Reversible merge/split (deferred)

Implements D6. `entity_merge` / `entity_split` as `cos_actions` with inverses: re-point both `fact_entities.entity_id` **and** the `facts.entity_id` denorm from merged → canonical, record the old links for the inverse, set `entities.status` / `merged_into`. **Bias under-merge**; type mismatch (person↔org) blocks a candidate outright; large/cross-type merges → human (the existing large-topology gate).

### Phase 4 — Nightly entity-gardener (deferred)

Implements D5. Reuse the page-gardener pattern over accumulated provisional entities: propose merges via name/alias/(embedding) similarity + LLM judgment → `entity_merge` actions under policy. This is "entity-topology maintenance." Embedding similarity is gated on a real encoder (see caveats); lexical+alias works now. Cold-start sprawl is expected and is what this pass reconciles.

### Separate track — Page routing quality (the "page drift" fix) — independent of identity

Do **not** fold this back into entity identity (re-conflating them is the original bug). This is about *where a fact renders*, not *what it's about*:
- **Closed-vocabulary routing:** prefer an existing wiki page; allow a new `page_hint` only when no existing page matches **and** a **fuzzy-dup check** passes — would have caught `career/2026-agent-pm-role-notes.md` vs existing `career/agent-pm-role-notes.md`, and `career/interview-patterns/product-estimation.md` vs `career/interview-prep/product-estimation.md`.
- Addresses the shadow run's 10 `unrouted_fact` + 7 missing/duplicate page routes.
- Build on existing helpers: `canonicalize_fact_routes` (`wiki_facts.py:2386`), `route_facts_to_sections` (`wiki_facts.py:2959`), `DEFAULT_FALLBACK_PAGE_HINTS` (`extraction.py:54`).

---

## Caveats (be honest about these)
- **Semantic candidate generation depends on the embeddings work.** With hash embeddings, Phases 1–2 do lexical + alias matching only, so semantic-variant mentions ("the streaming company" → Netflix) won't link until a real encoder exists (separate embeddings-productization workstream). Lexical+alias ER works now and is most of the value.
- **Cold start:** first ingest creates many provisional entities, many duplicates → expected; the Phase-4 gardener reconciles. Don't treat early sprawl as failure.
- **Don't let merge auto-apply for large/cross-type entities.** Type mismatch (person↔org) blocks a merge candidate outright; large merges → human.

## Non-goals
- No OWL/RDF/SPARQL/reasoner in the runtime (optional RDF export only, later, if interop is needed).
- No free-typed entity identity from the model (the model's guess is a *mention* to normalize + resolve, never a trusted id).
- No synchronous "perfect" ER at ingest — provisional link at write, reconcile later.
- No writing to `relations` / typed edges yet.
- `fact_entities` exists from Phase 1 but is **populated primary-only** until multi-mention extraction lands (Phase 2+); `facts.entity_id` is a denormalized cache of the primary link, not a second source of truth.

## Code touchpoints
`migrations.py` (new `_migration_018_entity_identity` — extend `entities`, create `fact_entities`, add `facts.entity_id`; register at `:609`) · `db.py` (`entities` `:69`) · `extraction.py` (`model_entity_key` capture `:1089,1112` → feed resolver, write primary `fact_entities` row; `entity_key` derivation `:1090` stays = page route; fallback routing `:54,611` for the page-routing track) · new `entities.py` (`normalize_entity_name`, `resolve_entity`) · `wiki_facts.py` (`entity_key_for_change` `:339` stays = page route; re-scope grouping `:615,624,631` to `entity_id`; merge paths `:710,761,795`) · `cos_actions.py` (`entity_merge`/`entity_split`, Phase 3) · `gardener.py` (entity-gardener, Phase 4) · `cos_policy.py` (large-merge risk tier, Phase 3) · `tests/test_cos.py`.

## Verification
```bash
uv run ruff check .
uv run pytest -q
# shadow ER on a copied brain db:
#   - facts.entity_id collapses page-fragmented entities (Peter Wang -> one id)
#   - the 5 case/format groups resolve with NO LLM calls
#   - resolution grouped by entity_id, not page:section (recheck the shadow false-positive conflict pairs)
#   - aliases grow as variants resolve in
#   - one fact_entities row per fact (is_primary=1); facts.entity_id matches it
```

## Acceptance / tests (consolidated)
- Exact-name mention links to the existing entity with **no LLM call**; novel mention creates a new entity. (offline path works without a provider)
- The five shadow case/format groups (`Databricks/databricks`, `Hightouch/hightouch`, `Peter/peter`, `Sierra/sierra`, `Unity Catalog/unity_catalog`) collapse to one entity each via normalization.
- A single entity owns facts across multiple page routes (identity ≠ routing); resolution/conflict grouping is by the primary `entity_id`.
- Phase 1 writes one `fact_entities` row per fact (`is_primary=1`) + the `facts.entity_id` denorm; the table accepts secondary mentions later with no migration.
- (Phase 2) Two plausible candidates → LLM disambiguation chooses from the closed set; never emits an unknown id.
- (Phase 3) `entity_merge` re-points both `fact_entities.entity_id` and the `facts.entity_id` denorm; `entity_split` (revert) restores exact prior links; large/cross-type merge → escalates.
- (Phase 4) Entity-gardener proposes merging `Netflix`/`Netflix Inc`; small merge auto-applies + is audited; large/cross-type merge → human residue.
