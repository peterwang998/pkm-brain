# Chief-of-Staff Retrieval Tuning Plan

**Status:** historical improvement plan; retrieval contract/current behavior now live in `docs/chief-of-staff-spec.md` and `docs/chief-of-staff-retrieval-contract.md`
**Last verified:** 2026-07-03 against commit `604e3f1`
**Trigger:** Codex implemented `docs/chief-of-staff-spec.md`; a retrieval quality check (20 sampled historical queries + 3 negative controls, seed 20260624) showed good recall but specific spec misses.
**Date:** 2026-06-24

## 1. Framing

The quality check Codex ran *is* the retrieval eval suite the spec calls for (§6), run once by hand. **Highest-leverage move: formalize it into a repeatable, gated suite first**, then fix against it. Otherwise every fix below is tuned by vibes and we can't tell regressions from improvements.

All fixes are retrieval-layer (mostly `service.py`); none require the autonomy machinery (actions/policy/gardener). This work is independent and shippable now.

## 2. Findings → root cause (grounded in code)

| # | Finding | Root cause |
|---|---|---|
| R1 | Every query returned exactly 8 facts, incl. unrelated; negative controls still returned fact rows | `search_facts(query, limit=8)` (`service.py:1532`, called `:1164`) has **no score floor** — chunks/pages/memories all have one (`:164–170`); facts are fixed top-k with no relevance gate |
| R2 | Confidence over-optimistic, esp. broad/meta (current query = 1.0 but wrong intent) | `retrieval_assessment` called **without facts** (`:1188`); verdict is an **OR over max layer scores ≥ floor** (`:2461–2464`) — strong vocabulary match ⇒ "found", no intent/margin/noise check |
| R3 | Agent-session traces dominate broad/meta results (current query 7/8 noisy) | Noise penalties (`:2274–2292`) are a flat 4/12; too weak vs strong lexical match on meta queries; no per-source-type cap in the final packet |
| R4 | Source-hit rate only 60% | Partly a **metric artifact**: `suppress_chunks_covered_by_facts` (`:1166`) intentionally removes the source chunk a returned fact already covers; the check counted raw chunks, not provenance-reachable sources. Possible real recall gap underneath. |
| R5 | Pages over-returned (7.65 avg); raw reference pages dominate (6/8) | `MANAGED_WIKI_BOOST=+8` (`:172`) insufficient to rank managed/semantic above raw reference; no tight page cap; raw reference not floored separately |
| R6 | One negative control ("mango orchard…") returned `partial` on unrelated evidence | `partial` vs `no_strong_match` boundary too loose; same OR-gate weakness as R2 |

## 3. Workstream 0 (foundational): retrieval eval suite

Turn the one-off check into a ratchet. Add `brain eval run --suite retrieval`.

- **Fixtures:** persist the 20 sampled historical queries + 3 negative controls as fixed golden cases; grow to ~50. Store expected verdict labels (found/partial/no_strong_match) and, where known, the grounding source IDs.
- **Anti-poisoning:** fixed synthetic negatives are eval artifacts, not evidence. The agent-session indexing sanitizer must redact exact negative-control strings from captured logs/reports so discussing or running the suite cannot make them retrievable facts later.
- **Metrics:**
  - verdict accuracy vs labels
  - source-hit rate (**redefined** — see W4)
  - fact precision = relevant returned facts / total returned facts
  - confidence calibration error (binned predicted-confidence vs actual found-rate; report ECE)
  - noise rate = session-trace chunks / returned chunks
  - negative-control pass = verdict `no_strong_match` **and** zero facts returned
- **Gate:** subsequent changes must not regress these; tie to the spec's eval-gate concept.
- Touchpoints: `evals/`, `cli.py`. Reuse Codex's two JSON reports as the seed dataset (`/private/tmp/brain_retrieval_sample_20260624.json`, `/private/tmp/brain_negative_controls_20260624.json`).

## 4. Prioritized workstreams

Order follows Codex's stated priorities, adjusted for dependencies (W1+W3 must precede W2 — you can't calibrate on ungated, noisy inputs).

### W1 — Fact relevance gating (highest priority; fixes R1, half of R4/R6)
- Add `FACT_SCORE_FLOOR` (seed near the chunk/page floor of 12; calibrate via the suite). In `search_facts`, drop facts below the floor → **dynamic 0..N**, not fixed 8.
- Enforce `status='active'` and a `min_truth_confidence` gate (spec already wants active-only — verify it's applied).
- Knee cut: after fact rerank, if there's a large score drop after rank j, cut the tail.
- **Negative-control fix falls out for free:** if no fact clears the floor, return `[]`. (Directly fixes "no_strong_match still returned fact rows.")
- Touchpoints: `service.py:1532`, `:1164`.
- Acceptance: returned-fact count varies by query (no constant 8); negative controls return 0 facts; fact precision ↑ on the suite.

### W2 — Confidence calibration (fixes R2, R6)
- Feed facts into `retrieval_assessment` (currently excluded, `:1188`/`:1117`) so strong fact / managed-page matches raise confidence and weak-only matches don't.
- Replace the OR-over-max-score verdict (`:2461–2464`) with a **calibrated function**:
  - weight high-trust layers (active memory, managed page, floored fact) above raw chunk / session matches;
  - require a **margin** (top score meaningfully above the field), not just ≥ floor;
  - **penalize breadth/noise:** high score dispersion + low margin ⇒ broad/meta ⇒ lower confidence; cap confidence when the packet is dominated by session traces or has no source hit.
- Calibrate thresholds against the suite (binning or isotonic/Platt): confidence ~0.9 should mean ~90% found.
- Tighten the `partial` vs `no_strong_match` boundary (R6).
- Touchpoints: `retrieval_assessment` (~`:2440–2470`), `retrieve_context`.
- Acceptance: calibration error ↓; broad/meta no longer 1.0 on missed intent; "mango orchard" control → `no_strong_match`.

### W3 — Noise suppression for broad/meta (fixes R3)
- Strengthen/adapt the agent-log penalty (`:2288–2292`): scale with query breadth/meta-ness rather than a flat constant.
- Add a **per-source-type cap** in the final packet (analogous to `MAX_CHUNKS_PER_DOCUMENT`): e.g. ≤1–2 `agent_session_log` chunks unless `agent_query`.
- Broad/meta detection: reuse the existing `specific_matches` signal (`:2426`) — low specificity ⇒ harden suppression + lower confidence (feeds W2).
- Touchpoints: `chunk_noise_reasons`/penalty (`:2274–2292`), packet assembly.
- Acceptance: noise rate ↓ on broad queries; current-query-type returns mostly managed pages/facts, not 7/8 session traces.

### W4 — Source grounding (fixes R4)
- **Fix the metric first:** count a source-hit if the grounding source is present as a chunk **or reachable via a returned fact/page provenance chain** (`fact.source_spans → chunk → document`). Dedup (`:1166`) deliberately removes covered source chunks, so a chunk-only metric undercounts.
- Re-measure. If still low, raise source recall: when a fact/page is the top answer, ensure its grounding document is represented — e.g. surface one best grounding chunk per top fact for verification.
- Touchpoints: eval metric; `retrieve_context` packet shaping.
- Acceptance: redefined source-hit ≥ target (~85%); report metric-fix gain vs recall-fix gain separately.

### W5 — Page selection (fixes R5)
- Prefer managed/semantic pages over raw reference: raise effective managed boost or floor raw-reference pages separately; lower the page cap (`max_wiki_pages`).
- Touchpoints: `select_wiki_pages` (~`:1686`), `WIKI_PAGE_SCORE_FLOOR` (`:166`), `MANAGED_WIKI_BOOST` (`:172`).
- Acceptance: managed pages preferred; avg page count down from 7.65; raw reference capped.

## 5. Sequencing

```
W0 (eval suite)  →  W1 (fact gating)  →  W3 (noise suppression)  →  W2 (calibration)  →  W4 (grounding) + W5 (pages)
```
W1+W3 first because calibrating confidence (W2) on ungated, noisy inputs is wasted. W4/W5 are largely independent and can run in parallel after W2.

## 6. Spec implications (recommend; do not silently change)
The findings expose under-specification in spec §5.9: it said "return facts directly when best" but never specified a fact score floor, dynamic-k, or that the verdict/confidence must incorporate facts. Recommend tightening §5.9 + §6 to require: a fact relevance floor, calibrated confidence (with ECE as an eval metric), and the per-source-type noise cap. Hold for your approval.

## 7. Open questions for Codex
1. What is `search_facts`'s current scoring basis (FTS/vector/reranker)? Floor calibration depends on whether fact scores share the chunk/page scale (~12) or a different one.
2. Should a returned fact always carry one grounding chunk in the packet (helps W4 + verification) or stay reference-only with provenance pointers (keeps the packet lean)?
3. Broad/meta detection: extend `specific_matches`, or add an explicit query-specificity classifier?
4. Calibration method: simple binned thresholds vs isotonic/Platt fit against the eval suite?
```
