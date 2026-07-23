# Gmail Temporal Recall Iteration — 2026-07-21

**Status:** the review-only architecture passes the current frozen synthetic gate
in three separately requested Luna-medium runs. Its source-bound append ledger,
idempotent replay, stale-head handling, and rollback primitives are implemented
and tested. A private Gmail holdout, actual same-source original-Brain parity
run, and review-only canary remain pending. The authoritative post-ingest runner,
per-message projection-v7 policy, durable zero-work/execution ledger, and
content-free full-corpus audit are implemented. A final ontology-aligned
Sol-medium diagnostic independently accepted every synthetic production artifact
with zero critical errors. Nothing in this audit authorizes automatic fact
promotion, routing, reminders, or calendar actions.

**Exploratory target used before 2026-07-22:** at least 85% useful-record recall,
at least 90% supported-proposal precision, and less than 1% critical semantic
error, with recall favored when a proposal can remain visibly deferred. This is
retained only as historical context; it is not the release bar.
**Privacy:** every historical replay and artifact report is content-free; private JSONL artifacts are mode `0600`

## Pragmatic Personal-Use Release Bar

The earlier 85% recall gate was useful for architecture exploration but is too
weak for daily use. A fair production bar for one person's review-first system is
smaller than a public product benchmark, but it must still distinguish three
different claims.

### Synthetic regression gate

One frozen adversarial benchmark must clear all of the following across three
fresh model runs and their deterministic consensus:

- at least 95% effective temporal recall when confirmed citations and visibly
  uncertain, non-routable review sidecars both count as recovered;
- at least 90% confirmed recall among members whose gold calibration permits a
  supported assertion;
- at least 95% precision on confirmed artifacts and at least 90% precision on
  the broader review arm;
- at least 90% exact-unit and complete-unit recall, so one recovered endpoint
  cannot conceal a missed endpoint in a multi-date assertion;
- zero accepted default-negative cross-bindings, selected promotions or routine
  noise, duplicate aliases, supported overclaims, and critical wrong-event,
  wrong-time, wrong-relation, or wrong-lifecycle errors;
- at least 95% parent decision-unit and semantic-member agreement between all
  pairs of fresh runs after reducer-equivalent aliases are collapsed.

The release unit is a production artifact, not a raw candidate ID. One
supported citation is one artifact. One uncertainty sidecar is also one
artifact, even if it contains multiple reducer-equivalent candidate aliases.
Scoring collapses aliases into semantic hypotheses and performs deterministic
one-to-one matching: an artifact can recover at most one gold member, a gold
member can justify at most one artifact, and additional artifacts are redundant
precision errors. An uncertainty sidecar is pure only when every hypothesis is
an allowed alternative for the same member. Parent-cluster reviews created by
split three-run semantics are triage signals only; they authorize no candidate
and cannot improve primary recall.

### Private-distribution gate

A frozen, thread-grouped historical Gmail holdout of roughly 150 messages,
with at least 50 genuinely useful temporal records, at least 60 required
temporal members, and at least 40 hard negatives, must demonstrate at least
95% effective member recall, at least 90% confirmed recall, at least 95%
confirmed precision, at least 90% review-arm precision, no critical supported
errors, and no more than 5% noise admitted to review. The same cohort must retain
at least 95% of every supported, scope-correct original-Brain non-temporal fact
unit at candidate, review, and persistence stages, with at least 50 units across
30 threads and reconciled source/thread counts. On 40 frozen temporal-recall
queries, top-five retrieval must reach at least 90% and top-ten at least 95%.
A separate source-only usefulness slice may be reported as a
diagnostic, but it cannot shrink the release denominator. Temporal cognition
may add
structure; it may not recreate the earlier
522-sources-to-10-facts collapse. The existing 120-message development cohort can
fill this role only after its labels are frozen independently of the current
pipeline.

### Operational gate

Every planned page must be covered, replay must be deterministic and idempotent,
no duplicate event or temporal reference may be persisted, and the rollout must
start review-only with an easy rollback. This gate deliberately permits uncertain
review items because recall is the priority. It does not authorize automatic
reminders or calendar actions. Those require a separate exact-only path: one
unambiguous normalized endpoint, a supported event binding, no deterministic
deferral, at least 99% measured precision, and user confirmation until enough
real operating evidence exists.

For this personal-use release, structural preparation must cover 100% of
fact-admitted/high-value messages and at least 99.5% overall, with every bounded
failure visible as quarantine rather than silent omission. Provider change to
the local mirror should be p95 within 15 minutes and end-to-end review p95 within
30 minutes. Historical backfill may average at most two verifier pages per
incoming message and use a candidate-bearing p95 ceiling of 24 pages. The
steady-state canary must run for at least 72 hours or 200 messages, whichever is
longer, and should target p95 at most 12 because routine daily mail should not
resemble dense historical digests. No backlog may remain older
than 24 hours, and user-visible review noise is capped at ten items per day.

## Outcome So Far

The remaining Gmail problem is association selection, not broad date discovery. The current deterministic analyzer still finds at least one temporal expression, subject mention, and candidate association in all 265 current messages classified as `important_temporal`. Relaxing the terminal-boundary schema alone did not reach the target. The implementation therefore moved to one-expression, segment-local selector packets, an explicit review-only rescue lane, and association-isolated deterministic reduction.

The production-shaped local pieces are implemented and regression-tested. The selector boundary now also exposes a deterministic candidate frontier and lossless alias-aware pages, so the model can classify every legal expression-subject binding instead of having to invent endpoint pairs.

The original non-private 36-message benchmark cleared the exploratory target but
did not meet the later semantic release bar. The final frozen benchmark contains
39 messages, 27 useful records, 34 semantic units, 36 required members, and 12
advertising or routine-noise records. Its deterministic 96-candidate frontier
contains all 36 members and all 34 complete units; 32 of 34 units are exact.

Three separately requested Luna-medium runs over that frontier returned
byte-identical complete verdict artifacts: 31 supported candidates, five
uncertain candidates, and 60 unsupported candidates in each run. The
three-run consensus therefore produced 31 confirmed citations and five pure,
non-routable uncertainty sidecars. Artifact-level scoring matched all 36
production artifacts one-to-one to all 36 required members: 100% effective
recall, 31 of 32 confirmed members recovered (96.9%), 100% confirmed precision,
100% review-arm precision, all 34 units complete, and 32 of 34 units exact
(94.1%). It selected no noise and produced no redundant artifact, impure
sidecar, supported overclaim, default-negative acceptance, critical semantic
error, or frontier regression.

All three run pairs had 1.0 Jaccard agreement on accepted parent clusters and on
recovered semantic members; raw candidate verdict agreement was also 1.0. This
passes the synthetic personal-use bar. It is still not a private-distribution or
operational release claim: the private Gmail holdout, same-source original-Brain
parity check, and live execution/persistence rollback canary remain outstanding.
A private external Gmail run still requires explicit informed approval; no
private text was transmitted in this iteration.

## Evidence

### Complete historical replay

The current read-only planner/frontier replay evaluated 733 admitted messages
across 356 eligible threads. It produced 5,364 recognized expressions, exactly
5,364 selector batches, and 16,558 verification candidates. Every expression
was covered: there were zero batch omissions, zero incomplete batches, and zero
omitted candidate mentions. All bounded payloads stayed within the configured
12,000-byte, 24-mention, and 96-batch limits.

This is strong losslessness and bounded-execution evidence over historical Gmail.
It is not human-labeled recall or precision evidence. In particular, it cannot
replace the private holdout or establish original-Brain fact parity.

### Boundary-only diagnostic

Reprojecting the prior endpoint citations after admitting terminal-boundary subjects only as deferred, semantically unspecified references produced 80 proposals. A fresh restricted Sol-medium pass judged:

- 56 of 85 independently judged material records supported;
- 63 of 80 proposals supported;
- 16 records with a critical semantic-error label.

After excluding records the same judge said should be suppressed, useful-record support was 53 of 71 (74.6%). This arm remained well below the release target. Because the independent judge reran and changed some materiality/filter labels, the change from the prior 53-of-76 diagnostic is directional development evidence, not an apples-to-apples quality estimate.

The result rules out a validator-only fix. A permissive whole-message selector still chooses the wrong occurrence or lifecycle often enough that post-hoc repair cannot recover the missing binding.

### Offline recall ceiling and policy sweep

The older independently labeled endpoint cohort contains 82 records the judge marked both material and admissible. Sixty-one of those have at least one explicit judged-best lead; across all strata there are 68 judged-best endpoint pairs. Reanalyzing the exact saved evidence with the current code showed:

- all 68 of 68 judged-best pairs are present in a current expression packet and independently validate;
- six of those valid pairs are absent from the packet's bounded lead hints, confirming that hints cannot be a recall boundary;
- of 13 useful misses for which the old judge supplied both supported endpoint sets, 12 endpoint combinations are colocated and 11 are legal current subjects;
- the resulting endpoint oracle ceiling is 72 of 82 useful records, or 87.8%. The additional 11 are endpoint-set ceilings rather than independently judged pairs, so this is not a measured recall result.

The content-free candidate sweep found this production-safe Pareto point:

| Candidate policy | Judged-best pairs retained | Additional endpoint-set recovery | Base bindings |
|---|---:|---:|---:|
| Valid packet subjects | 68/68 | 11/13 | 1,595 |
| Hint-only | 62/68 | 11/13 | 660 |
| Unclustered cap four | 68/68 | 11/13 | 1,196 |
| Alias-clustered local + subject bridge, cap four | 68/68 | 11/13 alias-aware | 1,122 |

The last policy reduces base candidate volume by 29.7% without losing a known semantic binding. Local-only was smaller on this cohort but was rejected as the production choice because it excludes cross-field subject bridges. The implementation uses four clusters as a page width rather than a destructive final cap: every overflow cluster receives a later page.

A later, deliberately narrow singleton cross-segment fallback improved useful
candidate exposure on this same 82-record cohort from 77 of 82 (93.9%) to 79 of
82 (96.3%). It added exactly two useful cases, one ordinary fact and one
temporal-rescue record, added no non-useful case, and forced both additions to
remain deferred. The fallback is available only when there is one global
expression, one retained review-ambiguous same-field event lead in another
segment, no temporal subject already in the packet, and a bounded source gap.
It is therefore a recall escape hatch, not a general cross-segment matcher.

This candidate pruning does not solve materiality precision. Every recall-preserving arm still exposed plausible bindings in 15 of 36 records the judge said should be suppressed. A grouped out-of-fold deterministic endpoint-feature verifier also found no operating point at both 90% supported-proposal precision and 85% useful-record recall. Materiality and semantic support therefore remain explicit model judgments, with deterministic negative gating only when no legal subject can be cited.

### Fresh full-text development cohort (pre-final architecture diagnostic)

A new 120-message, HMAC-opaque development cohort was rebuilt with untruncated source text. The deterministic planner produced:

- 672 recognized expressions and exactly 672 selector packets;
- 2,071 subject/lifecycle candidates, including 30 review-only event-title candidates;
- zero expression omissions under the hard packet caps;
- one anchored expression per packet, at most 16 mentions, at most four hints, and a 12,000-byte payload ceiling;
- 105 expression packets with no local subject mention, preserved for explicit selector abstention rather than silently discarded;
- 35 source-suppressed records eligible only for the non-routable temporal-rescue lane.

The cohort is still development data, not a frozen human holdout. Reanalysis of the exact saved text found zero stale endpoint inventories, zero invalid spans, zero context truncations, and zero packet omissions. Those checks demonstrate lossless expression planning under bounds; they do not demonstrate semantic quality.

Replaying the then-current validator-backed frontier over the same cohort produced
2,155 lifecycle-aware candidates in 1,440 alias clusters. Four-cluster lossless
paging produced 605 verifier pages; 152 expression packets had an empty complete
frontier and could be deterministically deferred without a model call. Fourteen
packets explicitly reported omitted candidate endpoints and therefore could not
be skipped; two of those had an empty visible frontier. Seventy-seven packets
required overflow pages. Every page stayed at or below four alias fragments, 12
candidate variants, and 12,000 candidate-payload bytes; the observed maximum was
11,754 bytes. These are dated bounded-execution diagnostics from before the final
lead and frontier changes, not current release evidence or quality measurements.

As a collateral check for the later lifecycle safety rule, its exact trigger
matched zero of 2,167 candidates in the rebuilt private development projection.
That does not prove private accuracy, but it confirms that the guard is not a
broad rewrite of historical Gmail candidates.

### Superseded 36-case non-private synthetic exploration

The reusable benchmark generator produced 36 wholly synthetic email messages: 24 useful temporal records and 12 promotions or routine notices. It covers planned and actual occurrences, deadlines, subject/body bridges, lifecycle transitions, reschedules, ranges, recurrence, locale and timezone ambiguity, terminal boundaries, quoted history, and candidate-bearing noise. It contains no Gmail-derived text and can therefore exercise the restricted external selector and judge without transmitting private mail.

Three Luna-medium iterations isolated two distinct problems. The first prompt treated deterministic deferral as epistemic uncertainty. The second separated evidential support from downstream handling but admitted a routine login-code expiration. The winning policy keeps ranges, recurrence, coarse expressions, and missing timezones eligible when their expression-subject binding is direct; reserves `uncertain` for genuine relationship, lifecycle, or materiality ambiguity; treats terminal boundaries as boundaries rather than occurrence starts; and explicitly suppresses routine no-action security metadata.

The third pass used four verifier pages per external process and one new deterministic expression rule: phrases such as `before lunch tomorrow`, `by end of day today`, and `before close of business tomorrow` are inventoried as one coarse, deferred temporal expression instead of losing the time-of-day boundary. The requested Sol-medium end judge reported:

| Projection | Useful-record recall | Supported-proposal precision | Critical errors | Selected noise |
|---|---:|---:|---:|---:|
| Supported only | 19/24 (79.2%) | 23/23 (100%) | 0/36 | 0/12 |
| Supported + uncertain | **22/24 (91.7%)** | **28/28 (100%)** | **0/36** | **0/12** |

The recall-biased projection therefore clears the exploratory gate of at least 85% recall, at least 90% supported-proposal precision, and less than 1% critical semantic error. Its two misses are explicit representation/discovery gaps: an effective policy/state-change date and registration opening/closing boundaries. All ordinary high-confidence, lifecycle, and ambiguous-event strata were recovered.

Revalidating the exact raw Luna verdict rows through the hardened v2 aggregate produced 23 exact supported citations across 19 records plus five non-routable uncertainty clusters across three additional records. Those clusters contain five plausible candidates and expose no exact-citation surface. Thus the same 22-of-24 review recall is preserved while alternatives, subject aliases, and unresolved reschedule endpoints can no longer masquerade as exact facts.

Sol is useful as the requested independent diagnostic, but it is not stable enough to become synthetic ground truth by itself. Earlier arm comparisons produced different semantic judgments for byte-identical proposal sets, including misclassifying a two-date alternative as a lifecycle error. The benchmark therefore embeds immutable record-level materiality and filtering gold; the scorer fails if Sol disagrees with it, and future candidate-level gold should use semantic locators rather than content-derived IDs. The winning pass had zero such record-level disagreements.

The winning evidence is bound to protocol fingerprint `gtfproto_54922ff40db39868eb88cdb419e9c51974fff9ab4dfec16fa3be40d539d03c73`. The exact synthetic source SHA-256 is `0f7267cbfc8bd1f9e24ca6804a7eafaff8afcdb0e80f72433d792d1e42da3332`; the Luna checkpoint SHA-256 is `0de5bcf9e0ca430f2c691b231642ca30e29d84614c3d668ac845906225ecd9b2`; and the recall-arm Sol labels SHA-256 is `d492aa9fa19da7147e42adb0fd9adb5dff42a2d696cfad1edd44b80d2c7b59e2`. Every artifact remains mode `0600`, and neither runner printed message content.

### Semantic-gold stability iteration — 2026-07-22

The earlier Sol-only proposal score hid semantic alias inflation. Re-scoring the
36-case v3 run against stable semantic locators found 26 of 28 correct candidate
members (92.9%), two duplicate aliases, and a missed completion boundary. The
benchmark was therefore expanded and Sol was demoted from ground-truth authority
to an independent diagnostic.

The final 39-case suite adds dense three-clause cross-binding, mixed authored / ad
/ prompt-injection / quoted-history content, policy effective dates, opening and
closing windows, anaphoric completion, mutually exclusive dates, and a compound
sentence with separate cancellation and scheduling lifecycles. Its semantic gold
uses source surfaces and derived semantics rather than unstable candidate IDs;
unmatched candidates are negative by default.

| Iteration | Main result | Failure exposed |
|---|---|---|
| v3 rescore | 26/28 candidate members | two aliases; completion missed |
| v4 | 30/32 units recovered | effective date and completion missed |
| v5/v6 | up to 31/32 reviewed units; ~97% review precision | dense clause cross-binding |
| v7 | 31/32 units; 94.3% review precision | lifecycle/date alias duplication |
| v8 | 32/32 units; 97.1% review precision | neighboring deadline cross-binding |
| v10, two fresh runs | 34/34 units and 36/36 members; 100% review precision | redundant lifecycle-free bases remained |
| v12, two fresh runs | 27/27 useful in both; 36/36 and 35/36 members; 100% precision | one conservative dense-clause miss in run B |
| v17, three fresh runs | 35/36 effective members; 30/32 confirmed members; 97.2% review precision | one correlated lifecycle miss and one redundant artifact in all three runs |
| v19, three fresh runs | **36/36 effective members; 31/32 confirmed members; 100% artifact precision** | no synthetic gate failure |

Before the three-run iterations, the frontier began omitting a lifecycle-free base
only when an exact, source-verified explicit scheduled, cancelled, or completed
candidate fully subsumed it. Deferred or unknown lifecycle candidates,
reschedules, and distinct direct actual occurrences remain intact. This reduced
the frontier without changing semantic-gold coverage.

The v17 three-run experiment exposed why consensus cannot be treated as a cure
for correlated semantic error. In the synthetic assertion, "The review meeting
took place on August 9, 2027 and was completed that afternoon," all three runs
rejected the direct, blocker-free actual occurrence, retained an ambiguous
completion refinement for the same August 9 endpoint, and separately retained
the correct completion boundary. That missed the occurrence and emitted the
completion twice. Stability was 1.0, but quality was not: effective recall was
35 of 36, confirmed recall was 30 of 32, and one of 36 production artifacts was
redundant.

The resulting deterministic guard is intentionally review-only and exact. When
the sole accepted candidate in a binding cluster is an unknown-lifecycle
candidate carrying both expression-scope and lifecycle-subject-binding
conflicts, and it has exactly one same-binding, same-expression, same-time,
blocker-free `strict_direct` actual-occurrence sibling, the accepted identity is
moved to that direct occurrence as `uncertain`. It can never create support.
Ambiguous, non-actual, non-direct, multi-candidate, or explicit-lifecycle cases
remain unchanged. A counterfactual replay fixes the v17 failure, while the exact
trigger matched zero of 2,167 candidates in the private development projection.

The final v19 deterministic frontier contains 96 candidates and recovers all 36
required members and all 34 complete units; 32 of 34 units are exact. Each of
the three complete Luna-medium runs produced the same 31 supported, five
uncertain, and 60 unsupported verdicts across 46 pages. The consensus produced
36 production artifacts: 31 supported citations and five pure uncertainty
sidecars. One-to-one artifact matching recovered all 36 members with no
redundant or unmatched artifact. Confirmed recall was 31 of 32 (96.9%), effective
recall was 36 of 36, supported and review-arm artifact precision were both 100%,
complete-unit recall was 100%, and exact-unit recall was 32 of 34 (94.1%). All
27 useful records were recovered; none of the 12 noise records was selected.
There were zero critical errors, overclaims, default-negative acceptances,
impure sidecars, aliases expressed as duplicate artifacts, or ratchet
regressions.

The frozen sample SHA-256 is
`f6a5405431817e871c4fef2e1bab41764897bc956c6cd5676601f6454ca8d0bc`.
The final semantic-gold SHA-256 is
`4efbea11bee95c377ba9f05f1a5276ed2fdae267582f7e5028aa8dfe07e8e377`;
the protocol fingerprint is
`gtfproto_deaa3ca12e8aabd90f8a83ef9160bb93881044549d5a4974636c47ba8a560c88`;
the candidate-authority fingerprint is
`gtfea_1890b411055f34f3f8442154afe32272075c228cdb30d8d2c583662f7165630c`;
and the three-run policy fingerprint is
`gtfep_dfaab918de2061a44c6892f8af9b6aba5306876ce4e01061ad23ee994e3b658c`.
All three checkpoint files have SHA-256
`bc5a6014abebdc3806fab643ba7d81d74b6e2da9a15260a347d71575ade3b439`;
their byte identity is consistent with the 1.0 verdict agreement.

The mode-`0600` manifests bind the model name and effort, production modules,
verifier policy, evaluators, semantic gold, benchmark builder, sample,
checkpoint, and cohort counts. The evaluator verifies fresh, coherent
provenance and distinct supplied evidence paths. It deliberately reports
`independent_invocations_verified: false`: file paths and hashes cannot prove
that three external calls were operationally independent, and the identical
checkpoint bytes make that caveat especially important. The evidence should be
described as three separately requested runs, not a cryptographically attested
independence result.

Sol-medium was retained as an independent disagreement diagnostic, not promoted
to ground truth. Four fresh contracts make the distinction important:

| Sol contract | Member recall | Artifact precision | Critical errors | Interpretation |
|---|---:|---:|---:|---|
| v1 | 84.2% assertion recall | 88.9% | multiple | incorrectly treated review-only support as automation-ready and rejected safe deferral |
| v2 | 34/36 (94.4%) | 34/36 (94.4%) | 1 | still misread deadline extension and deferred reschedule semantics |
| v3 | 35/36 (97.2%) | 35/36 (97.2%) | 1 | omitted the implemented intake-window exception for a registration deadline |
| v4 | 36/37 (97.3%) | **36/36 (100%)** | **0** | accepted every artifact; added one required member beyond frozen gold in the mixed authored/adversarial/quoted-history record |

The v4 judge accepted all 31 supported citations and all five uncertainty
sidecars, admitted all 27 useful records, suppressed all 12 noise records, and
reported no unsafe promotion. It counted 33 of 34 units complete. The one-member
denominator difference is not a rejected Brain artifact: immutable semantic gold
defines one current schedule member in that mixed record, while Sol assigned two
members to one unit. The pipeline and frozen gold were not changed to fit that
post-hoc judgment. Under both authorities, the diagnostic clears the personal
review-only bar; semantic gold remains the release authority.

The final Sol contract fingerprint is
`gtsj_1d9784d3e6929a8987a7d7d54b1c6d8e1ec6c3ea347732036348d21d266c6513`;
the label SHA-256 is
`03a497f2c4bf988622459886e7bd56e9e2ce26b9599a4fd141289e58e2a84550`;
the mode-`0600` label-manifest SHA-256 is
`a3b58ccba7371ed93b0b2e6c75c35a864a3d2346c6ff62ce6088308392efa440`.
The run used restricted external `gpt-5.6-sol` at medium reasoning over synthetic
content only and made seven fresh batch calls.

## Implemented Architecture

### 1. Lossless evidence inventory

`gmail_temporal_leads.py` inventories expressions and mentions independently of fact admission. Every endpoint has an exact source span, field, local segment identity, content-bound ID, and source fingerprint. Recurrence and unresolved coarse-relative expressions remain unresolved; message-anchored phrases such as `tomorrow morning` preserve the determinable calendar day while keeping the coarse time of day unresolved and deferred. Conservative structured event titles can become `event_title_candidate` endpoints, but they carry a review-only blocker and never become trusted event identity by themselves. Temporal evidence inside quoted or forwarded mail remains inventoried but carries `quoted_or_forwarded_context`, which deterministically blocks precise selection and forces deferral.

Fact admission now has three explicit association states:

- `fact`: the original Brain admitted the message;
- `temporal_rescue`: the original classifier suppressed it, but it may be inspected in a separate review-only lane;
- `none`: no association work is authorized.

The rescue state does not change expression or mention inventories and does not promote the message to an ordinary fact. Any rescued positive association is deterministically forced to low-confidence deferral; a model may still return a medium-confidence negative filtering decision such as `reject_nonmaterial`.

### 2. Expression-centric source packets

`gmail_temporal_batching.py` partitions one immutable analysis into deterministic segment-local packets. Each packet contains one temporal expression, only local subject/lifecycle candidates, an optional subject-line event bridge, a small number of ranking hints, exact source slices, and an endpoint authority manifest. Every recognized expression is either covered exactly once or receives a content-free omission reason. Source, analysis, plan, batch, and endpoint fingerprints prevent replies from being rebound to changed email.

The planner is pure and performs no model call, write, routing, admission change, or persistence. A selector citation is accepted only if it is a subset of the exact packet manifest.

The prose window is a byte bound, not a recall boundary. If one unusually long
sentence forces that window to trim around the temporal expression, exact event,
action, deadline, boundary, and lifecycle endpoints elsewhere in the same
deterministic sentence segment remain eligible as manifest endpoints. They are
shown by exact surface and unsafe distant bindings defer. Context padding from
an adjacent sentence never grants endpoint authority, and endpoint or payload
overflow remains an explicit incomplete frontier. A 5,600-character regression
that previously returned a falsely complete empty frontier now recovers its
distant event and scheduled lifecycle as two deferred candidates.

The narrow singleton fallback is the only deliberate exception to ordinary
segment locality. It may expose one same-field, nearby event from another
segment when the message has one temporal expression, no packet-local temporal
subject, and exactly one retained review-ambiguous event lead. The resulting
packet is reduced to that expression, event, and bounded context and is always
deferred. The 82-record replay shows why this exception exists: two useful
bindings were restored without exposing any additional non-useful record.

### 3. Endpoint-only semantic selection

The external selector is allowed to decide materiality and cite:

- the packet's anchored expression;
- one event, event-title, event-predicate, deadline, action, or deferred terminal-boundary subject;
- an optional lifecycle cue governing that same assertion;
- an optional matching ranking hint.

It cannot author relation, planned/actual kind, lifecycle, normalization, confidence, dates, spans, source text, or explanations. Those fields remain deterministic.

### 4. Association-isolated reduction

Each proposed association is checked against its packet manifest and then passed alone through the deterministic semantic validator. A malformed or cross-packet citation can no longer erase valid siblings from the same message. Valid associations are deterministically deduplicated, ranked, and capped at eight. Citation conflicts, packet omissions, invalid siblings, or cap overflow force the record to remain deferred.

This is the key stability change: recall is expanded before admission and selection, while unsafe ambiguity changes review state rather than inventing a precise temporal fact.

### 5. Explicit candidate verification

`gmail_temporal_frontier.py` enumerates every independently valid expression-subject binding in a finalized packet. It derives relation, planned/actual kind, lifecycle, normalization, blockers, risk features, and required deferral through the same deterministic semantic validator used after selection. Stable candidate and frontier fingerprints bind any later verdict to the exact analysis and packet manifest.

Reducer-equivalent overlapping event-title/event aliases become one decision cluster. Clusters are paged four at a time, but overflow is never discarded; candidate-count and byte bounds can split a large cluster into page-unique decision units without reusing response authority. A verifier must return one supported, unsupported, or uncertain verdict for every presented candidate. The plan-level validator recomputes the immutable page plan, requires every page and candidate exactly once, rejects stale or cross-page choices, and aggregates fragments by stable parent cluster. Exactly one supported candidate and no uncertainty in a cluster can yield an exact citation. Any uncertain candidate, supported-plus-uncertain mixture, or multiple-supported conflict becomes one non-routable cluster sidecar containing the authorized plausible candidate IDs and no citation. Complete empty frontiers are safe deterministic deferrals; incomplete empty frontiers remain unknown. The frontier, pages, and uncertainty sidecars contain no source surfaces of their own; the separately signed expression packet remains the sole text authority.

Before paging, a lifecycle-free base is omitted only when a non-deferred,
source-verified explicit scheduled, cancelled, or completed candidate with the
same endpoint fully subsumes it. Unknown or deferred lifecycle evidence,
reschedules, and independently grounded actual occurrences remain separate. This
removes a model-visible duplicate without using the model to rewrite temporal
semantics.

The post-verdict validator also contains the narrow v17 lifecycle guard. It can
move one unsafe, scope-conflicted unknown-lifecycle acceptance to its unique
strict-direct actual-occurrence sibling only as uncertainty. It never promotes
support and does not fire when there is more than one plausible occurrence.

### 6. Three-run semantic consensus

Production evaluation reduces exactly three complete verifier runs. A candidate
is confirmed only when all three runs call it supported. It remains uncertain
when at least two runs accept it as either supported or uncertain; fewer than two
acceptances reject it. Candidate-level consensus takes precedence so a one-vote
sibling cannot expand a two-vote candidate.

When runs split across reducer-equivalent aliases and no candidate itself reaches
quorum, accepted aliases are grouped by the exact semantic signature
`(expression, relation, kind, lifecycle, normalized value)`. Exactly one
signature with support from at least two distinct runs authorizes one
deterministically chosen alias as uncertain. Competing semantic signatures
produce a non-routable parent-cluster review that authorizes no candidate. The
normal production validator then recalibrates the consensus, preserving all
single-run lifecycle and alias safety rules.

### 7. Artifact-level evaluation

The evaluator scores what the system would actually present: supported citations
and uncertainty sidecars. Candidate aliases inside one sidecar are collapsed to
semantic hypotheses before deterministic one-to-one matching against gold. A
second artifact for an already recovered member is redundant; an impure sidecar
or an unmatched artifact lowers precision. This prevents candidate count from
inflating recall and prevents an ambiguous sidecar from being scored as several
independent facts. Split-semantic parent-cluster reviews are reported separately
as triage coverage and never improve effective recall.

### 8. Event-centered temporal memory

The selector output remains a non-routable evidence sidecar. A resolved occurrence should ultimately reference a stable event entity. Schedules, deadlines, cancellations, completions, and replacements should update an append-only event lifecycle only after event identity is reconciled. Ordinary durable facts remain ordinary facts; they are not required to carry a temporal object merely because their source email contains a date.

### 9. Message-level temporal structure

The frozen candidate and artifact semantics remain unchanged. A separate pure
review projection now groups message-local artifacts only when exact source
grammar supplies the structure:

- `single` for an independent temporal artifact;
- `alternatives` for an explicit source-local `X or Y` date/time list;
- `reschedule` for an explicit `rescheduled ... from X to Y` frame; and
- `split_semantics` for a parent cluster that still has competing semantic
  signatures and therefore authorizes no candidate.

Group members carry an ordered role (`independent`, `alternative`,
`rescheduled_old`, `rescheduled_replacement`, or `unresolved`) and an explicit
`present`, `missing`, or `conflicted` state. A group is metadata, not another
fact or recall artifact. It cannot increase the benchmark score, authorize a
candidate, route work, or rewrite relation, kind, lifecycle, normalization, or
the candidate's original defer state. Every projected wrapper remains
`requires_defer=true` and `routable=false`.

Two frozen v19 records exercise the intended behavior:

- `syn_lifecycle_03`, “The hiring interview was rescheduled from August 14,
  2027 to August 16, 2027,” produces two uncertainty artifacts in one complete
  reschedule group. Their source roles are ordered old then replacement, while
  both underlying hypotheses remain the pipeline's original
  `relation=unspecified`, `kind=unspecified`, `lifecycle=unknown`, and deferred.
- `syn_ambiguous_04`, “Possible dates are August 18, 2027 or August 19, 2027,”
  produces two uncertainty artifacts in one complete alternatives group, with
  source order 1 then 2. Reducer-equivalent subject aliases remain collapsed
  inside each artifact rather than becoming duplicate facts.

Missing endpoints remain visible as non-authorizing incomplete group members.
Incompatible subject families produce a conflicted group. This representation
improves review-time temporal recall without pretending that message-local
subject aliases are already durable event identity.

### 10. Review-only persistence boundary

Migration 25 adds an append-only ledger in the main Brain database for complete
Gmail temporal review runs and their supported citations, uncertainty sidecars,
and split-semantic cluster reviews. Groups remain embedded projection metadata
and are not inserted as extra artifacts. A separate compare-and-swap head points
to the current run for one Gmail message and pipeline scope; rollback only moves
or clears that head and retains immutable history. Exact replay is idempotent,
while the same deterministic input key producing different canonical bytes
fails closed. Partial projections, routable output, stale source authority,
failed artifact writes, and cross-scope head pointers are rejected atomically.
This ledger does not write facts, open questions, the Gmail mirror, or the Chief
of Staff operational database, and no daemon or external-call path is enabled.
Head reads revalidate the bound source and return an explicit `current`, `stale`,
or `cleared` status; rollback cannot restore a superseded or otherwise stale
Gmail source, although a stale head can still be safely cleared.

The persistence boundary is downstream of trusted Gmail Knowledge projections,
not the operational mirror. It revalidates connector-authored lineage, immutable
document content, the exact provider-message range, its trusted internal time,
and the deterministic subject-plus-payload text hash before accepting a review
projection. Three ordered SHA-256 component evidence fingerprints are required
and hash-bound, while
`independent_invocations_verified=false` remains explicit because file hashes
cannot prove independent model execution.

Migration 26 closes that runtime gap with append-only execution and component
receipts. The authoritative runner accepts only a Brain home, active document
identity, provider message identity, and exactly three protected component
files. It reloads the immutable projection, validates the target message range
and per-message policy, recomputes analysis, batching, complete frontier, pages,
sanitized requests, ensemble, and review projection, then persists projection
and execution evidence in one transaction. Production-scope direct sink calls
without runner evidence fail. Zero-work outcomes are also durable, idempotent,
and source-bound, so `not_admitted`, no-expression, and no-candidate messages
cannot be confused with unprocessed mail or repeatedly recomputed forever.

The v26 upgrade also retires a mutable production head created before execution
receipts existed while preserving its immutable review run. Production head
reads require a matching complete, scope-bound execution receipt and otherwise
return stale. This is an integrity boundary, not a hostile-process security
boundary: execution evidence is a validated in-process capability, so code that
already runs inside the Brain process could fabricate self-consistent evidence.

Component files must be three distinct owner-only, single-link regular files
with canonical schemas, distinct invocation identities, exact request/page/
candidate coverage, pinned external-Codex Luna-medium configuration, and valid
chronology. Provider execution is still self-reported rather than
cryptographically attested. The same in-process trust limitation applies to a
caller that bypasses the intended runner entry point. Crash-resumable pending component execution plus
an explicit append-only head-transition sequence remain follow-up hardening.
The runner is still non-routable and has no enabled daemon/external-launch path.

### 11. Local historical structural audit

A fresh local-only audit of the frozen 120-record private development cohort
processed 111 records with temporal expressions, 672 expressions/batches,
2,167 candidates, and 613 verifier pages. All 80 admitted records that contained
temporal expressions had candidates and pages; there were no admitted frontier
gaps. The five other admitted `durable_no_lead` records had no detected temporal
expression, so they are a separate discovery question rather than an omitted
frontier.

All 14 incomplete frontiers and all 104 omitted candidate mentions occurred in
the `suppressed_advertising_temporal` rescue stratum. The admitted high-confidence,
ambiguous, lifecycle, and durable-lead strata each had zero incomplete frontiers
and zero omitted candidate mentions. This is strong structural coverage evidence,
not semantic precision or recall: no private external judge was called, and the
cohort remains development data rather than the fresh blind holdout.

The original-Brain parity evaluator is also now fail-closed rather than a
placeholder ratio script. It requires the frozen cohort and every packet, one
complete original run, at least three complete V2 runs, exact packet/member
coverage, complete arm-blind semantic alignment and labels, hash-bound receipts,
and no omitted emitted member. The judge queue contains the canonical private
message evidence under opaque run aliases; its receipt binds the cohort, packet,
queue, completed judgments, and pinned Sol-medium contract. The release
denominator is every supported, scope-correct original non-temporal unit rather
than the judge's `useful` label. Each V2 stage must retain and precisely reproduce
at least 95% of at least 50 such units across at least 30 threads, and three
same-configuration V2 runs must reach at least 95% all-run
intersection-over-union stability. Exact duplicate outputs, critical errors,
heterogeneous target configurations, incomplete provenance, and a false gate all
fail structurally or exit nonzero. It reports only aggregate/opaque results and
explicitly does not claim cryptographic proof of independent invocation. The
actual private parity extraction and labeling run remains outstanding.

### 12. Projection-v7 full-corpus structural replay

On 2026-07-22 the current encrypted archive was rebuilt into a separate
temporary Brain home using projection v7/classifier v5. The local-only capture
created 7,125 immutable revisions, indexed 16,118 chunks, reconciled exactly
6,960 active and 165 deleted documents, and reported zero capture, ingest,
vector, or reconciliation errors. Projection v7 adds an ordered, trusted
per-message policy index. Thread-level advertising or delivery summaries can no
longer suppress a different message. Each target row records message delivery,
strong and weak advertising bases, exact fact-admission basis, provider
importance/star state, positive human evidence, and whether a later
operator-authored message exists in that thread.

The authoritative content-free audit then prepared 7,857 of 7,859 active
messages. The two failures were explicit bounded batch omissions, yielding
99.97% overall structural completeness and no silent loss. Both were
non-advertising, non-fact-admitted, relevance-free rescue messages--one
transactional and one unknown--so all 733 fact-admitted messages prepared
successfully. It found 45,306
temporal expressions, 34,584 legal verification candidates, 2,123
candidate-bearing messages, and 13,548 lossless verifier pages. Admission was
733 fact, 2,338 temporal rescue, and 4,786 not admitted. The fact-lane
population remained exactly 733 across the v6-to-v7 change, while target-message
authority removed thread-level poisoning. Relative to the last pre-v7
diagnostic, candidate-bearing coverage increased from 1,982 to 2,123
messages while preparation failures fell from 88 to two. Pages increased from
12,044 to 13,548, so the gain is real recall coverage rather than a free cost
reduction.

Overall page volume is 1.72 pages per mailbox message and 6.38 per
candidate-bearing message. The candidate-bearing p95 is 22 pages and p99 is 44;
that passes the bounded historical-backfill budget adopted above but not the
tighter steady-state target. The maximum remains bounded at 75 pages. Only
seven bulk messages entered temporal rescue, six of which had candidates, after
requiring target-specific provider, human, star, or later-operator relevance.
Lexical advertising cues are now weak evidence rather than an unconditional
negative: 69 such messages entered rescue and 59 had candidates, while provider
promotion categorization remains a strong negative unless the owner starred the
message. Semantic precision for those 59 messages is deliberately not claimed
until the private holdout is labeled and the pinned external verifier is run
with explicit informed approval.

The audit script emits only aggregates, static error buckets, bounded policy
strata, and volume percentiles. It prints no path, provider/document/message
identity, source hash, request fingerprint, request payload, exception detail,
or message text, and makes zero model or persistence calls.

## Verification

- Focused regression suites cover lossless batching, singleton fallback,
  lifecycle calibration, three-run alias and split-semantic consensus,
  artifact matching, sidecar purity, stability, and manifest rejection.
- The v19 aggregate passes every synthetic candidate, stability, and provenance
  gate under the current source and evaluator hashes.
- The historical replay and fresh packet planner print aggregates only.
- The planner, frontier, verdict validator, ensemble reducer, and evaluator are
  non-routable and make no external calls by themselves.
- The final complete repository run passed all 1,625 tests, including the 20
  connector tests that require a temporary localhost OAuth callback. The focused
  current Gmail policy/runner/batching/frontier/persistence/migration suite
  passed 353 tests, including the aggregate audit's privacy tests.
  Ruff, formatting, compilation, and diff checks also passed.

## What Remains Before A Release Claim

1. Freeze human labels for the thread-grouped private Gmail holdout independently
   of this pipeline. With explicit informed approval, run the pinned verifier and
   report the private-distribution metrics above. Preserve raw page verdicts and
   score one arm-blind proposal union; do not reinterpret model-judge support as
   human-gold accuracy.
2. Measure original-Brain parity on the same sources. Reconcile source and thread
   counts and require at least 95% retention and precision over every supported,
   scope-correct original non-temporal unit at candidate, review, and persistence
   stages. Do not condition the release denominator on a `useful` label.
3. Run a review-only canary with an easy rollback. Exercise one content-changing
   source revision, exact replay, restart, stale-head clearing, rollback, and
   bounded failure quarantine. Measure the tighter steady-state page and
   freshness budgets rather than extrapolating from the historical backfill.
4. Keep reminder and calendar
   automation disabled until the separate 99%-precision exact-only gate passes.

## Current Decision

Freeze the v19 protocol for the next evaluation step, not as a final production
claim. It is the best current personal-use, review-only candidate: discovery is
broad and deterministic; temporal references remain event-centered; model
judgment is restricted to a complete validator-backed frontier; three-run
consensus can lower confidence but cannot invent semantics; and uncertainty is
scored as a real artifact without being projected as an exact citation.

The synthetic benchmark now meets the fair personal release bar. The honest next
question is distribution shift, not another prompt tweak. Do not call the Gmail
pipeline production-ready until the frozen private holdout and same-source
original-Brain parity test pass and the review-only persistence canary
demonstrates idempotence and rollback. The Sol diagnostic is now understood: it
accepts every artifact and its remaining disagreement is one additional recall
denominator member beyond immutable gold.
