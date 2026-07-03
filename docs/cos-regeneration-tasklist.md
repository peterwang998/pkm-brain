# CoS Autonomy: Commit → Validate → Regenerate — Task List

**Status:** task list for Codex
**Last verified:** 2026-07-02 against the dirty working tree.
**Context:** raw sources are the durable source of truth; facts, managed wiki pages, entities, actions, and indexes are derived and regenerable. The new LLM extraction path has now been exercised on real brain data in temp/shadow homes. It produced span-backed facts, clean named-entity links, and reversible action output, but the 2026-07-01 run also exposed a routing blocker: 127/178 accepted facts (71%) fell back to `concepts/extracted-facts.md` because routing hints were loaded once per run by recency rather than ranked per source window.

**Current pre-regeneration prerequisite:** R1 + **R2** page-routing fixes plus extraction H1b hardening are extraction-shadow verified; resolve the gardener LLM timeout before relying on the full gardener pass:
- R1 (landed): relevance-rank routing hints per extraction window, not one global `updated_at DESC` slice.
- **R2 (landed; extraction-shadow verified): routing-target hygiene.** R1 alone cut fallback to 6% in the 2026-07-02 run but exposed that **only 48% of applied facts routed to canonical pages — 52% went to reference/log pages** (96 to `agent_session_log/*`). The code now restricts the routing-hint pool to managed canonical pages, excludes `references/*` / `agent_session_log/*`, normalizes the `wiki/` prefix, blocks reference destinations from auto-apply, fuzzy-snaps near-duplicate canonical routes, and reports route-quality metrics. Corrected shadow run `/private/tmp/pkm-r2-shadow-fixed-xmAxQp`: 234 accepted, 218 canonical routes (93.16%), 16 fallback/unrouted, 0 reference/log routes, 212 existing-canonical + 3 fuzzy-snapped-existing + 3 new-canonical. See `docs/entity-layer-spec.md` R2.
- H1b: treat excessive `evidence_unit_ids` as truncate-not-reject; restrict number faithfulness to genuine quantities; keep entity faithfulness window-scoped or advisory.
- **Remaining caveat before rebuild:** the same shadow run produced deterministic gardener candidates, but the xhigh Codex gardener judgment timed out at 600s. Fix gardener prompt size/batching/timeout handling before treating the full gardener pass as production-ready.

**Hard rules:**
- Do **not** regenerate/purge before Phase 0–2 and the **R1/R2**/H1b shadow verification are done. Raw sources in `~/brain/raw/` are the source of truth and must never be deleted.
- The destructive step (T10) requires explicit human go-ahead and a verified backup.
- Preserve irreplaceable human state: confirmed-by-user facts, answered/dismissed `open_questions`, and any hand-authored/non-managed wiki pages that are not clean projections from raw facts.

**Scope — three buckets (from the 2026-07-02 analysis):**
- **Reference/log pages** (~785: `agent_session_log/*` + `references/*`): **do not delete** — they are regenerable, ongoing projections of `agent_session_log` sources and return on next ingest. Exclude them from *routing* (R2), not from disk.
- **Managed fact-projections** (~643): the regeneration target — derived, home of the ~1,053 dup-page sprawl; wipe + rebuild from raw. (Regenerating these makes R3 legacy-page consolidation moot.)
- **Hand-authored / curated** (~37 non-managed non-reference pages: financial-planning `open_loops/*`, `decisions/*`, `projects/*` ideas, brain-design `concepts/*`, `people/*`) + human state: **preserve** (T2.1).

---

## Phase 0 — Safety / rollback (do first; blocks everything)

- [x] **T0.1 — Commit the autonomy implementation in logical groups.** Checkpoint commits landed on 2026-07-03: `0fe315d` for entity/extraction/gardener implementation and tests; `604e3f1` for active extraction/entity/regeneration specs. Re-run `uv run pytest -q` and `uv run ruff check .` after later cleanup diffs.
  - *Acceptance:* clean checkpoint exists; `git log` shows the autonomy commits.
- [ ] **T0.2 — Stamp all `docs/` per the doc convention** now that a commit exists (`docs/cos-determinism-and-doc-conventions.md` §3): `**Last verified:** <date> against commit <hash>`.
  - *Acceptance:* active docs carry stamps; `CONTRIBUTING.md` records the convention. Older historical docs can be stamped opportunistically as they are touched.

## Phase 1 — Validate the new path on a sample (it has never run)

- [x] **T1.0 — Shadow-verify R1 + R2 + H1b before any rebuild.** Run a real-data shadow extraction after per-window routing, **routing-target hygiene (R2)**, and hardening are in code. Confirm **most facts route to canonical (non-reference) pages** — the 2026-07-02 run had 6% fallback but 52% reference-routing, so measure canonical share directly — evidence-unit cap retries disappear, and quantity/entity false rejects stay down.
  - *Result:* `/private/tmp/pkm-r2-shadow-fixed-xmAxQp/summary_salvaged.json`; 234 accepted / 281 raw, 218 canonical routes (93.16%), 16 fallback/unrouted, 0 reference/log routes, 38 total rejected, 9 dropped, no destructive action. Gardener deterministic counts recorded; gardener LLM judgment timed out.
- [ ] **T1.1 — Configure providers** for `extractor` + `resolver` (+ `critic`, `auditor`) in `config/local/cos_llm.yaml`; cloud default. Verify `brain cos providers`/doctor reports them and enforces proposer ≠ critic/auditor.
  - *Acceptance:* role→provider resolution works; separation-of-duties validated.
- [ ] **T1.2 — Run authoring on a 5–10 doc sample** (meetings/notes only) via `brain cos run` or scoped extraction. Inspect created facts.
  - *Acceptance:* facts have `extraction_method='llm'` + non-empty `source_spans`; `evidence_quote` faithful to the cited span; `cos_actions` rows recorded.
- [ ] **T1.3 — Validate resolver judgment** on the sample: merges are semantically correct (no lexical over-merge); genuine contradictions → `display_contested`/residue, not auto-picked.
  - *Acceptance:* the opposite-meaning high-overlap test passes; sampled merges spot-checked correct.
- [ ] **T1.4 — Run the eval bundle**; confirm gates are real and green: extraction span-coverage (scoped to `extraction_method='llm'`), topology **real F1** (not smoke), conflict `false_truth_resolutions=0`, retrieval, routing.
  - *Acceptance:* evals pass on the sample, or remaining gaps documented.

## Phase 2 — Preserve irreplaceable human state

- [ ] **T2.1 — Export human-curated state** to a durable file: `confirmed_by_user=1` facts + answered/dismissed `open_questions` (with their answers/dispositions) + hand-authored/non-managed wiki pages. Define how they're re-applied after regen (re-confirm matching regenerated facts; re-attach resolved conflicts; preserve non-managed pages unless explicitly superseded).
  - *Acceptance:* export exists; re-apply procedure written.
- [ ] **T2.2 — Back up the runtime brain** (`~/brain/db/brain.sqlite` + `~/brain/wiki/`) to a timestamped location outside the live home.
  - *Acceptance:* restorable backup verified.

## Phase 3 — Build the regeneration path (missing today)

- [ ] **T3.1 — Implement `brain cos rebuild-facts --from-sources`** (no full-corpus re-extraction command exists; nightly is `limit=10`). It should: purge **derived** facts/managed-page projections (never `raw/`), re-extract active documents via `extractor`, re-resolve via `resolver`, re-curate managed pages, and re-apply preserved human confirmations (T2.1). Support `--dry-run`, `--source-types`, `--limit`. Mirror the existing chunk-rebuild command's safety (`cli.py:519`).
  - *Acceptance:* `--dry-run` reports the plan (docs in scope, facts to purge, est. actions); a sample run produces span-backed facts and retains human-confirmed facts.
- [ ] **T3.2 — Scope & cost guardrails.** Default-exclude `agent_session_log` from fact extraction (noisy bulk); add per-run caps; surface provider/model + estimated cost before a full run.
  - *Acceptance:* agent logs excluded by default; cost/scope shown pre-run.

## Phase 4 — Execute regeneration (destructive; gated)

- [ ] **T4.1 — Full scoped regeneration.** After Phases 0–3 are green and with explicit human go-ahead + verified backup (T2.2): run `rebuild-facts` over the scoped corpus (meetings/notes/docs).
  - *Acceptance:* new corpus is `extraction_method='llm'` with ~100% span coverage; legacy facts archived (not silently dropped); managed pages regenerated from facts; retrieval lifecycle correct (superseded suppressed, conflicts contested); human confirmations re-applied; eval bundle green.

## Phase 5 — Verify autonomy posture (parallel-ok)

- [ ] **T5.1 — Confirm policy is actually promoted** (not all-L3): low/medium auto-apply with post-hoc audit; high → human. Verify the `large_topology_fact_threshold=8` gate forces large merges/splits to L3.
  - *Acceptance:* a low-risk action auto-applies + is audited; a large-topology action escalates to residue.
- [ ] **T5.2 — Confirm timeout-sweep never resolves a truth conflict into a winner** (topology/presentation only).
  - *Acceptance:* test asserts truth conflicts never auto-resolve on timeout.

---

## Suggested order
T0.1 → T0.2 → **T1.0** → T1.1 → T1.2 → T1.3 → T1.4 → T2.1 → T2.2 → T3.1 → T3.2 → **(human go-ahead)** → T4.1 → T5.1/T5.2.

## Verification bundle (run after each phase)
```bash
uv run ruff check .
uv run pytest -q
uv run brain eval run --suite extraction --home ~/brain   # scoped to llm facts
uv run brain eval run --suite topology   --home ~/brain   # real F1
uv run brain eval run --suite conflict   --home ~/brain
uv run brain eval run --suite retrieval  --home ~/brain
```
