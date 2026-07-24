# Public Gmail Temporal Review-Beta Evaluation — 2026-07-23

**Decision:** pass for an opt-in, review-only personal beta; not evidence for private-mail production promotion or automatic application

## Outcome

The sealed public-synthetic V4 replay recovered every intended temporal member, both multi-artifact structures, and every required canonical event title. It produced no selected hard negative and no forbidden temporal binding. The remaining misses in the original score were caused by coupling semantic correctness to the verifier's `supported` versus `uncertain` confidence label.

Score v10 separates those estimands. An exact artifact now counts toward semantic recall and review-artifact precision regardless of which review lane it occupies. Only a supported artifact matching supported gold earns supported-precision credit, and only supported output confirms a supported-gold member. This preserves the system's recall bias without converting uncertainty into trust.

The resulting recommendation is deliberately narrow: the temporal sidecar may be used as a personal review queue. It may not silently update facts, reminders, calendar state, event identity, or Chief-of-Staff actions.

## Evaluation Boundary

- Prediction code freeze: commit `2382f4f` (`Stabilize Gmail temporal identity and recall scoring`).
- Cohort: 12 public-synthetic `example.test` cases: eight positive and four negative.
- Gold: 11 temporal members, including nine supported-calibration members, two uncertainty members, two complete structural groups, and eight required canonical event titles.
- Frontier: 22 deterministic candidates across nine candidate-bearing cases; three cases were zero-work before any model call.
- Prediction provider: external Codex `gpt-5.6-luna`, medium reasoning, ephemeral and read-only. The independent evaluator was `gpt-5.6-sol`, medium.
- Execution: three logical prediction runs, 15 external calls total, with gold unavailable until the prediction seal existed.
- Privacy: no private Gmail content entered this benchmark or the final external audit.
- Evaluation mode: `development_replay`. A public diagnostic call was used while resolving a transient CLI-startup failure, so this run does not claim blind first use.

The first sealed attempt retained its failed receipt after the external CLI failed during initialization. The second attempt completed without reusing partial predictions. The v10 score is an authenticated rescore of that immutable second attempt; the plan, prediction seal, model calls, components, and result bytes were not rerun or edited.

## Metrics

| Measure | Personal review-beta floor | Score v10 | Result |
| --- | ---: | ---: | --- |
| Effective semantic recall | at least 10/11 | 11/11 (100%) | Pass |
| Confirmed recall | at least 7/9 | 7/9 (77.8%) | Pass |
| Supported precision | at least 7/8 | 7/8 (87.5%) | Pass as diagnostic only |
| Review-artifact precision | at least 11/12 | 12/12 (100%) | Pass |
| Complete lifecycle/option groups | 2/2 | 2/2 (100%) | Pass |
| Canonical event-title recall | 8/8 | 8/8 (100%) | Pass |
| Selected hard-negative cases | zero | 0/4 | Pass |
| Forbidden or critical semantic bindings | zero | 0 | Pass |
| Unscored cluster-review escalations | zero for this challenge | 0 | Pass |

The strict all-perfect smoke remains false because it requires 100% confirmed recall and 100% frozen-label supported precision. Those are intentionally stronger than the review-beta floors.

## Root Cause And Correction

The V9 scorer rejected a semantically exact supported artifact whenever gold expected uncertainty. That one confidence mismatch removed the old reschedule endpoint from four downstream measures: effective recall, review precision, complete-reschedule recall, and canonical-title recall. The reverse mismatch was already treated differently: uncertainty could recover supported gold for effective recall. This asymmetry made the benchmark appear less structurally capable than the sealed output actually was.

Score v10 now:

- matches exact subject, relation, lifecycle, normalized value, and required structure independently of confidence status;
- keeps unsupported or missing-status artifacts ineligible for semantic credit;
- uses supported-versus-uncertain agreement only for confirmed recall and supported precision;
- reports the supported-precision numerator and overconfident artifacts explicitly;
- deterministically prefers a calibration-consistent artifact when duplicate semantic outputs exist;
- rejects gold members that differ only by calibration metadata; and
- excludes unlabeled cluster-review escalations from artifact precision while reporting them as unscored workload and failing the all-outputs-scored gate when present.

Candidate evaluation v2 and the private holdout's confirmed-member scorer now use the same separation: effective artifact precision and semantic recall are confidence-neutral, while supported precision, confirmed recall, overclaim counts, and critical calibration gates remain confidence-sensitive. The retained run-manifest schema stays at v1 because prediction evidence did not change; the evaluation result carries its own v2 version.

The immutable V4 gold has one known label defect. The old endpoint of the named reschedule is stated in an exact `rescheduled to ... from ...` frame, its deterministic repair records an exact endpoint role, and all three verifier runs returned `supported`. The frozen gold says `uncertain`. External Sol-medium review judged the source directly supportive under the verifier contract. The gold was not rewritten after predictions were inspected. Consequently, 7/8 supported precision is preserved as the frozen diagnostic but must not be cited as production calibration evidence.

## Independent Judge

The initial external `gpt-5.6-sol` medium audit classified semantic extraction as 11/11, structural groups as 2/2, canonical titles as 8/8, semantically valid artifacts as 12/12, and filtering as 0/4 negative cases selected. It identified the metric conflation as P1 and the lack of explicit gold for cluster-review escalations plus insufficient freeze-time confidence-label adjudication as P2 issues. A post-fix Sol-medium re-audit found no remaining P0, P1, or P2 defect and returned `PASS guarded personal review beta`. Score v10 resolves the metric and escalation-accounting defects; the frozen-label defect is disclosed rather than altered.

The judge recommended the same guarded release boundary used here: review-only personal beta, with any hard-negative selection, swapped reschedule endpoint, wrong concrete date or lifecycle, or unsupported cancellation promoted as active schedule treated as an automatic failure.

## Evidence Commitments

| Artifact | SHA-256 |
| --- | --- |
| V4 challenge | `c2316eca5125c6cb2217705920fb7226cc0c260e059de7b38943ffb89bc77cea` |
| V4 gold | `d663af5f6b6ebdd1615bb65c01325a35945e7c99f11afba690fe4b7808d801d9` |
| Prediction plan | `b34e76f1397fb22304288198e10216e6bbbf8c2bcdc1dafd86ccd2716d32c820` |
| Prediction seal | `e94937737d22f88671f1ff419f06b8ab154c3ed1506efee33648b8e50b1232f1` |
| Immutable result | `cf3b7e8427e7944691a5ae21e39568f3b84da3324b8534e8e494527748c78759` |
| Score v10 | `ef3e2e14921191db2b46e5447a19f5cf50fb05f1bf8e5488c950a27754cdbc58` |
| Original prediction launcher | `866ad3bdb8ab231bbaed48c2b7f3b3cca465589132597d0052bb7f198188dccf` |
| Score v10 implementation | `bf4364d0a7de2c950aa27be23e1cc005a9b567567c403799556df895503db78f` |
| Final external Sol-medium report | `dffae1cc6f76de87501c921e59f12a9497d9d028097852aa256abb725d8a5639` |

The score records `prediction_launcher_trust_basis=exact_prior_launcher_artifact`, `prediction_launcher_exact_artifact_verified=true`, `gold_opened_after_this_prediction_seal=true`, and `release_eligible=false`.

Repository validation after the v10 and candidate-evaluation-v2 changes passed 59 focused public-challenge tests, 165 focused candidate/holdout tests, and the complete 2,526-test Python suite. Ruff lint and scoped formatting/diff checks passed. The unrelated modified app files were excluded from this milestone.

## Release Limits

This result does not establish general Gmail production quality. The cohort is small, synthetic, development-overlapped, and outside the user's private-mail distribution. The run does not validate live Gmail refresh, message-unseen updates to existing threads, persisted event-identity reconciliation, downstream retrieval use, reminders, or Chief-of-Staff routing. The public run also records `independent_invocations_verified=false`; its three runs and 15 external calls are sealed and stable but are not independently provider-attested.

Before automatic application, each eligible deterministic class still needs at least 99% observed supported-time precision with zero critical errors on separately frozen evidence. Before a broader personal-production claim, the existing private natural/capability holdout and bounded live canary remain required. The current result justifies using the temporal output as a visible, reversible review aid while collecting that evidence.
