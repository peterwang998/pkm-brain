# Gmail Temporal Recall Iteration — 2026-07-21

**Status:** development evidence only; review-only and not approved for persistence, routing, reminders, or automatic application

**Target for this personal-use iteration:** at least 85% useful-record recall, at least 90% supported-proposal precision, and less than 1% critical semantic error, with recall favored when a proposal can remain visibly deferred
**Privacy:** every historical replay and artifact report is content-free; private JSONL artifacts are mode `0600`

## Outcome So Far

The remaining Gmail problem is association selection, not broad date discovery. The current deterministic analyzer still finds at least one temporal expression, subject mention, and candidate association in all 265 current messages classified as `important_temporal`. Relaxing the terminal-boundary schema alone did not reach the target. The implementation therefore moved to one-expression, segment-local selector packets, an explicit review-only rescue lane, and association-isolated deterministic reduction.

The production-shaped local pieces are implemented and regression-tested. A fresh external Luna/Sol pass over the full-text 120-message development cohort is not yet reported here: the execution security gate requires explicit informed approval before private Gmail text may be transmitted to those external models. No attempt was made to bypass that gate.

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

### Fresh full-text development cohort

A new 120-message, HMAC-opaque development cohort was rebuilt with untruncated source text. The deterministic planner produced:

- 672 recognized expressions and exactly 672 selector packets;
- 2,071 subject/lifecycle candidates, including 30 review-only event-title candidates;
- zero expression omissions under the hard packet caps;
- one anchored expression per packet, at most 16 mentions, at most four hints, and a 12,000-byte payload ceiling;
- 105 expression packets with no local subject mention, preserved for explicit selector abstention rather than silently discarded;
- 35 source-suppressed records eligible only for the non-routable temporal-rescue lane.

The cohort is still development data, not a frozen human holdout. Reanalysis of the exact saved text found zero stale endpoint inventories, zero invalid spans, zero context truncations, and zero packet omissions. Those checks demonstrate lossless expression planning under bounds; they do not demonstrate semantic quality.

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

### 5. Event-centered temporal memory

The selector output remains a non-routable evidence sidecar. A resolved occurrence should ultimately reference a stable event entity. Schedules, deadlines, cancellations, completions, and replacements should update an append-only event lifecycle only after event identity is reconciled. Ordinary durable facts remain ordinary facts; they are not required to carry a temporal object merely because their source email contains a date.

## Verification

- 120 focused lead/selection/batching/reduction tests pass.
- The complete repository suite passes: 1,297 tests.
- Ruff passes for all changed temporal modules and tests.
- The historical replay and fresh packet planner print aggregates only.
- The new planner and reducer are non-routable and make no external calls by themselves.

## What Remains Before A Release Claim

1. With explicit informed approval, run the 672 packets through restricted ephemeral Luna-medium and judge the resulting proposals independently with restricted ephemeral Sol-medium.
2. Report useful-record recall, supported-proposal precision, critical-error rate, rescued-record recall, and selected-noise rate. Do not reinterpret model-judge support as human-gold accuracy.
3. If proposal support remains below 90%, add a pairwise verifier over the selector's small candidate set rather than shrinking the discovery inventory.
4. If recall remains below 85%, inspect misses by admission, expression inventory, subject inventory, and selector abstention before expanding any grammar.
5. Freeze the winning code, prompt, schema, and model configuration, then label a fresh thread- and sender-grouped human holdout. The existing formal promotion gate remains stricter than this exploratory personal-use target.

## Current Decision

Keep expression discovery broad and deterministic; keep temporal references event-centered; put ambiguity into a review state; and move model judgment to small, exact, expression-local endpoint packets. Do not promote the whole-message endpoint selector, exhaustive edge graphs, heuristic auto-selection, or model-authored temporal semantics.
