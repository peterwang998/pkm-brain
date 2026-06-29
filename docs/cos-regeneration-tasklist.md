# CoS Autonomy: Commit → Validate → Regenerate — Task List

**Status:** task list for Codex
**Last verified:** 2026-06-26 against working tree atop `73b1e90` (working tree dirty, 260 tests green, autonomy engine never run on real data)
**Context:** `docs/chief-of-staff-autonomy-activation-spec.md` is implemented but uncommitted; existing corpus is 2,625 legacy facts with **zero** source spans; new LLM authoring path has produced 0 facts / 0 `cos_actions`.

**Hard rules:**
- Do **not** regenerate/purge before Phase 0–2 are done. Raw sources in `~/brain/raw/` are the source of truth and must never be deleted.
- The destructive step (T10) requires explicit human go-ahead and a verified backup.
- Preserve the only irreplaceable state: 40 `confirmed_by_user` facts + 46 answered + 11 dismissed open_questions.

---

## Phase 0 — Safety / rollback (do first; blocks everything)

- [ ] **T0.1 — Commit the autonomy implementation in logical groups.** The entire activation (≈14 modules + new `synthesizer.py`) is uncommitted on a 2-day-old base. Group e.g.: per-role provider config; resolver+extraction spans; synthesizer; topology apply; policy/risk-tiering; nightly dispatch; evals; docs. Verify `uv run pytest -q` (260) and `uv run ruff check .` green before/after.
  - *Acceptance:* clean working tree; `git log` shows the autonomy commits; tag a baseline (e.g. `cos-autonomy-impl`).
- [ ] **T0.2 — Stamp all `docs/` per the doc convention** now that a commit exists (`docs/cos-determinism-and-doc-conventions.md` §3): `**Last verified:** <date> against commit <hash>`. Add the "Docs conventions" section to `CONTRIBUTING.md`.
  - *Acceptance:* every `docs/` file carries a stamp; convention recorded.

## Phase 1 — Validate the new path on a sample (it has never run)

- [ ] **T1.1 — Configure providers** for `extractor` + `resolver` (+ `critic`, `auditor`) in `config/local/cos_llm.yaml`; cloud default. Verify `brain cos providers`/doctor reports them and enforces proposer ≠ critic/auditor.
  - *Acceptance:* role→provider resolution works; separation-of-duties validated.
- [ ] **T1.2 — Run authoring on a 5–10 doc sample** (meetings/notes only) via `brain cos run` or scoped extraction. Inspect created facts.
  - *Acceptance:* facts have `extraction_method='llm'` + non-empty `source_spans`; `evidence_quote` faithful to the cited span; `cos_actions` rows recorded.
- [ ] **T1.3 — Validate resolver judgment** on the sample: merges are semantically correct (no lexical over-merge); genuine contradictions → `display_contested`/residue, not auto-picked.
  - *Acceptance:* the opposite-meaning high-overlap test passes; sampled merges spot-checked correct.
- [ ] **T1.4 — Run the eval bundle**; confirm gates are real and green: extraction span-coverage (scoped to `extraction_method='llm'`), topology **real F1** (not smoke), conflict `false_truth_resolutions=0`, retrieval, routing.
  - *Acceptance:* evals pass on the sample, or remaining gaps documented.

## Phase 2 — Preserve irreplaceable human state

- [ ] **T2.1 — Export human-curated state** to a durable file: 40 `confirmed_by_user=1` facts + the 46 answered / 11 dismissed `open_questions` (with their answers/dispositions). Define how they're re-applied after regen (re-confirm matching regenerated facts; re-attach resolved conflicts).
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
T0.1 → T0.2 → T1.1 → T1.2 → T1.3 → T1.4 → T2.1 → T2.2 → T3.1 → T3.2 → **(human go-ahead)** → T4.1 → T5.1/T5.2.

## Verification bundle (run after each phase)
```bash
uv run ruff check .
uv run pytest -q
uv run brain eval run --suite extraction --home ~/brain   # scoped to llm facts
uv run brain eval run --suite topology   --home ~/brain   # real F1
uv run brain eval run --suite conflict   --home ~/brain
uv run brain eval run --suite retrieval  --home ~/brain
```
