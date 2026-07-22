# Gmail Temporal Recall Iteration — 2026-07-21

**Status:** development evidence only; review-only and not approved for persistence, routing, reminders, or automatic application

**Target for this personal-use iteration:** at least 85% useful-record recall, at least 90% supported-proposal precision, and less than 1% critical semantic error, with recall favored when a proposal can remain visibly deferred
**Privacy:** every historical replay and artifact report is content-free; private JSONL artifacts are mode `0600`

## Outcome So Far

The remaining Gmail problem is association selection, not broad date discovery. The current deterministic analyzer still finds at least one temporal expression, subject mention, and candidate association in all 265 current messages classified as `important_temporal`. Relaxing the terminal-boundary schema alone did not reach the target. The implementation therefore moved to one-expression, segment-local selector packets, an explicit review-only rescue lane, and association-isolated deterministic reduction.

The production-shaped local pieces are implemented and regression-tested. The selector boundary now also exposes a deterministic candidate frontier and lossless alias-aware pages, so the model can classify every legal expression-subject binding instead of having to invent endpoint pairs.

A non-private 36-message adversarial benchmark now clears the personal-use target under the requested external models. The winning recall-biased Luna-medium pass selected 22 of 24 useful records; Sol-medium supported all 28 presented proposals, reported no critical errors, and agreed with the embedded materiality/filter gold on all 36 records. None of the 12 advertising or routine-noise records was selected. This is a synthetic acceptance result, not a production-distribution claim. A fresh external Luna/Sol pass over the full-text 120-message Gmail development cohort is still not reported here: the execution security gate requires explicit informed approval before private Gmail text may be transmitted to those external models. No attempt was made to bypass that gate.

## Evidence

### Complete historical replay

The read-only current projection replay evaluated 733 admitted messages across 356 eligible threads:

- 5,363 recognized expressions in 335 threads;
- 8,239 recognized mentions in 350 threads, including 128 review-only structured event-title candidates across 106 threads;
- 6,204 bounded association hints in 318 threads;
- all 265 `important_temporal` proxy messages retained an expression, mention, and hint;
- 150 of those 265 had a semantically typed occurrence/deadline hint and 18 had a fully resolved hint;
- the analyzer also retained 58 coarse-relative and 26 recurrence expressions without pretending either class had a resolved instant.

These are coverage diagnostics against historical classifier proxies, not human recall or precision.

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

### Fresh full-text development cohort

A new 120-message, HMAC-opaque development cohort was rebuilt with untruncated source text. The deterministic planner produced:

- 672 recognized expressions and exactly 672 selector packets;
- 2,071 subject/lifecycle candidates, including 30 review-only event-title candidates;
- zero expression omissions under the hard packet caps;
- one anchored expression per packet, at most 16 mentions, at most four hints, and a 12,000-byte payload ceiling;
- 105 expression packets with no local subject mention, preserved for explicit selector abstention rather than silently discarded;
- 35 source-suppressed records eligible only for the non-routable temporal-rescue lane.

The cohort is still development data, not a frozen human holdout. Reanalysis of the exact saved text found zero stale endpoint inventories, zero invalid spans, zero context truncations, and zero packet omissions. Those checks demonstrate lossless expression planning under bounds; they do not demonstrate semantic quality.

Replaying the new validator-backed frontier over the same cohort produced 2,155 lifecycle-aware candidates in 1,440 alias clusters. Four-cluster lossless paging produced 605 verifier pages; 152 expression packets had an empty complete frontier and can be deterministically deferred without a model call. Fourteen packets explicitly reported omitted candidate endpoints and therefore cannot be skipped; two of those had an empty visible frontier. Seventy-seven packets required overflow pages. Every page stayed at or below four alias fragments, 12 candidate variants, and 12,000 candidate-payload bytes; the observed maximum was 11,754 bytes. These are bounded execution diagnostics, not quality measurements.

### Non-private synthetic external iteration

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

### 6. Event-centered temporal memory

The selector output remains a non-routable evidence sidecar. A resolved occurrence should ultimately reference a stable event entity. Schedules, deadlines, cancellations, completions, and replacements should update an append-only event lifecycle only after event identity is reconciled. Ordinary durable facts remain ordinary facts; they are not required to carry a temporal object merely because their source email contains a date.

## Verification

- 162 focused lead/selection/batching/frontier/reduction/verifier/benchmark tests pass.
- The complete repository suite passes: 1,339 tests.
- Ruff passes for all changed temporal modules and tests.
- The historical replay and fresh packet planner print aggregates only.
- The new planner, frontier, verdict validator, and reducer are non-routable and make no external calls by themselves.

## What Remains Before A Release Claim

1. With explicit informed approval, run the now-pinned verifier policy over the lossless 120-message private Gmail cohort. Preserve raw page verdicts and score one arm-blind union of proposals so identical proposals cannot receive different Sol judgments merely because they appeared in different arms.
2. Report useful-record recall, supported-proposal precision, critical-error rate, rescued-record recall, selected-noise rate, candidate-frontier coverage, uncertainty-cluster review burden, and page completeness. Do not reinterpret model-judge support as human-gold accuracy.
3. Add semantic-locator candidate gold for the synthetic suite, including default-negative cross-pairs, duplicate aliases, alternative-date groups, reschedule endpoint groups, and mixed authored/advertising/quoted messages. Sol should remain a disagreement diagnostic rather than the only source of truth.
4. Integrate the verifier and uncertainty sidecars into the Gmail ingestion orchestration. The current frontier, policy, and evaluation harness are production-shaped but do not yet cause ingestion to persist or route a temporal fact.
5. Label a fresh thread- and sender-grouped human holdout after the private development run. The existing formal promotion gate remains stricter than this exploratory personal-use target.

## Current Decision

Keep expression discovery broad and deterministic; keep temporal references event-centered; put ambiguity into a non-routable parent-cluster review state; and make model judgment classify a complete, validator-backed, expression-local candidate frontier. Separate evidential support from downstream normalization and identity deferral. Preserve subject bridges, cluster reducer-equivalent aliases, and page overflow losslessly. Keep supported-only precision measurable, but do not project uncertain alternatives as exact citations. Do not promote the whole-message endpoint selector, lead-only recall, heuristic auto-selection, silent candidate caps, model-authored temporal semantics, or independent per-arm judging of identical proposals.
