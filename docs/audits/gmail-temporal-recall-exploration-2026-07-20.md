# Gmail Temporal Recall Exploration — 2026-07-20

**Status:** development evidence only; not approved for promotion, persistence, routing, or automatic application
**Models:** restricted ephemeral external Codex; `gpt-5.6-luna` medium for endpoint selection and `gpt-5.6-sol` medium for an independent aggregate audit
**Privacy:** private sample files were mode `0600`; model processes had no tools, network, workspace access, persistent history, plugins, apps, memories, or MCP servers; reports contain aggregate counts only

## Question

Can Gmail temporal recall improve substantially without weakening original Brain fact admission, allowing a model to invent time, or turning every fact into a temporal object?

The answer is yes for discovery and experimental review-candidate construction, but not yet at promotion quality. The best architecture is a layered, review-only endpoint selector behind deterministic evidence inventories and ahead of deterministic semantic validation. The repository implements the inventories, ranking hints, selector contract, and validator; it does not yet contain a production model call site or review queue. Exhaustive candidate graphs, heuristic “high-confidence” auto-selection, and model-authored temporal semantics all failed in distinct ways.

## Evidence Surfaces

The complete historical replay remains described in [Gmail Temporal History Evaluation](gmail-temporal-history-evaluation-2026-07-19.md). It processed 42,533 projection files, 14,227 distinct source revisions, 7,125 opaque thread lineages, and 356 currently fact-eligible threads without exposing private content. The general Gmail fact-admission rate remained 5.11%, close to the original Brain benchmark.

This exploration reused one HMAC-opaque, stratified 120-message cohort containing admitted durable/important mail plus routine and advertising negatives. Because the analyzer, prompt, selector schema, and repairs were iterated against this cohort, it is now a development set. It cannot satisfy the fresh human-grounded promotion gate.

The cohort is intentionally stratified and therefore does not estimate natural mailbox prevalence. Each external judge pass also made an independent materiality judgment, so small denominator changes across arms are judge variation rather than corpus changes.

## What The Experiments Established

### 1. Expression discovery was not the main bottleneck

The original direct grammar reached only 2 of 265 classifier-marked important-temporal current threads. The final bounded replay kept expressions, mentions, and at least one review hint in all 265. It produced 5,428 hints for that proxy cohort, but 5,408 were explicitly `review_ambiguous`; only 20 were strict or review-resolved. This is discovery breadth, not accuracy. On the first frozen audit, the independent judge found:

- a supported temporal expression in 89 of 95 material records;
- a supported event/action mention in 85 of 95;
- both endpoints somewhere in the inventories in 83 of 95, or 87.4%;
- a supported precomputed association edge in only 61 of 95, or 64.2%.

Association, not date recognition, was the principal recall loss.

### 2. Exhaustive graphs were the wrong abstraction

The broad analyzer retained 854 hints for the 120 records from 1,643 candidate edges, and 68 records required graph truncation. An attempted exhaustive-edge Sol audit became slow and internally inconsistent. The graph is useful only as a small ranking hint surface. It must not define what endpoint pairs the selector may cite. Hint construction is now bounded before Cartesian expansion; when endpoint sampling occurs, the analysis reports omitted expression/mention counts and explicitly marks `candidate_edge_count` as an evaluated lower bound rather than an exact graph size.

### 3. Model-authored semantics were unstable

The first constrained selector could cite any known expression/mention pair but also chose relation, planned/actual kind, lifecycle, normalization option, and confidence. Sol judged 57 of 83 material records supported (68.7%) and 78 of 123 proposals supported (63.4%), with 23 critical errors: 3 wrong events, 6 wrong lifecycles, 8 wrong relations, and 6 wrong time expressions. Only 18 records were safe for automatic application.

This was useful as a recall probe, not a safe temporal-memory writer.

### 4. Exact surfaces helped the model be selective, but did not make heuristics true

Adding verified endpoint surfaces reduced shotgun output from 123 to 92 proposals and covered 76 rather than 72 records. However, the arm also preselected every blocker-free `strict_direct` or `review_resolved` hint. Sol supported only 51 of 83 material records (61.4%) and 60 of 92 proposals (65.2%), with 29 critical errors.

The sharpest failure was the proxy “high-confidence” stratum: only 3 of 15 were supported and 12 had critical errors. All 15 records did contain one blocker-free trusted-looking pair; 14 deliberately had unknown planned/actual kind. The lesson is not to guess the kind. More importantly, blocker-free proximity is not human truth. Heuristic leads must remain ranking hints until a class is calibrated independently.

### 5. Post-hoc repair could not rescue a bad authority boundary

A post-hoc arm stripped non-subject lifecycle/boundary cues and derived labels after the old selector had already chosen endpoints. Proposal precision rose directionally, but review recall collapsed. Once a schema lets the model confuse a lifecycle cue with the event subject, deterministic relabeling cannot reconstruct the missing subject. The selector schema itself must distinguish subject evidence from lifecycle evidence.

### 6. Endpoint-only authority improved proposal support, not record coverage

The endpoint-only Luna arm removed heuristic preselection and asked the model to cite separate temporal expressions, event/action subjects, and optional lifecycle cues. Deterministic code owned every semantic field. On the same development cohort, Sol found at least one supported proposal in 57 of 83 material records (68.7%; Wilson 95% interval 58.1–77.6%) and supported 64 of 80 presented proposals (80.0%; interval 70.0–87.3%). These are independent-model support rates on reused development data, not human-grounded recall or precision. Relative to the first selector arm, record-level supported coverage was unchanged, the proposal support rate rose from 63.4% to 80.0%, and critical-error labels fell from 23 to 15. The remaining critical errors were concentrated in lifecycle/cross-event interpretation, not normalization authorship.

This is the best observed development arm because it improved judged proposal support without reducing judged record-level coverage. It still fails every promotion threshold. The final code review then made the validator more conservative than the judged projection: only event/event-predicate/deadline/action spans may be subjects; free-text confidence cannot be `high`; unknown kind, deadline ranges, multiple possible lifecycle subjects, anonymous evidence scope, and unsupported cues defer or fail closed. Global graph truncation is surfaced as a blocker and lowers confidence, but does not automatically defer an otherwise clean direct association. Those post-audit repairs require a fresh holdout rather than another promotion claim on this development set.

A final diagnostic projected the same endpoint citations through that stricter validator and asked a fresh Sol pass to judge the result. This was a validator remap, not a fresh selector run. Sol found at least one supported proposal in 53 of 85 independently judged material records (62.4%; Wilson 95% interval 51.7–71.9%) and supported 58 of 72 proposals (80.6%; interval 70.0–88.0%), with 14 critical-error labels: 9 wrong time expressions, 2 wrong lifecycles, 2 wrong relations, and 1 wrong event. The independent materiality denominator changed from 83 to 85, as expected across judge passes. The diagnostic coincided with eight fewer proposals and fewer lifecycle-error labels, but did not improve aggregate proposal support and had lower record-level coverage. Judge variation and the remap prevent attributing those changes to any one repair. Because it reused both the selector output and the development cohort, it is evidence for the remaining architecture gap, not a new quality claim.

## Best Development Architecture

The final design has five independent layers:

1. **Source-native structured lane.** Parse bounded `text/calendar` evidence before free text. Preserve UID, recurrence ID, sequence, method, status, DTSTART, DTEND, TZID, RRULE, exceptions, and organizer/attendee identity. This is the first candidate for eventual automatic application. The current encrypted archive retains exact RAW ciphertext but its normalized projection keeps only attachment descriptors, so this lane is not implemented yet.

2. **Untruncated deterministic candidate inventories.** Discover temporal expressions independently of classification and preserve every recognized candidate with exact spans, calendar-date alternatives, typed local wall time, timezone basis, resolution status, and blockers. Independently inventory recognized event, action, deadline, boundary, lifecycle, and artifact mentions. Admission may gate review associations but cannot change either evidence inventory. This prevents graph pruning from deleting recognized endpoints; it does not imply perfect expression or mention recall.

3. **Bounded ranking hints, not an exhaustive graph.** Keep a few direct, field-local, near-field, and subject/body bridge hints with explicit risk features, candidate counts, and truncation. Newline layout is a feature rather than an automatic hard boundary because real Gmail rendering frequently places labels and values on adjacent lines.

4. **Endpoint-only external selector.** Partition the lossless local inventories into deterministic, overlapping field/segment batches. Each call presents exact verified surfaces, bounded source-positioned context windows, a hard-bounded subset of endpoint IDs, and at most a few ranked hints. The caller must enforce both byte and endpoint ceilings and retain a content-free overflow diagnostic; it must never serialize an unbounded whole-message inventory. The selector may decide materiality and cite only:

   - one temporal expression ID;
   - one event, event-predicate, deadline, or action subject mention ID;
   - an optional lifecycle mention ID;
   - an optional matching hint ID.

   It cannot author normalization, relation, kind, lifecycle, confidence, dates, spans, source text, or explanations. A versioned analysis fingerprint and content-bound endpoint IDs prevent stale replies from rebinding to changed email.

5. **Deterministic semantic validator.** Derive the sole complete normalization when one exists; otherwise defer. Derive relation, planned/actual kind, lifecycle, blockers, repair flags, final decision, and confidence from cited evidence. Terminal boundaries, cancellation, and completion never become occurrence starts. Well-formed unknown or mismatched optional references fail soft, while malformed IDs, unknown core endpoints, and stale fingerprints fail closed. Every output is immutable, sidecar-only, and `routable=False`.

The implementation lives in `gmail_temporal_leads.py` and `gmail_temporal_selection.py`. It does not modify base-fact admission, default retrieval, event routing, or persistence.

## Gmail-Specific Additions Still Required

- Add quote-, forward-, field-, and message-aware segment identities so an endpoint cannot silently bind across unrelated quoted occurrences.
- Add a production selector batch planner with hard byte/endpoint ceilings, deterministic overlap, per-segment fingerprints, and explicit overflow accounting. The current repository contains the authority validator, not a production external-model call site.
- Add a review-only temporal rescue gate for strongly temporal mail rejected by the general fact classifier. This gate must be independent of fact admission and can never auto-apply. Sol repeatedly found a small number of filtered routine records that should have been admitted.
- Give events stable identity and record schedule, replacement, cancellation, and completion as an append-only lifecycle ledger. The first free-text pass may cite a lifecycle cue only to block or prioritize review; a separate reconciliation pass may update the ledger only after resolving a stable event identity. A generic broad `rescheduled` cue remains deferred until old and replacement endpoints can be distinguished deterministically.
- Parse structured ICS evidence from the encrypted RAW message. The current archive parser discards inline `text/calendar` content and stores only descriptors for attached calendar parts.
- Keep Chief-of-Staff operational state separate. Brain should supply evidence-backed recall; reminders, reply drafts, waiting state, and meeting-prep workflow state belong to the operational substrate.

## Promotion Evidence

Retire this 120-record cohort as development data. Freeze code, schema, prompt, model/version, and repair behavior, then build two new thread-grouped and sender/template-grouped sets:

- a natural-prevalence, time-split mailbox holdout for operational rates;
- a challenge set covering deadlines, zoned and unzoned times, ambiguous numeric dates, relative dates, multi-event mail, quoted chains, reschedules, cancellations, completion, ICS, ads, and routine notices.

Human-label assertion tuples `(subject, expression, relation, kind, lifecycle, normalization)`, double-label at least 25%, and adjudicate disagreement. Measure admission recall, inventory recall, exact tuple recall, proposal precision, normalization/timezone accuracy, lifecycle identity, negative review burden, temporal-query Recall@k, and original-Brain retrieval regression separately.

Required gates remain:

- 95% lower confidence bound of at least 85% for review association recall;
- 95% lower confidence bound of at least 95% for supported proposal precision;
- zero critical occurrence, timezone, cross-event, cancellation, or reschedule errors;
- zero ad/routine automatic applications;
- no more than two percentage points of base-fact or default-retrieval regression;
- automatic application only for a separately calibrated structured class with zero critical errors over at least 300 representative examples and a one-sided 95% precision lower bound of at least 99%;
- a live incremental canary that observes actual content-changing lifecycle revisions, not merely 72 elapsed hours.

## Decision

Adopt the endpoint-only, evidence-derived design as the Gmail temporal recall development path. Abandon exhaustive edge enumeration, model-authored semantics, and heuristic high-confidence preselection. Keep the implementation review-only and unpromoted until a fresh human holdout and a structured ICS lane satisfy their class-specific gates.
