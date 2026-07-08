# CoS Fact Determinism — Refinements + Doc-Stamping Conventions

**Status:** note for Codex
**Last verified:** 2026-06-25 against working tree atop commit `73b1e90` (**uncommitted** — see §0)
**Author:** Claude

## 0. Current-state caveat (read first)

At time of writing, HEAD is `73b1e90` but the working tree has substantial **uncommitted** changes implementing gap-fix items G1/G2/R1 (contract conformance wired, gardener LLM judgment, real auditor). `docs/architecture-code-guide.md` already documents this uncommitted code. **Action:** commit the implemented work in logical groups, then re-stamp the affected docs against the resulting commit(s) per §3. Do not leave code-derived docs describing behavior that exists only in an uncommitted tree.

---

## 1. Deterministic fact operations — refinements

Context: the architecture guide states "fact insertion/updating/merging/status changes are deterministic once a candidate exists." That standard is correct, but it needs three sharpenings and one acceptance test. These refine — not reverse — the current design.

### 1.1 Separate deterministic *mechanics* from deterministic *decisions*
Two different things hide under "deterministic":
- **Mechanics** — applying a transition: union `source_ids`, set `supersedes_id`, write `status`, compute the inverse. This should *always* be deterministic. No change needed.
- **Decisions** — *whether* two facts merge, or *whether* A supersedes B. This is only safe to make deterministically when the decision is **truth-preserving regardless of semantics**.

**Action:** document this split in `docs/chief-of-staff-spec.md` (fact-resolution section) so future contributors don't add semantic judgments to the deterministic path by reflex.

### 1.2 Tighten the deterministic-merge "safe list"
A previous review filed "near-duplicate statements with strong lexical overlap" as safe-to-auto-merge. It is not. Lexical similarity is not semantic identity: *"Q3 revenue was $5M"* vs *"Q3 revenue target was $5M"* are high-overlap, opposite meaning. This is concrete in the code — `merge_similar_replacement_facts_with_actions` (`wiki_facts.py:809`) uses `SequenceMatcher(...).ratio()` (`wiki_facts.py:1075`) to decide merges.

**Standard going forward — only these are auto-safe deterministic merges:**
- normalized-exact statement match (`normalized_statement`, `wiki_facts.py:375`);
- adding new `source_ids` to an otherwise-identical claim.

Everything else (lexical near-dup, "probably the same", recency-implies-truth) is a **semantic decision** and must route to the existing escape hatches: `display_contested` / `open_questions` residue, or an LLM/critic path under CoS policy — never silent auto-merge.

**Action:** review `merge_similar_replacement_facts_with_actions` and reclassify pure lexical-ratio merges as residue-producing (or critic-gated) rather than auto-applied. Keep the merge *mechanics* deterministic; move the *decision* off the deterministic path. Add a test: two high-`SequenceMatcher`-ratio but semantically opposite statements must NOT auto-merge.

### 1.3 The stronger justification is reproducibility, not just auditability
Determinism is worth defending less for "blame boundaries" and more because it makes curation **idempotent and replayable** — which is the precondition for eval gates and the shadow→audit→promote loop to mean anything. An LLM-driven merge could not be evaled or replayed. State this justification in the spec so the rationale survives.

### 1.4 "Is a fact just a chunk?" is the fact layer's acceptance test
A fact only earns its place over a raw chunk if **both** hold:
1. **Provenance is lossless** — `source_spans` + `evidence_quote`, not just `source_ids`/doc-ids.
2. **Lifecycle actually changes retrieval** — superseded facts suppressed; conflicted facts surfaced as contested, not silently picked.

If neither holds, a "fact" is a lossy, possibly-hallucinated restatement of a chunk — strictly worse than the chunk. This is not hypothetical: legacy facts currently have `span_coverage=0.0` (the extraction eval fails on exactly this), so today's legacy facts *are* lossy chunks.

**Action:** treat this as a standing acceptance test for the fact layer:
- keep the extraction span-coverage eval as a **hard gate** (don't soften it; scope it to `extraction_method='llm'` per gap-fix R2 so it blocks new spanless facts while not penalizing legacy);
- verify retrieval lifecycle behavior with tests: a superseded fact is not returned as authoritative; a conflicted pair returns as contested. (Spec §5.9 already requires this — assert it.)

---

## 2. Architecture guide tweaks (`docs/architecture-code-guide.md`)

The guide is accurate and current. Two improvements:

1. **Stamp it** per §3 — it documents fast-moving code and will silently drift (it described uncommitted code this week).
2. **Promote real ceilings into "Major TODOs."** Today only embeddings productization is listed up top. Add:
   - `page_merge` / `page_split` / `rename_page` are proposable but **not implemented for application** (`ACTION_TYPE_SPECS` `implemented: False`) — the gardener can propose topology changes it cannot apply.
   - No active producer creates `wiki_page_syntheses` Markdown (storage + apply exist; no generator).
   These are already correctly described in the detailed sections; surfacing them in the TODO summary makes the current ceiling visible at a glance.

---

## 3. Doc-stamping convention (apply to all `docs/`)

Problem: code-derived docs cite `file:line` anchors and "current behavior" that drift across commits. A reader (human or agent) cannot tell whether a doc reflects the current tree. This nearly caused a wrong review this week.

**Convention — every doc under `docs/` carries a header block:**

```
**Status:** draft | canonical | note | historical
**Last verified:** <YYYY-MM-DD> against commit `<short-hash>`
```

Rules:
- **Code-derived docs** (architecture guide, audits, specs/plans citing `file:line`) **must** record the commit they were derived from / verified against. `file:line` numbers are only interpretable relative to that commit.
- Prefer **symbol names as the stable anchor**, with line numbers as a convenience (e.g., `merge_similar_replacement_facts_with_actions (wiki_facts.py:809)`), since symbols survive line drift.
- **Commit code first, then stamp.** Never stamp a code-derived doc against a HEAD that predates the behavior it describes (the current architecture-guide situation). If a doc must describe uncommitted work, mark it `(uncommitted)` like this note does, and re-stamp after the commit lands.
- **Re-stamp on material edit:** when you change a doc's claims, re-run the relevant verification (`pytest` / `ruff` / `brain eval` / `rg`) and update `Last verified` to the current `git rev-parse --short HEAD`.
- Fill the hash with `git rev-parse --short HEAD`.

**Action:** add the header block to existing docs (start with `architecture-code-guide.md`, `chief-of-staff-spec.md`, the audit/remediation docs), and add a short "Docs conventions" section to `CONTRIBUTING.md` so this is enforced going forward.

---

## Summary of actions for Codex
1. Commit the uncommitted G1/G2/R1 work in logical groups; re-stamp affected docs (§0, §3).
2. Move semantic merge decisions (lexical-ratio) off the deterministic auto path into residue/critic; keep mechanics deterministic; add the opposite-meaning test (§1.2).
3. Document the mechanics-vs-decisions split and the reproducibility justification in the CoS spec (§1.1, §1.3).
4. Keep span-coverage a hard gate (scoped to LLM facts) and assert retrieval lifecycle behavior (§1.4).
5. Stamp the architecture guide + promote unimplemented-topology and missing-synthesis-producer into its Major TODOs (§2).
6. Adopt the doc-stamping header across `docs/` and record it in `CONTRIBUTING.md` (§3).
