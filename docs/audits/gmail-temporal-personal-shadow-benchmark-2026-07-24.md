# Gmail Temporal Personal-Shadow Benchmark — 2026-07-24

**Decision:** pass the pragmatic temporal-quality benchmark for a reversible,
review-only personal shadow; do not claim complete Brain V2 release readiness,
automatic promotion readiness, or production-evidence eligibility

## Outcome

The Gmail temporal extraction and review slice now clears the personal-use
precision, recall, and current-state stability targets. In a fresh three-run
public-synthetic development replay, it recovered all 88 intended temporal
members, including all 62 critical members, and every emitted review artifact
matched gold. No critical wrong date, event, timezone, or lifecycle binding was
observed in this 100-case replay.

That result supports a narrow operating decision: run the temporal sidecar as a
visible, reversible review shadow. It does not support silent fact mutation,
calendar or reminder creation, Chief-of-Staff action, or deterministic
auto-promotion.

The full personal-release benchmark remains incomplete because Original Brain
semantic parity and retrieval relevance have not been measured. The replay is
also explicitly a `development_replay` over public transformed fixtures, so it
is not evidence about private Gmail distribution quality and is not eligible as
production-release evidence.

## Pragmatic Personal-Use Benchmark

| Measure | Target | Observed | Result |
| --- | ---: | ---: | --- |
| Effective temporal recall | at least 90% | 88/88 (100%) | Pass |
| Critical temporal effective recall | at least 95% | 62/62 (100%) | Pass |
| Minimum critical category recall | at least 95% | 100% | Pass |
| Confirmed member recall | diagnostic | 76/82 (92.68%) | Disclosed |
| Supported artifact precision | at least 95% | 76/76 (100%) | Pass |
| Review artifact precision | at least 90% | 94/94 (100%) | Pass |
| Complete temporal-unit recall | at least 90% | 80/80 (100%) | Pass |
| Exact temporal-unit recall | at least 90% | 80/80 (100%) | Pass |
| Complete reschedule recall | at least 95% | 8/8 (100%) | Pass |
| Candidate-bearing negative rejection | at least 80% | 21/21 (100%) | Pass |
| Canonical subject recall | at least 90% | 63/63 (100%) | Pass |
| Canonical event-title recall | at least 90% | 55/55 (100%) | Pass |
| Critical wrong bindings or overclaims | zero | 0 | Pass |
| Semantic stability | at least 90% | 93.88% parent-cluster minimum | Pass |
| Critical current-state agreement | 100% | 100% | Pass |
| Original Brain semantic parity | at least 95% | unmeasured | Blocked |
| Retrieval top-5 / top-10 relevance | at least 90% / 95% | unmeasured | Blocked |

These gates compare point estimates with the agreed personal-use thresholds;
they do not establish population-level confidence. For example, the Wilson 95%
lower bound for 62/62 critical recall is 94.17%.

The 100% recall figures are effective semantic recall, not deterministic
confirmation. Six supported-gold members entered the uncertainty lane, so
confirmed recall is 92.68%. This distinction is intentional: a recall-biased
personal review queue should retain well-grounded uncertainty without treating
it as trusted current state.

## Evaluation Boundary

- Prediction code freeze: commit `bdffa01` (`Stabilize Gmail temporal fact
  recall`).
- Cohort: 100 public-synthetic cases, comprising 60 positives and 40 negatives.
- Frontier: 259 deterministic candidates across 81 candidate-bearing cases.
  All 60 positive cases were candidate-bearing; 19 negative cases correctly
  required no model work.
- Frontier reachability: all 88 gold members were reachable before verification.
- Prediction provider: external Codex `gpt-5.6-luna`, medium reasoning, three
  runs and 132 external calls.
- Prediction isolation: gold remained inaccessible until the prediction seal
  existed; no local model or test invoker was used.
- Independent evaluator: external Codex `gpt-5.6-sol`, medium reasoning, with no
  tools, network, memory, private Gmail, Brain database, or HMAC-key access.
- Privacy: the replay contained no private Gmail. The evaluator reviewed the
  supplied hash-bound aggregate evidence summary; it did not independently
  recompute hashes or rerun scoring, and no private artifacts were provided.
- Evidence class: public-synthetic `development_replay`, with
  `release_eligible=false` and
  `production_release_evidence_gate_passed=false`.

## What Improved

The final iteration removed deterministic ambiguity before model verification
rather than trying to recover precision by globally tightening the verifier.
The material corrections were:

- binding deadlines only to direct, bounded action/date grammar and eliminating
  action/date cross-products;
- making opening and closing expressions own their temporal bindings while
  removing redundant predicate and occurrence aliases;
- recognizing archived and forwarded history variants and preventing quoted
  schedules from overriding authored current state;
- separating cancellation lifecycle evidence from time normalization, while
  rejecting questions, rumors, denials, conditionals, and attributed claims as
  exact cancellations;
- preserving both endpoints of real reschedules without swapping old and new
  state; and
- aligning action vocabulary and candidate generation across discovery, lead,
  selection, frontier, and verifier stages.

The deterministic frontier fell from 306 to 259 candidates while retaining
88/88 gold reachability. The fresh replay emitted none of the prior 14 false or
redundant review artifacts.

## Stability And The Remaining Temporal Caveat

Across the three verifier runs:

- accepted gold-member minimum pairwise Jaccard was 97.73%;
- accepted parent-cluster minimum pairwise Jaccard was 93.88%;
- critical candidate minimum pairwise agreement was 96.36%; and
- exact raw-candidate minimum agreement was 93.82%, reported as a diagnostic.

Critical current state was fully stable in every run pair:

| Category | Stable members | Agreement |
| --- | ---: | ---: |
| Deadline | 15/15 | 100% |
| Scheduled event | 25/25 | 100% |
| Cancellation | 6/6 | 100% |
| Rescheduled replacement | 8/8 | 100% |

Historical `rescheduled_old` confidence was stable for 6/8 members (75%). Two
exact old slots received supported/unsupported/supported verdicts and were
retained as uncertainty by the ensemble. Their replacement/current state was
stable, no wrong binding was emitted, and effective reschedule recall remained
8/8. Automatic promotion of those old slots was not evaluated, and its
false-positive behavior on broader historical language remains unmeasured.
They therefore remain visible for review and are not eligible for automatic
persistence.

The repository's stricter stretch gate remains false because it requires at
least 95% accepted parent-cluster Jaccard and at least 95% agreement in every
critical category. That failure is nonblocking for the bounded review shadow,
but it blocks deterministic auto-promotion under the stretch policy.

## Independent Judgment

The external Sol-medium evaluator returned:

- `personal_quality_verdict=accept_with_conditions`;
- `temporal_quality_benchmark_passed=true`;
- `precision_recall_release_blocker=false`;
- `current_state_stability_release_blocker=false`;
- `full_personal_release_benchmark_passed=false`;
- `stricter_stretch_gate_passed=false`; and
- `production_evidence_verdict=development_evidence_only`.

Its two blocking findings apply to broader claims, not to the bounded temporal
shadow. Missing parity and retrieval evidence blocks a complete personal Brain
V2 release. The public development replay blocks a production-evidence claim.
Neither finding contradicts the guarded review-only acceptance.

## Full-Release Gaps

### Original Brain parity

The parity preflight froze 150 fact-rich Gmail threads and 254 identical message
packets for Original and V2 arms, but it made no model or persistence calls. Its
manifest correctly records `semantic_denominator_verified=false` and
`release_evidence_ready=false`. Candidate, review, and persistence retention are
therefore still unmeasured.

### Retrieval relevance

Retrieval routing and structural integrity exist over 6,960 active projections
and 15,953 chunks, but there is no frozen 40-query relevance authority. The
top-5, top-10, and source-recall targets cannot yet be scored.

### Private-distribution evidence

Before making a complete personal-release claim, run a privacy-preserving local
shadow over a separately frozen private cohort and expose only aggregate scores
for review. Private Gmail must not be sent to an external evaluator.

## Operating Decision

The safe next operating mode is:

1. run the temporal sidecar in an isolated, reversible personal shadow;
2. show both supported and uncertain output for human review;
3. preserve source evidence, audit logs, rollback, and current-versus-history
   distinctions;
4. prohibit automatic persistence of temporal-shadow outputs, reminders,
   calendar changes, and downstream Chief-of-Staff action; and
5. complete Original Brain parity plus the frozen 40-query retrieval evaluation
   before reconsidering a full release.

## Evidence Commitments

| Artifact | SHA-256 |
| --- | --- |
| Fixture | `473bd0a0a691c72b112235d3e882bc7a80aacb0e9184cef18782735227eb1653` |
| Challenge | `4905712a2d13d97f17dc387be29a1a165372973a1427d29ece0462dd1517822f` |
| Gold | `6b9a2123880ccc9271371b7fad399fb3c24d9a633a1cf49e6e32f32aaa2f8846` |
| Frontier diagnostics | `faf22ea424d41630c34f3f1b22b2d6577c185dd3227b9ac3f39095fbcb8791dd` |
| Exact prediction launcher | `4c25eb161d55411ffccb94c4e606c7c3c52b78e1ad4962ac6c9ad29947b8700f` |
| Prediction seal | `c210ebec195096fa395304cf1665dd5fad864b57f5c656aa54a649f050091c2e` |
| Results | `004b1dfe80a4952f678fc7a69a5041f70fc50e68c9ed9c5193e9be2178a885a4` |
| Score v13 | `d4a25efb6e0a904733a1a1b70a6dde29cf004a4e495b3e5c88928e9a384bfa1e` |
| Score authentication tag (HMAC-SHA256) | `ce0bc793476be009eaa73e9af799ecee214afba321ebee63acac98d4e03b7c91` |
| Final-judge schema | `bea33b35df92b3d0c643d969773a9b0198d8aec73246420163205fd44161aa7a` |
| Final-judge prompt | `b530bafb1d7ace6a11bb801670e62fc4f8d1e4d59a0adcc029a92b375b71867e` |
| Final Sol-medium report | `14ff565cb1dd50ff63ad7e95756be2c62927b734b1debe69d02669aa415d3442` |

Repository validation at the prediction freeze passed 552 focused deterministic
tests and the complete 2,761-test Python suite. Ruff lint and scoped diff checks
passed. The user's unrelated modified app files were excluded from the
milestone commit.
