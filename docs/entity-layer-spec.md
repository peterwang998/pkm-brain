# Entity Layer & Resolution — Spec

**Status:** spec for Codex — Phase 1–4 landed; **Phase 2.5 quality gates landed**; **page-routing R1/R2 landed and extraction-shadow verified**; R3 remains.
**Last verified:** 2026-07-05 against commit `b4e284c` plus the current autonomy-policy diff. Phase 1 verified against the replay DB `/tmp/pkm_entity_phase1_replay/db/brain.sqlite` (171 facts → 66 entities, 0 LLM calls). Phase 2 verified with focused tests for typed resolution, structured mentions, secondary links, and closed-list resolver disambiguation against working tree atop `0b75dda` (dirty). Phase 2.5 verified with the real-data temp sample under `/private/tmp/pkm-entity-phase25-P1iqqG` and the 20-source broad replay under `/private/tmp/pkm-entity-phase25-broad-fixed-HXRD5W` (179 facts, 187 named links). Phase 3 merge/split behavior verified with focused entity + policy tests. Phase 4 entity-gardener verified with `tests/test_gardener.py` temp-brain merge/apply coverage, `uv run pytest -q` (296 passed), `uv run ruff check .`, and `git diff --check`. R1/R2 focused extraction routing tests pass (`uv run pytest tests/test_cos.py -q`). R2 real-data shadow extraction verified under `/private/tmp/pkm-r2-shadow-fixed-xmAxQp` (20 sources, 234 accepted facts, **93.16% canonical route rate**, 0 reference/log routes, 218 auto-applied in temp, 16 `unrouted_fact` residue); gardener deterministic candidates generated, but xhigh LLM gardener judgment timed out at 600s. **G1 decomposed gardener verified 2026-07-03** under `/private/tmp/pkm-e2e-shadow-20260703-gardener`: full e2e shadow (256 accepted facts, **93.36% canonical**, 0 reference routes — R2 reproduces), per-candidate judgment over **50 candidates / 4 xhigh workers / 120s per-call timeout: 0 timeouts, 0 errors, 3 dropped** (first non-rubber-stamp run); gardener wall time 429s (~34s/call, 3.5× timeout margin). **F1 high-certainty entity-merge recalibration landed 2026-07-05:** compact/exact-name entity merges remain low-risk despite large fact counts; generic large topology still escalates.
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

### Implemented today (pre-Phase-1 baseline; Phase 1 has since landed — see below)
- `entities` + `relations` tables exist (`db.py:69,77`) but are **dead** — zero readers/writers.
- Extractor asks the LLM for an entity guess and **captures it** as `metadata.model_entity_key` (`extraction.py:1089,1112`). ← the signal we need already exists.
- Persisted `entity_key` is **deterministically derived from the page route**: `entity_key_for_change(topic, page_hint, section_hint)` → `slug(topic):slug(stem):slug(section)` (`extraction.py:1090`, `wiki_facts.py:339`).
- `facts.entity_key` column + index `(entity_key, status, observed_at)` (`migrations.py:155,174`); `facts.page_hint`/`section_hint` (`migrations.py:156,157`).
- Fact grouping / resolution / merge all key off `entity_key` (`wiki_facts.py:615,624,631,710,761,795,1049`) — resolution is scoped by **page:section bucket**, not by entity.
- Fallback routing → `unrouted_fact` residue when the model routes to `concepts/extracted-facts.md` (`extraction.py:54,611`).
- Latest migration: `017_create_cos_stage_watermarks` → **next migration is 018**.

### Phase 1 — landed ✅ (verified 2026-06-28 against `/tmp/pkm_entity_phase1_replay/db/brain.sqlite`)
- `entities` extended (aliases / status / merged_into / description); `fact_entities` link table (9 cols); `facts.entity_id` denorm — all present (migration 018 applied).
- `entities.py` resolver wired: 171 facts → **66 entities** (was 111 `entity_key` buckets), **105 `exact` + 66 `created`, 0 LLM calls**; 171 `fact_entities` rows all `is_primary=1`; denorm consistent (0 mismatches); 21/66 entities learned an alias.
- Identity split from routing: `entity_id` (66) now distinct from the page route `entity_key` (still 111); big entities unified across routes (Sierra 20→1, Peter 12→1, Hightouch 10→1, Unity Catalog 8→1).

### Still to-do
- **R2 extraction shadow verified; next blocker is gardener timeout / R3.** 127/178 accepted facts (71%) fell back to `concepts/extracted-facts.md` in the 2026-07-01 real-model run because routing hints were recency-ordered and loaded once per run, so windows rarely saw their true target page. R1 now selects per-window relevance-ranked hints; R2 now excludes reference/log destinations, normalizes `wiki/` prefixes, records route metrics, and fuzzy-snaps near-duplicate canonical paths. The 2026-07-02 corrected R2 shadow run improved to **218/234 canonical routes (93.16%)**, with **0 reference/log routes**, **215 existing targets**, **3 fuzzy snaps**, **3 new canonical pages**, and **16 fallback/unrouted facts**. The xhigh gardener LLM judgment timed out at 600s; **G1 (decomposed per-candidate judgment) has since landed and shadow-verified 2026-07-03 with 0 timeouts** — remaining gardener work is drop observability (G1.1) and effort tiering for cost (G1.2), see **Phase 4 gardener hardening (G1–G3)** below.
- **Phase 2.5 quality gates landed:** entity-worthiness `mention_kind` gate (drop generic/deictic), `entity.admit_kinds` concept knob, persisted `fact_entities.mention_kind`, and quote fuzzy-snap-to-source. The 2026-06-30 real-LLM temp sample accepted 4/4 facts and linked only the two `named` Databricks mentions; three `generic` mentions (`interviews`, `office`, `new manager`) stayed in fact text but created no entities. The 20-source broad replay accepted 179 facts, applied 187 links, and gated 295 non-admitted mentions (198 concept, 95 generic, 2 deictic); all applied links were `mention_kind=named`.
- Broad-run decision: admission is controlled by `mention_kind`, not by `entity_type`. Do **not** add a deterministic "block `entity_type=concept`" rule; named referents that currently type as `concept` (`CCPA`, `GDPR`, `FinOps`, `Palantir model`) are legitimate identity nodes even though the coarse type enum is imperfect.
- Broad-run measurement gap: 41/179 facts had no entity link because every extracted mention was concept/generic/deictic or absent. That is acceptable for v1 under the named-only policy, but extraction reports should expose `facts_without_entity_link` and reason counts so the concept-admission tradeoff stays visible.
- Quote recovery landed, but instrumentation is still missing: add `exact_quote_count`, `fuzzy_snapped_count`, and `quote_reject_count` to validation reports before deciding whether mini is good enough or the extractor model needs to be stronger.
- Existing Phase-1 replay rows remain mostly untyped and primary-only until re-extraction or a one-shot backfill; new extractor payloads can now carry structured typed mentions.
- LLM disambiguation is wired for ambiguous exact/lexical candidates, but existing compound-name over-splits (`Sierra POC`→`Sierra`, `Databricks Content Layer`→`Databricks`, `Peter Wang`→`Peter`) need reprocessing, resolver configuration, or the Phase-4 gardener to reconcile.
- **Phase 3 landed:** `entity_merge` / `entity_split` actions re-point and restore `fact_entities.entity_id`, `facts.entity_id`, and entity status/aliases through reversible `cos_actions` inverses.
- **Phase 4 landed:** the existing nightly/page gardener now also proposes conservative `entity_merge` actions from active entity evidence. It handles compact spacing/punctuation duplicates (`Hightouch`/`High Touch`), lexical containment candidates (`Sierra`/`Sierra POC`) behind medium risk + optional LLM judgment, known type mismatch skips, failed-candidate suppression, and `entity_merge` payload/action features.
- R2 closed-vocab page routing / fuzzy page-dup gate is implemented and extraction-shadow verified; the remaining real-data issue is gardener LLM judgment timeout.

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

### Phase 2 — LLM disambiguation + structured mentions + `entity_type` — landed

Three things land together because they share one extractor upgrade — from "a single `model_entity_key`" to "a **list of structured mentions** (`surface`, `type`, span)":

**(a) LLM disambiguation tier (D4).** Only when Phase-1 resolution returns **multiple candidates** or a known alias-ambiguity (e.g. `Peter` vs `Peter Wang`, or `Sierra POC` vs the existing `Sierra`): prompt the `resolver` with the mention + context + the **closed candidate list** (id, name, type, description, aliases); it picks one **or** says "new" — it **never free-types an id**. This is what resolves the ~8 compound-name over-splits Phase 1 left behind. It's the first LLM in the resolution path — keep it on the ambiguous minority only (~1 obvious case in 171).

**(b) Structured mentions → co-occurrence signal.** The extractor emits all entity mentions per fact (not just the primary), writing the secondary `fact_entities` rows. Once secondary mentions exist, **co-occurrence becomes a resolution signal** — e.g. "UC" co-mentioned with "Databricks" resolves to *Unity Catalog*, not *University of California* — usable by both this tier and the Phase-4 gardener. This is the payoff of carrying the link table from Phase 1.

**(c) Populate `entity_type`.** The column already exists on `entities` (base schema), but Phase 1 left it **null for all 66**. Each mention now carries a `type`; `resolve_entity` writes it to `entities.entity_type` **on create**. LLM-proposed, deterministically stored. Set-once on create (under-change bias); systematic disagreements are reconciled by the Phase-4 gardener, not overwritten per mention. Existing null-type entities get typed on the next re-extraction (the corpus is regenerable) or via a one-shot backfill pass.

#### `entity_type` — goal & intended use
**Goal:** give every entity a coarse, **closed-vocabulary** kind so resolution and merge can reason about *what an entity is*, not just its string. Closed enum (no free-typing — same discipline as entity ids):

`person | organization | product | project | concept | place | event | other`

Used for, in order of value:
1. **Candidate blocking during resolution** — match a mention only against existing candidates of the **same type**, so the org "Sierra" never collides with a person named "Sierra". Narrows the candidate set deterministically *before* any LLM call (feeds D3 blocking).
2. **Merge safety (Phases 3–4) — the primary reason it matters.** A **type mismatch (person↔organization) hard-blocks a merge** candidate. Over-merge is the dangerous, hard-to-untangle direction; type is the cheapest deterministic guard against it.
3. **Disambiguation quality (this phase)** — type is shown in the closed candidate list so the resolver picks correctly, and the mention's type hint seeds the match.
4. **Routing / taxonomy (page-routing track)** — type informs default page placement (people → people pages, orgs → `companies/*`) and directly attacks the `career/*` vs `companies/*` fuzziness Codex found.
5. **Retrieval & salience (later)** — type-filtered queries ("all companies"), type-aware ranking.

**What it does *not* do:** type is *cross-kind* safety, not *same-kind* splitting. `Sierra POC` vs `Sierra` are both `organization`/`product`, so type won't separate them — that is the semantic job of the (a) disambiguation tier. Don't expect `entity_type` to clean up the compound-name residual on its own.

**Implementation notes:** `extraction.py` now accepts `entities` / `entity_mentions` while preserving the legacy `entity_key` fallback; deterministic validation rejects invalid mention types and derives mention spans when the surface appears in a cited chunk. `cos_actions.py` writes all resolved links to `fact_entities`, with `facts.entity_id` kept as the primary denorm. `entities.py` type-blocks exact/alias candidates, allows null-type legacy entities to be typed on first compatible match, and calls the `resolver` role only when more than one plausible candidate exists. If the resolver returns an unknown id, deterministic code ignores it and falls back instead of accepting a free-typed id.

### Phase 2.5 — Quality gates from the 2026-06-29 real-LLM sample

The first real-LLM Phase-2 run (Codex `gpt-5.4-mini`, 2 hyprnote docs → 18 facts → 21 entities; artifacts under `/private/tmp/pkm-entity-llm-sample-20260628-213226/`) confirmed the typed-mention path works for **named** entities (Peter→person, Databricks→org, Unity Catalog→product, with co-mentions) but exposed three quality problems. Each fix below states *how* and *why it works*.

#### Issue A — Generic/deictic mentions are minted as entities (the blocking issue)

*Evidence:* ~7 of 21 nodes are non-entities — `Our Team`, `Partner Team`, `Engineering Managers`, `Engineers`, `Product Marketers`, `End Users`, `Solution Architect Team`. It also poisons type: **4 of 7 `organization` entities are not orgs** (`Our Team`, `Partner Team`, `Product Marketers`, `Solution Architect Team`) — which would corrupt the Phase-3 merge-safety guard that trusts `entity_type`.

*Root cause:* `resolve_entity` creates a node for **every** mention. Two mention classes have no stable identity: **deictic** (`our team`, `the partner team` — speaker/context-relative; the *same* string denotes different teams across docs, so it can be neither merged nor kept-apart correctly) and **generic role-classes** (`engineers`, `product marketers` — common-noun plurals, not specific referents). This is the entity-layer form of the lesson the fact layer already learned with the claim-class gate: **provenance ≠ value** — `our team` has a real span and passes the substring gate, yet is worthless as an entity.

*Fix — an entity-worthiness gate (the claim-class gate, applied to entities):*
1. The extractor tags every mention with a closed **`mention_kind`**: `named | concept | generic | deictic` (separate from `entity_type`, exactly as `claim_class` is separate from the fact statement).
2. `resolve_entity` (`entities.py`) **creates/links a node only for `named`** (and `concept` per Issue B). `generic` and `deictic` mentions are dropped from the entity layer — the surface text **stays in the fact statement**, so no information is lost; it just doesn't mint a node.
3. **Bias under-create:** missing or low-confidence `mention_kind` ⇒ no entity.
4. Optionally persist `mention_kind` on the kept `fact_entities` rows (additive migration 019) so retrieval/gardener can distinguish a named entity from a concept tag.

*Why it works:* it puts the fix exactly where the defect is — node **creation** — and uses the LLM only for the linguistic judgment it is good at (named vs generic vs deictic needs sentence context), while deterministic code does the gating. It is the same shape as the claim-class gate, which already works in production, so we are reusing a proven pattern rather than inventing one. The under-create bias makes the failure mode "occasionally miss a borderline named entity" (recoverable by re-extraction or the gardener) instead of "a graph full of junk that corrupts merge-safety and retrieval." And because the 4 fake orgs are never created, the type distribution and the Phase-3 person↔org guard are clean for free — **one gate fixes both the sprawl and the type corruption.**

*Future (optional):* resolve deictic mentions to their concrete referent via coreference (`our team` → Peter's actual team) instead of dropping — defer; dropping is the safe default now.

#### Issue B — Concepts-as-entities is an accidental scope explosion (make it a deliberate knob)

*Evidence:* 10 of 21 nodes are `type=concept` (`Lineage`, `RBAC`, `ABAC`, `Data Observability`, `Privacy Automation`, `Agents`, …). Not junk like Issue A, but a large, sprawl-prone class admitted **by default, not by decision.**

*Fix — an explicit admission policy knob:*
- Add config `entity.admit_kinds` (in `cos_llm.yaml`), **default `[named]`**. A `concept` mention becomes an entity only when `concept` ∈ `admit_kinds`.
- Forward path (concepts without flooding): admit a concept as a full entity only once it **recurs across ≥N documents**, promoted by the Phase-4 gardener — so one-off technical nouns don't each become a node.

*Why it works:* the 10 concept nodes weren't chosen; they were a side effect of "every mention becomes an entity." A config default of `named` converts an implicit explosion into an explicit, reversible choice that directly serves the identity goal motivating this layer (Peter/Databricks/UC aggregation), while leaving a clean opt-in for topical retrieval later. Recurrence-gated promotion keeps the bias toward *fewer, higher-value* nodes — the same conservative posture as under-merge — so turning concepts on later still doesn't flood the graph.

#### Issue C — Quote fidelity: keep the gate, fix the recovery

*Evidence:* 5 of 28 final rejects (10 across retries) because `gpt-5.4-mini` **paraphrased the `evidence_quote`** instead of copying a source substring. The gate is behaving correctly (accepted quotes are verbatim transcript, disfluencies and all); the cost is recall.

*Fix — deterministic fuzzy-snap-to-source (do not loosen the gate):*
1. When `evidence_quote` is not an exact (whitespace-normalized) substring of the cited chunk, locate its best-matching window in that chunk (`SequenceMatcher` / token overlap).
2. If similarity ≥ **0.85**, **snap to clause/sentence boundaries, extract the true source substring, and persist *that*** as `evidence_quote` (compute spans from it). The model's paraphrase is discarded.
3. If similarity < 0.85, reject exactly as today.
4. Also strengthen the extractor prompt (few-shot "copy the quote character-for-character"); the `extractor` role is per-config, so a stronger model can be pointed at it if recall stays low.

*Why it works:* it separates two things the current path conflates — "the model must **author** the evidence" vs "the evidence must **be** a true source span." We only require the latter. Snapping derives the true span deterministically, so the gate's invariant — *every stored `evidence_quote` is a verbatim source substring* — is **preserved, in fact strengthened**: even when the model rewords, the persisted quote is guaranteed real source text. The 0.85 floor is the safety dial — benign normalizations ("approximately"→"about", removed disfluencies) recover, while genuine fabrications (low overlap) still hard-reject. Net: recall up, provenance integrity unchanged.

---

### Phase 3 — Reversible merge/split — landed

Implements D6. `entity_merge` / `entity_split` are `cos_actions` with inverses: merge re-points both `fact_entities.entity_id` **and** the `facts.entity_id` denorm from merged → canonical, records the old entity rows/link rows/denorms for the inverse, sets `entities.status='merged'` / `merged_into`, and folds source names/aliases into the canonical entity. Split consumes that inverse and restores exact prior links and entity rows. **Bias under-merge**; **type mismatch hard-blocks a merge** when both sides have incompatible known `entity_type` values; large/cross-type merges escalate through the existing topology policy gate.

### Phase 4 — Nightly entity-gardener — landed

Implements D5. The existing gardener now runs page topology and entity topology together. Entity candidates are generated from active `entities` + `fact_entities` + linked fact evidence, then pass through the same optional gardener LLM keep/drop judgment and `cos_actions` proposal path as page candidates.

Current deterministic signals:
- `same_normalized_name_or_alias` and `same_compact_name_or_alias` (`Hightouch` vs `High Touch`) → low-risk `entity_merge`.
- `name_containment` (`Sierra` vs `Sierra POC`) → medium-risk `entity_merge`, intended for LLM/critic review rather than blind merge.
- near-name similarity with shared source/fact-token evidence → medium-risk `entity_merge`.
- known incompatible `entity_type` pairs are skipped before action proposal; the Phase-3 apply-time type guard remains the hard backstop.

Embedding similarity is still gated on a real encoder (see caveats); lexical+alias works now. Cold-start sprawl is expected and this pass reconciles it through reversible actions.

### Phase 3–4 follow-ups (deferred; from the real-model runs)

Both are Phase-C (turn-autonomy-on) concerns, not blockers, but they belong with the landed merge/gardener code.

**F1 — Recalibrate entity-merge risk by signal certainty, not raw fact count. Landed 2026-07-05.** On the real 41-entity set the deterministic proposer found **2/2 correct merges** — `High Touch`→`Hightouch` (23 facts) and `Unity Catalogue`→`Unity Catalog` (11 facts) — and **both escalated to `high` → human solely because `large_topology = affected_fact_count >= 8`**, not because they were uncertain. That gate was designed for *irreversible* page topology; `entity_merge` is **reversible** (Phase 3, tested) and signal-scored, so escalating obvious merges on volume fills the review queue with "merge High Touch into Hightouch? [obviously yes]" — exactly the noisy backlog to avoid. Compact/exact-name signals (`same_normalized_name_or_alias`, `same_compact_name_or_alias`) now stay **low** (auto + sampled audit) regardless of fact count as long as they are not cross-type/cross-entity/type-mismatch merges; fact-count sensitivity remains for fuzzy signals (`name_containment`, `near_name`) that *can* be wrong. Implemented in `entity_merge_candidate`, `gardener_candidate_reasoning_effort`, `classify_action_risk`, and the promoted COS policy row `entity_merge_high_certainty_l1`; focused tests cover both the high-certainty bypass and the generic large-topology escalation.

**F2 — Confirm the gardener LLM discriminates (not rubber-stamps).** The gardener kept **25/25** reviewed candidates in both real runs and dropped none. Plausibly correct given how real the legacy dups are, but add a test that feeds a deliberately-bad candidate (two clearly-distinct same-type entities with only incidental token overlap) and asserts the LLM **drops** it — so "keep all" is a verified judgment, not a silent default.

### Phase 4 gardener hardening — decompose the judgment call (G1–G3)

*Motivated by the 2026-07-02 R2 shadow run, where the xhigh gardener LLM judgment timed out at 600s. The design is grounded in the real call path, not the symptom.*

**What the code actually does today** (`generate_gardener_candidates`, `gardener.py:73-113`): the deterministic proposer emits ~1,830 candidates, sorts by `candidate_sort_key`, then **hard-truncates to `max_candidates=25` at `gardener.py:90-91` — before any LLM sees them** — and `apply_gardener_judgment` sends all 25 in **one** `complete_json` call (`gardener.py:512`, ~16.5K tokens at `reasoning_effort=xhigh`). So two separate things are true: (a) the run is already ~99% deterministic-lead — 1,805 of 1,830 candidates are disposed of by a blind sort-truncation with **zero judgment**; (b) the 25 that do reach the LLM are decided in one all-or-nothing packet that timed out. The truncation is the bigger authority leak; the megacall is the visible failure.

**Two axes, kept separate.** *Authority* = who decides (deterministic rule vs LLM). *Invocation* = how the LLM is called (one board-meeting vs many small parallel calls). The 600s timeout is purely an **invocation** failure and says nothing about authority. Decomposing the call is what **enables** more LLM-led judgment — the board-meeting caps at ~25 and dies, whereas per-candidate parallel calls scale to hundreds — so it is *not* a move toward less LLM. Reject the tempting-but-wrong inference "the LLM timed out, so use it less / auto-apply the mechanical cases": those cases are **reversible** (Phase 3), so a wrong keep costs one `entity_split`, and re-entrenching deterministic auto-apply fights the very heuristics that produced the legacy page sprawl.

**Ownership invariant:** *propose* deterministic (cheap, exhaustive, ranked — keep it) → *dispose* LLM (where judgment belongs — expand it) → *commit* deterministic + reversible (non-negotiable). "LLM-lead" means the LLM **disposes**; it never proposes from scratch (lower recall, costlier) or mutates state directly.

- **G1 — Decompose + isolate the judgment call. Landed ✅ (verified 2026-07-03).** Replace the single `complete_json(gardener_judgment_prompt(all 25))` with a fan-out: one small judgment call per candidate (or a tiny same-type batch), packet = the candidate + only its two affected pages/entities + 2-3 representative facts per side. Run through a bounded worker pool (mirror the extractor's `max_workers`), **per-call timeout 60-120s**, merge results. **One timeout → that candidate becomes `needs_review`, not a whole-run abort** — today a single 600s stall kills all 25; that all-or-nothing failure mode is the most urgent fix here and is independent of the authority question. `xhigh` is not the villain — `xhigh`-on-a-16.5K-megapacket is; `xhigh` on a focused two-page packet is fine. *Implementation:* `apply_gardener_judgment` runs `mode: per_candidate` via `ThreadPoolExecutor` with `needs_review` isolation (`gardener.py:503-593,723`); shadow run judged 50 candidates (2× the old ceiling, via `judgment_limit`) with 0 timeouts / 0 errors at ~34s/call.
- **G1.1 — Make drops observable (new; from the 2026-07-03 run).** The first non-rubber-stamp run dropped **3/50** candidates — but `apply_gardener_judgment` discards dropped candidates counter-only (`gardener.py:565-567`): no candidate_key, no action_type, no rationale survives. We cannot distinguish "dropped 3 junk page-merges" from "dropped 3 correct merges," and the returned top-25 contained **0 of the 4 `entity_merge` candidates** with no way to tell dropped vs kept-but-truncated. A judgment layer whose negative decisions leave no trace cannot be post-hoc audited — which is the premise the whole auto-apply posture rests on. **Change:** record each drop (candidate_key, action_type, entity/page names, LLM rationale) in the gardener result (e.g. `llm_judgment.dropped` list) and persist alongside proposed actions; same for kept-but-truncated below the return cut.
- **G1.2 — Cost: tier effort, don't shrink the candidate set.** Gardener wall time was 429s because **all 50** candidates were judged at `xhigh`, yet 23/25 returned were `low` risk. The fix is the F1×G1 composition below — map risk signal → `reasoning_effort` per job (compact/exact-name → low effort; `name_containment`/`near_name`/`large_topology` → xhigh). Explicitly **not** the fix: cutting `judgment_limit` back down — that re-shrinks LLM authority (anti-G2) to save cost the effort tier saves anyway.
- **G2 — Lift / soften the top-25 cut.** The `[:max_candidates]` truncation at `gardener.py:91` is the real deterministic-authority leak, not the megacall. Once G1 makes calls cheap and parallel, raise `max_candidates` (or replace the blind sort-cut with a cheap LLM triage pass) so judgment reaches far more of the 1,830 instead of 25. This is the concrete lever for "more LLM-lead."
- **G3 — Keep commit deterministic + reversible (hold the line).** The one axis that does **not** move toward the LLM. Merges/splits commit only through `cos_actions.apply_entity_merge` / `apply_entity_split` with full inverse-capture and the person↔org type hard-block (`guard_entity_merge_types`). Reversibility is *why* G1/G2 can safely hand the LLM more authority; it is not a reason to hand it commit rights.

**Composes with F1/F2:** F1's risk tiers set each candidate's **effort tier** in G1 (compact/exact-name → low effort or a confirm-batch; `name_containment` / `near_name` / `large_topology` → single-candidate, high effort), so effort scales with ambiguity while the LLM stays in the loop everywhere. F2's discrimination test moves to the **per-candidate** call shape — feed one deliberately-bad candidate, assert `drop`.

### Separate track — Page routing quality (the "page drift" + fallback fix) — independent of identity

Do **not** fold this back into entity identity (re-conflating them is the original bug). This is about *where a fact renders*, not *what it's about*. **This is now the #1 blocker on output usefulness:** in the 2026-07-01 real-model run, **127/178 accepted facts (71%) fell back to `concepts/extracted-facts.md`** and only 51 auto-applied to real pages.

**R1 — Relevance-rank routing hints per window (landed).** `load_extraction_routing_hint_pool()` now loads a broad contract/wiki-page pool and each extraction window receives `ranked_extraction_routing_hints()` for its own document/window text, rather than one global recency slice. The ranker is lexical today; semantic ranking improves once a real encoder lands (see Caveats).
*Why it works:* the model can only route to a page it is shown; putting the actually-relevant existing pages in front of it converts fallbacks into correct routes. This is the dominant lever for the 71% and is independent of the rest. (Lexical/FTS ranking already beats recency; semantic ranking improves once a real encoder lands — see Caveats.)

**R2 — Routing-target hygiene (landed; extraction-shadow verified).** R1 gave the model targets but exposed the next problem: in the 2026-07-02 full run, fallback fell to 6% yet **only 48% of applied facts landed on canonical pages — 52% went to reference/log pages** (96 to `references/agent_session_log/*`). Root cause: the routing-hint pool includes ~785 mostly-unmanaged reference/transcript pages, and those are *lexically closest* to source windows, so the relevance ranker surfaces them. The tell: `agent_session_log` is skipped as an extraction *source* but was the #1 routing *destination*. Implemented fixes:
- **Destination allowlist:** routing-hint pool = **managed canonical pages only** — exclude `references/*`, especially `references/agent_session_log/*`. (Data: 640 managed non-reference pages vs 785 near-unmanaged reference pages, so `managed=1 AND not-in-reference-namespace` removes the pollution without starving routing.)
- **Path normalization:** strip/normalize the `wiki/` prefix so the exclusion catches `wiki/references/…` (25 facts hit `wiki/references/spencer-…`).
- **Canonical-destination-required for auto-apply:** a fact routed to a reference page is held as residue (or re-routed), never auto-applied.
- **New-page path (the old R2):** if no managed canonical page matches, allow a *new* canonical page only behind a **fuzzy-dup check** (catches `career/2026-agent-pm-role-notes.md` vs `career/agent-pm-role-notes.md`) — never a reference page.
- **Route metrics:** validation reports now expose canonical/fallback/invalid/existing/new/snapped route counts so shadow runs measure route quality directly.
- **Verification:** `/private/tmp/pkm-r2-shadow-fixed-xmAxQp` corrected shadow run: 234 accepted facts, 218 canonical routes (93.16%), 16 fallback/unrouted, 0 reference/log originals, 212 existing-canonical + 3 fuzzy-snapped-existing + 3 new-canonical routes.
- *Note:* reference/log pages are **regenerable, ongoing projections** of `agent_session_log` sources — deleting them is futile (they return on next ingest); the fix is exclusion here, not deletion.

**R3 — Legacy page consolidation (so targets are clean).** The 2026-07-01 gardener found **1,035 `page_merge` candidates** — the legacy wiki is full of near-duplicate pages, which *also* degrades routing (dup targets muddy the hint set). Consolidate via the page/entity gardener, but as a **one-time supervised migration** (large-topology → human), not nightly autonomy. **If the from-source regeneration (`docs/cos-regeneration-tasklist.md`) is done, R3 is largely moot** — the managed pages are wiped and rebuilt clean, so there is no legacy sprawl left to consolidate.

- Together these address the run's 127 fallbacks + the legacy dup backlog.
- Build on existing helpers: `load_extraction_routing_hint_pool` / `ranked_extraction_routing_hints`, `canonicalize_fact_routes` (`wiki_facts.py`), `route_facts_to_sections` (`wiki_facts.py`), `DEFAULT_FALLBACK_PAGE_HINTS` (`extraction.py`).

---

## Caveats (be honest about these)
- **Semantic candidate generation depends on the embeddings work.** With hash embeddings, Phases 1–2 do lexical + alias matching only, so semantic-variant mentions ("the streaming company" → Netflix) won't link until a real encoder exists (separate embeddings-productization workstream). Lexical+alias ER works now and is most of the value.
- **Cold start:** first ingest creates many provisional entities, many duplicates → expected; the Phase-4 gardener reconciles. Don't treat early sprawl as failure.
- **Don't let merge auto-apply for large/cross-type entities.** Type mismatch (person↔org) blocks a merge candidate outright; large merges → human.

## Non-goals
- No OWL/RDF/SPARQL/reasoner in the runtime (optional RDF export only, later, if interop is needed).
- No free-typed entity identity **or `entity_type`** from the model — the entity guess is a *mention* to normalize + resolve (never a trusted id), and `entity_type` is the closed enum (`person|organization|product|project|concept|place|event|other`).
- No synchronous "perfect" ER at ingest — provisional link at write, reconcile later.
- No writing to `relations` / typed edges yet.
- `fact_entities` is the source of truth for fact↔entity links. New structured extractions can populate secondary mentions; legacy and Phase-1 replay facts may remain primary-only until re-extraction/backfill. `facts.entity_id` is a denormalized cache of the primary link, not a second source of truth.

## Code touchpoints
`migrations.py` (`_migration_018_entity_identity` — extend `entities`, create `fact_entities`, add `facts.entity_id`) · `db.py` (`entities`, `fact_entities`) · `extraction.py` (`entities` / `entity_mentions` payload, closed `entity_type`, mention spans, fallback routing) · `entities.py` (`normalize_entity_name`, type-blocked `resolve_entity`, closed-list resolver disambiguation) · `wiki_facts.py` (`entity_key_for_change` stays = page route; resolution grouping uses primary `entity_id`) · `cos_actions.py` (fact write path resolves primary + secondary mentions; `entity_merge`/`entity_split` landed with reversible inverses) · `gardener.py` (page gardener + entity-gardener candidate generation/judgment/action proposal) · `cos_policy.py` (entity merge/split use the existing topology large/cross-type risk tier) · **Phase 2.5:** `extraction.py` (emit `mention_kind`; quote fuzzy-snap-to-source in the validation/retry harness), `entities.py` (entity-worthiness gate — create only `named` + admitted `concept`), `cos_llm.yaml` (`entity.admit_kinds`, default `[named]`), optional migration 019 (`fact_entities.mention_kind`) · `tests/test_entities.py` · `tests/test_cos.py` · `tests/test_gardener.py`.

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
- (Phase 2) Each created entity gets a closed-enum `entity_type`; a person-mention and an org-mention sharing a surface string do **not** cross-link (type blocks the match).
- (Phase 2) A multi-entity fact writes >1 `fact_entities` row (secondary mentions), enabling co-occurrence resolution.
- (Phase 2.5) `our team` / `engineers` (deictic / generic-role mentions) create **no** entity, yet the surface text remains in the fact statement; a missing `mention_kind` ⇒ no entity (under-create).
- (Phase 2.5) With `admit_kinds=[named]`, a one-off `concept` mention creates no entity; adding `concept` to `admit_kinds` makes it an entity.
- (Phase 2.5) A paraphrased `evidence_quote` with ≥0.85 source overlap is snapped to the true source substring (stored quote is a verbatim span); <0.85 is rejected.
- (Phase 3) `entity_merge` re-points both `fact_entities.entity_id` and the `facts.entity_id` denorm; `entity_split` (revert) restores exact prior links; a person↔org (type-mismatch) merge is **hard-blocked**; large/cross-type merge → escalates.
- (Phase 4) Entity-gardener proposes merging `Netflix`/`Netflix Inc`; small merge auto-applies + is audited; large/cross-type merge → human residue.
