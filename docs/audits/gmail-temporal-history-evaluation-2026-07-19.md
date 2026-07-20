# Historical Gmail Temporal Evaluation — 2026-07-19

**Status:** first promotion-readiness implementation tranche complete; cross-sectional gates passed and temporal association-recall gates failed

## Decision

The historical Gmail import is sufficient to replace the old evaluation-volume requirement and most of the waiting period. It is not sufficient to promote Gmail temporal cognition.

The full replay establishes connector/index integrity, current selection, source-admission parity, content-safe aggregate reporting, read-only behavior, and deterministic discovery. It also exposes a major recall problem: the current direct grammar recognizes only a tiny fraction of the threads that independent classifier proxies identify as temporally important. The next tranche must improve association recall in a sidecar/review-only lane and calibrate it against a small blinded human set before any new class becomes route-eligible.

Hard safety gates were not relaxed. Private-content leakage, unintended writes, nondeterministic replay, and wrong-occurrence routing remain zero-tolerance. The production Gmail Knowledge path remains disabled.

## Evaluation Boundary

- Input: the private projection directory only; the live Brain home and mailbox were not mutated.
- Scope: every retained projection file, latest-per-lineage current state, and deduplicated source history.
- Processing: deterministic local parsing only. No LLM received Gmail content during this replay.
- Output: aggregate counts and gate states only. No titles, bodies, addresses, provider IDs, raw expressions, local paths, or sample identifiers are recorded here.
- Baseline: the original Brain's 4.8% Gmail fact-eligibility rate.

## Corpus And Parity

| Measure | Result |
| --- | ---: |
| Projection files discovered / processed | 42,533 / 42,533 |
| Invalid or non-Gmail files | 0 |
| Renderer/classifier variants collapsed | 28,306 |
| Unique source revisions evaluated | 14,227 |
| Opaque thread lineages | 7,125 |
| Current active / deleted threads | 6,960 / 165 |
| Current fact-eligible threads | 356 (5.11%) |
| Difference from original Brain baseline | +0.31 percentage points |

Current selection matches the isolated Brain v2 database exactly: 6,960 active, 165 deleted, and 356 fact-eligible Gmail documents. This passes the configured two-percentage-point capability-parity gate. It establishes aggregate admission parity, not identical thread composition or downstream fact quality.

## Temporal Discovery

| Measure | Result |
| --- | ---: |
| Current revisions with a parsed candidate | 77 / 6,960 |
| Current candidates | 96 |
| Deadline / occurrence candidates | 81 / 15 |
| Day / exact precision candidates | 90 / 6 |
| Important-temporal proxy threads detected | 2 / 265 (0.75%) |
| Same-message explicit-date proxy threads detected | 0 / 288 (0%) |

The two coverage measures are classifier/heuristic proxies, not labeled recall. Their value is diagnostic: all 263 missed important-temporal threads also contained a temporal-form proxy and a same-message temporal event/action cue. Overlapping miss strata were 143 explicit-full-year, 185 inferred-year month/day, 180 numeric, and 176 relative/weekday threads; 37 exposed an ICS availability signal. Ninety-nine misses had the proxy only in the body and 164 had it in both subject and body.

This pattern points to association recall, not an absence of temporal expressions. Broadening date regexes alone would be the wrong fix. The next detector should inventory temporal expressions and event/action mentions independently, then expose an association mode such as direct grammar, structured artifact, subject singleton, or classifier-prior-only. `important_temporal` may prioritize unresolved review but may not invent or validate a relation, entity, or date pairing.

Candidate yield was also poorly aligned with source importance: 54 candidates appeared across 44 advertising/bulk revisions, 40 across 31 routine revisions, and only two across two fact-eligible signal revisions. This is not an advertising auto-application failure—the fact gate excludes ineligible mail, the sidecar is not integrated with persistence, and replay performed zero writes—but it confirms that discovery output is not yet useful enough for promotion.

## Historical Lifecycle Depth

| Measure | Result |
| --- | ---: |
| Adjacent source-revision transitions | 7,102 |
| Evidence-content-changing transitions | 0 |
| Threads with evidence-content variation | 0 |
| Candidate-assignment changes | 0 |

Most retained multiplicity represents renderer or classifier variation rather than changed message evidence. Historical replay can therefore replace cross-sectional volume and long passive waiting, but it cannot validate cancellation/reschedule ordering, duplicate active occurrences after a change, or incremental freshness. A bounded 72-hour live canary remains necessary once the cross-sectional and labeled gates pass.

## Gate Results

| Gate | Result | Assessment |
| --- | ---: | --- |
| Full historical coverage | 100% | Pass |
| Base fact-admission drift | +0.31 pp | Pass |
| Aggregate privacy assertion | No private fields emitted | Pass |
| Read-only / unintended writes | 0 writes | Pass |
| Detector replay nondeterminism | 0 differences | Pass |
| Important-temporal detection proxy | 0.75% vs 85% target | Fail |
| Same-message explicit-date proxy | 0% vs 90% target | Fail |
| Frozen calibration cohort | No ≥100-record fully labeled cohort yet | Not evaluated |
| Human temporal recall / supported-time precision | No labeled set yet | Not evaluated |
| Independent `gpt-5.6-sol` medium acceptance | No judge run in this tranche | Not evaluated |
| Critical occurrence/timezone/lifecycle errors | No gold labels yet | Not evaluated |
| Cross-occurrence errors | No gold labels yet | Not evaluated |

Unavailable gold metrics are not reported as zero errors. Exact deterministic replay is a structural result; it does not prove semantic correctness.

## Relaxed Promotion Plan

1. Keep the new discovery sidecar disconnected from routing and persistence.
2. Add independent expression and event/action mention inventories, structured ICS parsing, conservative association modes, and an unresolved review queue. Keep inferred-year, relative, unzoned, abbreviation-based, multi-event, cross-span, lifecycle, and classifier-assisted output review-only.
3. Freeze 100-120 HMAC-opaque records and their manifest fingerprint before annotation, with at least five members in every deterministic stratum: direct hits, important-temporal misses, explicit-proxy misses, human correspondence, lifecycle language, and advertising/bulk negatives. Require complete labels; sparse or selectively omitted labels remain not evaluated.
4. Require at least 85% human-labeled temporal recall, 95% supported-time precision, and zero critical errors for the review lane. Require at least 99% observed supported-time precision and zero critical errors separately for each narrow class considered for auto-application.
5. Use an ephemeral external `gpt-5.6-sol` medium audit only after the labeled set exists; never use classifier-derived proxies as judge truth.
6. After historical and labeled gates pass, run a 72-hour incremental canary that includes real content-changing revisions before controlled promotion.

This makes historical imports do the work they are genuinely good at while preserving a small, targeted live check for evidence they do not contain.
