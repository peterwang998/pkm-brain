# Gmail Temporal Recall Iteration — 2026-07-21

**Status:** the personal-use release candidate passes the frozen synthetic gate in two fresh runs; private-distribution and operational validation remain pending, and every output remains review-only and unapproved for persistence, routing, reminders, or automatic application

**Exploratory target used before 2026-07-22:** at least 85% useful-record recall, at least 90% supported-proposal precision, and less than 1% critical semantic error, with recall favored when a proposal can remain visibly deferred
**Privacy:** every historical replay and artifact report is content-free; private JSONL artifacts are mode `0600`

## Pragmatic Personal-Use Release Bar

The earlier 85% recall gate was useful for architecture exploration but is too
weak for daily use. A fair production bar for one person's review-first system is
smaller than a public product benchmark, but it must still distinguish three
different claims.

### Synthetic regression gate

One frozen adversarial benchmark must clear all of the following in two fresh
model runs:

- at least 95% useful-record and required-member recall when `supported` and
  visibly `uncertain` review results both count as recovered;
- at least 90% exact-unit and complete-unit recall, so one recovered endpoint
  cannot conceal a missed endpoint in a multi-date assertion;
- at least 95% precision on `supported` candidates and at least 90% precision on
  the broader review arm;
- zero accepted default-negative cross-bindings, selected promotions or routine
  noise, duplicate aliases, supported overclaims, and critical wrong-event,
  wrong-time, wrong-relation, or wrong-lifecycle errors;
- at least 95% parent decision-unit and semantic-member agreement between fresh
  runs after reducer-equivalent aliases are collapsed.

### Private-distribution gate

A frozen, thread-grouped historical Gmail holdout of roughly 120--200 messages,
with at least 50 genuinely useful temporal records, must demonstrate at least
90% useful-record and member recall, at least 95% supported precision, no
critical supported errors, and no more than 5% noise admitted to review. The same
cohort must retain at least 95% of the original Brain's independently judged
useful non-temporal facts, with source and thread counts reconciled. Temporal
cognition may add structure; it may not recreate the earlier
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

## Outcome So Far

The remaining Gmail problem is association selection, not broad date discovery. The current deterministic analyzer still finds at least one temporal expression, subject mention, and candidate association in all 265 current messages classified as `important_temporal`. Relaxing the terminal-boundary schema alone did not reach the target. The implementation therefore moved to one-expression, segment-local selector packets, an explicit review-only rescue lane, and association-isolated deterministic reduction.

The production-shaped local pieces are implemented and regression-tested. The selector boundary now also exposes a deterministic candidate frontier and lossless alias-aware pages, so the model can classify every legal expression-subject binding instead of having to invent endpoint pairs.

The original non-private 36-message benchmark cleared the exploratory target but
did not meet the later semantic release bar. The final frozen benchmark contains
39 messages, 27 useful records, 34 semantic units, 36 required members, and 12
advertising or routine-noise records. Its deterministic 98-candidate frontier
contains all 36 members and all 34 complete units, with 32 of 34 units exact.

Two fresh Luna-medium runs over that frontier passed every synthetic personal-use
gate. The first recovered 27 of 27 useful records, 36 of 36 members, all 34
complete units, and 31 of 34 exact units. The second recovered 27 of 27 useful
records, 35 of 36 members, 33 of 34 complete units, and the same 31 of 34 exact
units. Both measured 100% strict-supported and review-arm precision, with zero
selected noise, default-negative acceptance, duplicate aliases, supported
overclaims, critical calibration errors, or frontier regressions. After alias
collapse the runs agreed on 56 of 57 parent decision units (98.2%) and on 35 of
36 semantic members (97.2%).

This clears the synthetic gate and is enough to freeze a personal-use release
candidate. It is not yet a production-distribution claim: the private Gmail
holdout, original-Brain parity check, persistence integration, and rollback
canary remain outstanding. A private Gmail run still requires explicit informed
approval; no private text was transmitted in this iteration.

## Evidence

### Complete historical replay (pre-final architecture diagnostic)

The read-only current projection replay evaluated 733 admitted messages across 356 eligible threads:

- 5,363 recognized expressions in 335 threads;
- 8,239 recognized mentions in 350 threads, including 128 review-only structured event-title candidates across 106 threads;
- 6,204 bounded association hints in 318 threads;
- all 265 `important_temporal` proxy messages retained an expression, mention, and hint;
- 150 of those 265 had a semantically typed occurrence/deadline hint and 18 had a fully resolved hint;
- the analyzer also retained 58 coarse-relative and 26 recurrence expressions without pretending either class had a resolved instant.

These are coverage diagnostics against historical classifier proxies, not human
recall or precision. They predate the final lead and frontier changes and are not
current release evidence.

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
| v12, two fresh runs | **27/27 useful in both; 36/36 and 35/36 members; 100% precision** | one conservative dense-clause miss in run B |

Before the final external runs, the frontier began omitting a lifecycle-free base
only when an exact, source-verified explicit scheduled, cancelled, or completed
candidate fully subsumed it. Deferred or unknown lifecycle candidates,
reschedules, and distinct direct actual occurrences remain intact. This reduced
the frontier from 108 to 98 candidates without changing semantic-gold coverage.

The final deterministic frontier recovers all 36 required members and all 34
complete units; 32 of 34 units are exact. Run A produced 31 supported and five
uncertain candidates and recovered all 36 members. Run B produced 30 supported
and five uncertain candidates and recovered 35 members, missing the Beta
workshop assertion in the dense three-clause case. Each run retained all 27 useful
records. Both had 100% strict-supported and review precision, no selected noise,
no aliases or supported overclaims, and passed every frozen gate. Raw candidate
acceptance agreed on 93 of 98 candidates; four disagreements were two
reducer-equivalent alias swaps. The meaningful post-collapse measures were 56 of
57 parent decision units (98.2%) and 35 of 36 semantic members (97.2%).

The frozen sample SHA-256 is
`f6a5405431817e871c4fef2e1bab41764897bc956c6cd5676601f6454ca8d0bc`.
The final protocol fingerprint is
`gtfproto_7b39a46d5444afe1e3ec25ffbcc56c568645d7b5a17c98ecd4e7073df64c9c07`.
The two 46-page checkpoint SHA-256 values are
`1ad200a089db3508088eaeef9848e2d6fa525d81a99ef96bfc416358a9d56867`
and
`fd5881619e8f850b8b848a82d7396340165d1333a92bcbd2519a75688724d096`;
their run-manifest SHA-256 values are
`3fa0b46bcafaddb878f24e2c9ef659ef0a8123471a7022cee9758eb21d928b4e`
and
`8a4a55d39620c07490033e91d5b8dc717fee6febafb89a122dd93e79374ca1f9`.
The mode-`0600` manifests bind the production modules, verifier policy,
evaluator, semantic gold, benchmark builder, sample, checkpoint, and cohort
counts. This prevents stale or mixed evidence from silently passing; it is not a
cryptographic attestation that an external model produced the rows.

The requested Sol-medium independent diagnostic supported all 36 run-A proposals
across all 27 material records, selected none of the 12 noise records, and
reported zero critical errors. A generic run-B judgment rejected one
byte-identical deferred arrival-boundary proposal by treating arrival as an event
start, despite the fixed ontology. Two isolated reruns agreed once the ontology
was stated explicitly: an arrival or end is a deferred terminal boundary with an
unspecified relation, not an occurrence start. The full ontology-aligned run-B
diagnostic then supported all 35 presented proposals across all 27 material
records, selected no noise, and reported zero critical errors. The final run-A
and run-B label SHA-256 values are
`839aa16bf298cb323522b47165575af5e0752d719c17afb598ebfcde5d3d9cda`
and
`a1cca23ec21be7a13cda6ff5f031f7c91ed77312a8ac35fbef8e4ce2deed1271`.
This prompt sensitivity is why immutable semantic gold remains the release
authority and Sol remains an independent disagreement diagnostic.

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

### 6. Event-centered temporal memory

The selector output remains a non-routable evidence sidecar. A resolved occurrence should ultimately reference a stable event entity. Schedules, deadlines, cancellations, completions, and replacements should update an append-only event lifecycle only after event identity is reconciled. Ordinary durable facts remain ordinary facts; they are not required to carry a temporal object merely because their source email contains a date.

## Verification

- The focused semantic-gold benchmark suite passes 19 tests; the lead suite
  passes 91 tests; the focused selection/frontier suite passes 96 tests.
- The complete repository suite passes: 1,417 tests.
- Ruff passes for all changed temporal modules and tests.
- The historical replay and fresh packet planner print aggregates only.
- The new planner, frontier, verdict validator, and reducer are non-routable and make no external calls by themselves.

## What Remains Before A Release Claim

1. Freeze human labels for the thread-grouped private Gmail holdout independently
   of this pipeline. With explicit informed approval, run the pinned verifier and
   report the private-distribution metrics above. Preserve raw page verdicts and
   score one arm-blind proposal union; do not reinterpret model-judge support as
   human-gold accuracy.
2. Measure original-Brain parity on the same sources. Reconcile source and thread
   counts and require at least 95% retention of independently useful non-temporal
   facts.
3. Integrate verifier and uncertainty sidecars into Gmail ingestion with complete
   page accounting, deterministic replay, idempotent persistence, and no duplicate
   active event or temporal reference.
4. Run a review-only canary with an easy rollback. Keep reminder and calendar
   automation disabled until the separate 99%-precision exact-only gate passes.

## Current Decision

Freeze the current architecture as the personal-use release candidate. Keep
expression discovery broad and deterministic; keep temporal references
event-centered; put ambiguity into a non-routable parent-cluster review state;
and make model judgment classify a complete, validator-backed,
expression-local candidate frontier. Separate evidential support from downstream
normalization and identity deferral. Preserve subject bridges, collapse only
proven reducer-equivalent aliases, and page overflow losslessly. Keep
supported-only precision measurable, but do not project uncertain alternatives
as exact citations. Further synthetic tuning is unlikely to be more informative
than the private holdout and operational canary.
