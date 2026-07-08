# Chief-of-Staff — Autonomy Activation Spec

**Status:** historical activation plan; largely executed and superseded by `docs/chief-of-staff-spec.md`, `docs/extraction-payload-spec.md`, `docs/entity-layer-spec.md`, and `docs/cos-regeneration-tasklist.md`
**Last verified:** 2026-07-03 against commit `604e3f1`
**Goal:** turn the existing shadow substrate into an autonomous Chief of Staff that curates facts from ingested sources, flags hard conflicts for humans, authors wikis, performs low/medium-risk topology maintenance autonomously, and stops large topology changes for human review.

## 0. Decisions locked (from review, 2026-06-25)

- **Timing:** curation runs as a **nightly batch** over newly-ingested/changed sources. Ingest stays fast and offline-capable; the LLM is never on the ingest path.
- **Provider posture:** **per-role provider config**, cloud by default, each role independently overridable to a local (Ollama) model. Designed so high-volume roles can migrate to local later without touching call sites.
- **Autonomy:** **low + medium risk auto-apply; high risk → human.** Medium auto-applies *with* mandatory post-hoc audit.
- **Hard always-human gate:** **large topology changes only** (page merge/split above a blast-radius threshold). Conflict resolution, archives/retractions, and contract-scope edits are **risk-tiered, not blanket-human** — but genuine truth contradictions needing external ground truth compute as high-risk and therefore escalate.

## 1. Current state (recap)

As of 2026-07-03, this recap is historical. The action ledger, policy engine, guarded revert, role taxonomy, extractor, gardener, synthesizer, auditor stub/config path, page topology actions, entity merge/split actions, and nightly extraction/gardener/synthesis/timeout/audit stages are wired. Remaining live ceilings are tracked in the active specs: productized embeddings, resolver/critic promotion depth, MCP `open_questions` exposure, cleanup of archived legacy tables, and regeneration prerequisites.

## 2. Target operating model

Nightly autonomous run over changed sources:
```
ingest (deterministic, offline)
  → extract (extractor)         → fact_upsert actions
  → resolve (resolver)          → fact_merge / fact_supersede / resolve_conflict / display_contested actions
  → garden (gardener + judge)   → page_merge / page_split / rehome_fact / rename_page / edit_contract actions
  → synthesize (synthesizer)    → synthesize_page actions (non-canonical block)
  → for every proposed action: POLICY decides {auto-apply L0/L1, auto+audit L2, escalate L3}
  → audit (auditor, sampled)    → sampled_ok/bad, demote, guarded revert
  → residue (open_questions)    → human surface for L3 + hard-gated items
```
All writes go through `cos_actions`. Humans see only: large-topology proposals, genuine truth contradictions, low-confidence escalations, and critic/proposer disagreements.

## 3. Workstreams

### WS1 — Per-role provider configuration (foundational)
Everything else depends on this.
- Add a CoS LLM config (e.g. `config/local/cos_llm.yaml`) with a `default` provider/model and per-role overrides for `extractor`, `resolver`, `gardener`, `synthesizer`, `critic`, `auditor`.
- `complete_json(role=...)` resolves provider via: role override → default → (none ⇒ stage skips with `status=skipped`, reason logged). Preserve offline-safety: an unconfigured role never blocks the deterministic spine.
- Enforce separation of duties at config-validation time: `critic`/`auditor` must not resolve to the same provider+model as the proposer roles they review (warn or reject).
- Files: `llm.py` (provider resolution), new config loader, `cli.py` (a `brain cos providers` status/doctor command), tests.
- Acceptance: each role can be pointed at a distinct cloud or local provider; default-cloud works with zero per-role config; unconfigured role skips cleanly.

### WS2 — Wire the three unwired roles
**resolver** (replaces deterministic merge/supersede decisions):
- LLM judges, per `entity_key` group, whether facts are the *same claim*, a *clear supersession*, or a *genuine contradiction*. Emits `fact_merge` / `fact_supersede` / `resolve_conflict` / `display_contested` actions.
- Keep deterministic *mechanics* (union sources, set status, inverse); move the *decision* to the resolver. Only normalized-exact dup + source-addition stay deterministic-auto (see `docs/archive/cos-determinism-and-doc-conventions.md` §1.2).
- Classify output: clear supersession ⇒ low/medium risk (auto-eligible); genuine contradiction needing external truth ⇒ high risk ⇒ `display_contested` + escalate. Never timeout into a winner.
- Files: `wiki_facts.py` (`merge_similar_replacement_facts_with_actions`, `resolve_fact_groups`), new resolver prompt, tests (incl. opposite-meaning high-lexical-overlap must NOT auto-merge).

**synthesizer** (the missing wiki author):
- Generate the non-canonical synthesis block from active facts, cite `fact_ids`, store in `wiki_page_syntheses` with `fact_hash`/`prompt_version`. This is the absent producer the audit flagged.
- Files: new `synthesize_page` producer, `wiki_facts.py` render integration, tests (synthesis cites facts; deleting it leaves the canonical page valid; retrieval never cites it alone).

**critic** (separation-of-duties gate for auto-apply):
- For L1/L2 actions, an independent critic adjudicates before/at apply; disagreement → escalate to L3.
- Files: `cos_actions.py` decision path, `cos_policy.py`, tests (critic disagreement blocks auto-apply).

### WS3 — Implement topology apply
- Implement application for `page_merge`, `page_split`, `rename_page` (currently `ACTION_TYPE_SPECS implemented: False`) with ledger-level inverses, reprojection, and reindex. Reuse the guarded-revert + `applied_state_hash` machinery.
- Files: `cos_actions.py` (apply + inverse per type), `wiki_facts.py` (reprojection), tests (apply/revert round-trip; out-of-order revert refuses).
- Acceptance: every declared topology action either applies-with-inverse or is explicitly rejected; no `implemented: False` remains for the actions we intend to use.

### WS4 — Risk tiering + policy promotion (turn autonomy on)
Define `risk_tier` deterministically from action features, then promote policy:
- **low** → L0/L1 auto (L1 = proposer + critic): exact dedup, source union, canonicalize, small rehome, clear fact merge, clear supersession, `edit_contract` on a contract-less page, synthesis.
- **medium** → L2 auto **+ mandatory sampled audit**: non-trivial merges/splits below the large-topology threshold, medium-confidence resolutions.
- **high** → L3 human: **large topology changes (always)**, genuine truth contradictions, low confidence, critic/proposer disagreement.
- Promotion is a recorded policy **version bump**, gated on the relevant eval suite passing (WS6). No caller booleans.
- Define the **large-topology threshold** (e.g. affected_fact_count or affected_page_count ≥ N, or cross-entity merge) as a policy parameter, not a hard-coded constant.
- Files: `cos_policy.py` (risk function + seed promotion), `gardener.py`/`resolver` (emit risk features), tests.

### WS5 — Nightly orchestration over changed sources
- Extend `run_nightly_maintenance`: replace hardcoded `shadow=True` with policy-driven dispatch once WS1–WS4 land. Process only newly-ingested/changed sources via a per-document watermark (`content_hash + extractor_model + prompt_version`) to bound cost and avoid duplicate `fact_upsert` actions.
- Keep `cos_role` gating (primary-only curation) and honest per-stage status.
- Add an on-demand CLI (`brain cos run`) mirroring the nightly stages for manual/catch-up runs.
- Files: `automation.py`, `extraction.py` (watermark), `cli.py`, tests.

### WS6 — Eval upgrades (quality gates before promotion)
- **Topology**: upgrade from smoke (`candidate_generation_smoke=1.0`) to real precision/recall/F1 fixtures — required before any topology auto-apply.
- **Conflict**: fixtures distinguishing clear-supersession (auto) vs contradiction-needs-truth (escalate); keep `false_truth_resolutions=0` as a hard sub-metric.
- **Resolver/merge**: opposite-meaning lexical-overlap trap; merge precision.
- **Extraction**: scope span-coverage gate to `extraction_method='llm'` (gap-fix R2) so new facts must carry spans without legacy facts blocking the gate.
- Files: `evals.py`, `retrieval_fixtures.py`.

### WS7 — Human review surface + non-blocking backlog
- Surface in UI: L3 residue (large topology, contradictions, low-confidence), recent auto-applied actions, audit failures, policy version.
- Timeout-into-uncertainty for topology/presentation residue only; truth contradictions never timeout into a winner. Cap/prioritize the queue by impact so a slow human never blocks curation.
- Files: `ui_server.py`, `automation.py` (timeout sweep), tests (truth never auto-resolves on timeout).

### WS8 — Provenance acceptance test (fact ≠ lossy chunk)
- Extractor must emit `source_spans` + `evidence_quote`; enforce via the scoped span-coverage gate (WS6).
- Assert retrieval lifecycle: superseded facts not returned as authoritative; conflicts returned as contested pairs (spec §5.9).
- Files: `extraction.py`, `service.py`, tests.

## 4. Sequencing

```
WS1 providers → WS6 evals (topology/conflict/resolver real) → WS2 roles (resolver, synthesizer, critic)
→ WS3 topology apply → WS8 provenance gate → WS4 risk tiering + promotion → WS5 nightly dispatch → WS7 review/backlog
```
Rationale: providers unblock everything; evals must be real before promotion; roles + apply + provenance must exist before policy is promoted past L3; nightly dispatch flips on last; review surface hardens the loop. **Do not promote any action past L3 until its eval suite passes and WS3/WS8 are done.**

## 5. Invariants (must not regress)
- Every mutation goes through `cos_actions`; nothing auto-applies above L3 without a passing, non-skipped eval gate.
- Large topology changes always require human approval.
- Truth contradictions never auto-resolve into a winner and never timeout into one.
- Reverts are guarded at the ledger level; the deterministic spine never hard-depends on an LLM.
- Curation is primary-only; ingest stays offline-capable.
- Proposer ≠ critic ≠ auditor (provider/model separation enforced by config).

## 6. Definition of done (what "autonomous" looks like)
- A new source ingested today is, by the next nightly run: extracted into span-backed facts, merged/resolved by the resolver (clear cases auto, contradictions escalated), routed onto contract-governed pages, synthesized into a readable block, with low/medium topology cleanups auto-applied and audited, and large topology changes queued for you — all recorded as reversible `cos_actions`, all gated by passing evals.

## 7. Open questions (defaulted; flag to change)
- **Large-topology threshold** value (default: ≥ ~8 affected facts or any cross-entity merge — tune via topology eval).
- **Conflict auto-resolution scope**: default allows auto only for same-entity, same-or-higher-authority-source, explicit-recency supersession; everything else escalates. (Source-authority model still doesn't exist — until it does, "authority" means explicit user confirmation only.)
- **Cost guardrail**: per-run extraction cap + which source types to extract (default: skip `agent_session_log` bulk extraction unless query/agent-history-relevant, per existing source-aware weighting).

## 8. Verification bundle
```bash
uv run ruff check .
uv run pytest -q
uv run brain eval run --suite extraction --home ~/brain   # scoped to llm facts
uv run brain eval run --suite retrieval  --home ~/brain
uv run brain eval run --suite topology   --home ~/brain   # must be real F1, not smoke
uv run brain eval run --suite conflict   --home ~/brain
uv run brain eval run --suite routing    --home ~/brain
```
