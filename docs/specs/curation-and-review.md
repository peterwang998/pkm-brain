# Curation And Review

**Status:** canonical living feature spec
**Last verified:** 2026-07-11 against public release `0.1.1` snapshot `71a6462`
**Owns:** Chief-of-Staff roles, action/policy/audit flow, fact relations, review Queue, autonomy settings, and review-volume controls

## Operating Model

The Chief-of-Staff layer curates evidence into facts and managed pages while keeping durable mutations reversible:

```text
extract -> resolve -> garden -> synthesize
  -> propose action
  -> policy + optional critic
  -> auto-apply or human residue
  -> sampled audit
  -> demote/revert when gates fail
```

Ingest is not model-backed. Curation runs on changed sources in the nightly daemon job or an explicit `brain cos run`.

## Core Primitives

- facts: source-backed canonical claims;
- entities: identity across facts and routes;
- actions: universal reversible write path;
- policy: versioned ordered autonomy rules;
- page contracts: scope and retrieval intent;
- syntheses: optional derived prose;
- open questions: uncertainty and review residue;
- evals: promotion gates;
- audit: sampled post-apply control loop.

The action ledger covers fact, page, entity, contract, synthesis, topology, and review operations. Every applied mutation records enough prior state for a guarded inverse.

## Model Roles

Provider roles are independently configured under `config/local/cos_llm.yaml`:

| Role | Responsibility | Verified local Codex mapping, 2026-07-10 |
|---|---|---|
| extractor | propose atomic source-backed facts | `gpt-5.6-luna`, low |
| resolver | semantic fact/entity disposition | `gpt-5.6-luna`, medium |
| gardener | dispose page/entity topology candidates | `gpt-5.6-luna`, medium base; low/medium/xhigh per candidate |
| synthesizer | optional derived page prose | `gpt-5.6-luna`, medium |
| critic | independent pre-apply review | `gpt-5.6-luna`, medium |
| auditor | sampled post-apply review | `gpt-5.6-sol`, xhigh; Luna fallback |

These are deployment settings, not hard-coded product requirements. Role config may select another supported provider. An unconfigured role skips cleanly.

The provider doctor reports same-provider/model warnings for the current critic versus all Luna proposer roles. Critic separation is therefore role/prompt/process-level, not model-independent. The Sol auditor remains model-separated. This is an explicit cost/independence tradeoff to resolve before claiming independent critic judgment.

Model/effort changes require provider smoke tests and the owning eval suite, not a fact rebuild by default.

## Deterministic Mechanics, Gated Judgment

Deterministic mechanics always:

- build candidates and high-recall pair sets;
- enforce hard type, provenance, route, and blast-radius guards;
- match policy and eval state;
- write the action and exact inverse;
- apply, reject, revert, or retire state idempotently.

LLM or deterministic classifiers may judge relation or candidate disposition. They do not write tables directly.

Only truth-preserving exact operations are unconditionally safe, such as normalized-exact duplicate/source union. Lexical overlap alone is never proof of semantic identity.

Timeout, invalid output, critic disagreement, failed eval, or low confidence never silently selects a winner. The active caller may retain human residue or explicitly reject critic disagreement, but the disposition remains recorded on the action.

### Fact Critic Evidence Contract

For `fact_upsert`, critic review has three outcomes:

- `agree`: the current cited units directly entail the statement;
- `evidence_incomplete`: bounded same-chunk context directly entails the statement but the citation omitted required units;
- `disagree`: the statement remains unsupported, over-broad, contradictory, or misattributed after context review.

The critic sees the source document identity, current citation, up to four adjacent units on each side, stable transcript speaker labels, known participants, and bounded speaker self-identification context. Titles and participant lists are context rather than proof of a substantive claim.

An `evidence_incomplete` result may name up to five omitted or replacement unit IDs from the repairable context. Deterministic code unions omitted IDs with the current citation, reconstructs exact spans and quote text, and runs one fresh critic review. Only a final `agree` may apply; a second incomplete result becomes disagreement. Repair metadata and both reviews remain in `evidence_json`.

Fact application falls back to deterministic entity resolution when optional LLM entity disambiguation returns invalid output. Parallel critic batches isolate an unhandled worker failure to that action and mark it `failed` with diagnostic evidence rather than aborting unrelated settled actions.

## Policy And Risk

`cos_policy` is versioned. A proposal records the matched policy version, risk tier, confidence, and any critic/audit evidence.

Risk principles:

- low: exact/high-certainty, reversible, narrow impact;
- medium: reversible but semantically or structurally non-trivial;
- high: contradiction, missing provenance, failed gate, ambiguous cross-type work, large topology, or critic disagreement.

Low/medium work may auto-apply only under the active policy and required critic/audit gates. High risk reaches a human.

Audit failures can trigger guarded revert. Automated policy demotion is allowed only when the audited action records a concrete `policy_id` and `policy_version`, and that policy rule's aggregate sampled-bad rate exceeds its own threshold. Unscoped legacy/W2a actions remain visible as audit findings but cannot demote policy because they provide no rule attribution.

A demotion maps the breached historical rule to its equivalent family in the active policy and raises only that rule to L3. Every unrelated rule retains its autonomy level, critic requirement, timeout behavior, and sample rate. If the breached rule is already L3 or no unambiguous current rule can be identified, no policy version is created. A caller flag must not bypass policy.

`brain cos reconcile-policy-escalations` is the repair path for questions created under a superseded policy. It is dry-run by default and reports the current decision for every linked action. Apply mode re-decides only current L0-L2 actions through the normal policy, critic, action ledger, and inverse mechanics; critic disagreement defaults to rejection. Genuine current-L3 work remains open, terminal-action questions close as stale, and failures remain reported rather than being hidden. Changing the Settings autonomy mode alone still does not reclassify the existing Queue.

## Fact Relations

A candidate-to-counterpart relation uses this vocabulary:

| Relation | Meaning | Default outcome |
|---|---|---|
| `duplicate` | same claim | union provenance into canonical fact |
| `supports` | additional evidence for same claim | union supporting provenance without rewriting primary statement/quote |
| `refines` | compatible claim with more detail | preserve both; tag refinement/support relationship |
| `updates` | later state replaces current state | preserve history; supersede older current-state fact |
| `complementary` | different aspects can both be true | keep both |
| `contradicts` | cannot both be true in the same time scope | human review |
| `unrelated` | lexical pair finder was noise | proceed independently |
| `unsure` | confidence below gate | human review |

Temporal scope distinguishes event, current state, interval state, stale observation, and atemporal claim. A later observation is not automatically a contradiction.

`fact_relations.py` is the shared classifier. High-recall lexical cues may find pairs but may not decide contradiction. Classification records relation, confidence, rationale, and classifier version for audit.

The current gated deterministic relation suite passed contradiction recall 1.00 and false-conflict rate 0.025 in the July 11 UTC W2a run. Broader LLM relation classification remains policy/eval gated.

### Conflict Admission

The Conflict Queue is reserved for direct, pairwise incompatibility. A candidate may enter `fact_conflict_review` only when:

1. a specific existing fact is in the candidate's entity or page scope;
2. the deterministic relation classifier labels that pair `contradicts` at confidence 0.70 or greater; and
3. the resolver confirms that the two statements cannot both be true under the same entity, topic, time, and scope.

Same entity, same page, lexical opposition cues, different numbers, an already-contested nearby fact, or insufficient context are not conflict evidence by themselves. `complementary`, `unrelated`, `supports`, `refines`, and resolver `no_conflict` outcomes proceed independently through normal fact policy. Resolver failure remains fail-closed only for a pair that already passed the deterministic contradiction gate.

`brain cos reconcile-conflicts` rechecks historical active cards against this contract. It is dry-run by default. Apply mode narrows confirmed conflicts, closes false conflict questions, and sends released candidates through the current fact policy and critic rather than bypassing either control.

## Review Queue

The Queue aggregates existing state; it is not another persistence layer. Sources include:

- open questions;
- policy-gated topology actions;
- proposed memories;
- sampled-bad audit actions;
- batched unrouted inbox work.

Every decision dispatches to the owning action, question, memory, or routing primitive and returns an undo/reopen handle when supported.

### Identity And Lifecycle

- topology proposals have deterministic `candidate_key` identity;
- at most one open action is visible per key;
- applying/rejecting/obsoleting a candidate retires same-key siblings;
- stale retries return a successful already-handled/obsolete result where the target state already exists;
- the read surface rechecks preconditions and never presents an obsolete action as approvable;
- persistent cleanup of historical duplicate rows must be auditable.

### Complete Cards

A reviewer must not leave the card to decide.

- Policy: action type, candidate claim, route/entity, quote/source, relation/rationale, and human policy label.
- Candidate-versus-existing conflict: candidate and every counterpart with canonical evidence, time, route, relation, and currentness.
- Historical conflict group: `comparison_mode: alternatives`, at least two ordered and hydrated peer facts, and a nonempty `select_facts` subset. These facts are never labeled as candidate/existing because the legacy resolver did not preserve directional candidate lineage. Every selected fact becomes active and human-confirmed; only unselected facts are superseded. Selecting all is a convenience, not a separate all-or-nothing decision.
- Page split: source page and every resulting child path, section/fact counts, representative facts, moved total, and rationale. Missing preview disables approval.
- Entity merge: both names, aliases, statuses, fact counts, and merge direction before IDs.
- Memory/audit/anomaly: content, scope/type, provenance, finding, and exact proposed effect. A topology audit also shows merge direction, current page/contract statuses, affected counts, and representative facts, including an explicit zero-fact state.

Every fact card shows a labeled source date. `source_date` is the fact's `observed_at` when present; otherwise it is the newest source-document `captured_at`, `created_at`, or `ingested_at` timestamp in that order. The raw ISO timestamp remains inspectable, chunk provenance resolves to the owning document, and a missing date is shown explicitly rather than silently replaced with the Queue item's creation date.

A candidate-less candidate-versus-existing decision is invalid UI state. The server must either hydrate the candidate or mark the card non-approvable. Legacy `kind=conflict` groups are an explicit exception because they are symmetric comparisons: the Queue hydrates their untyped options and fact IDs into `alternatives`, labels the orientation `contested`, and never renders an empty Candidate panel.

Unrouted route choices include only active semantic Wiki pages. `reference` and `index` page types, `references/*` and `agent_session_log/*` paths, `index.md` and `log.md`, malformed page types, and titles containing the internal Codex-provider prompt are excluded both when loading the route pool and when scoring final candidates. Returned route labels are whitespace-compacted and bounded to 120 characters.

### Anomaly And Audit Semantics

A `document_extraction_anomaly` is a document-level quality alert, not a proposed knowledge mutation. It identifies the source document, reviewed sample size, blocked count, and block rate. The default alert requires at least five critic-reviewed facts, avoiding noisy 3/3 samples; the local threshold remains configurable. Because it has no linked action, its valid decisions are Confirm Quality Issue, False Positive, and Later; the generic Approve path must never be shown or dispatched. Confirm Quality Issue records the compatible `acknowledged` disposition, while False Positive records `dismissed`; neither mutates facts or reruns extraction.

An `audit_flagged` Queue item means the post-apply auditor sampled an already-applied action and marked it `sampled_bad`. The underlying action type remains visible for provenance. In particular, `audit_flagged fact_upsert` means an applied fact insert/update failed sampled audit; it is not a new fact proposal. The card shows the auditor rationale, the applied change, affected object counts, and enough current evidence to evaluate that change without opening another screen.

Audit admission is state-aware and is shared by Queue rows, Queue counts, and direct decision lookup:

- if the current target-state hash still equals the action's recorded applied-state hash, the finding remains reviewable and Revert executes the guarded inverse;
- if a topology action's target state has drifted, its historical action-level finding is obsolete and is excluded rather than offering an unsafe or misleading revert;
- if a `fact_upsert` target has drifted but the exact audited statement still exists as an active or contested fact, the finding remains reviewable against that current fact and Reject Applied Fact records a targeted, reversible fact-status correction through the action ledger;
- if the audited fact no longer exists in a reviewable state, the finding is excluded;
- Keep Applied Action/Fact records a new `sampled_ok` audit result and never reapplies the original action.

A guarded revert that was refused for state drift must not leave both a failed sampled-bad action and an unhelpful generic `revert_drift` card. Reconciliation may restore the original action's applied status only when the exact audited fact is still active, auto-resolve the linked drift residue with explicit evidence, and return the action through the state-aware audit path.

The current working tree implements `approvable`, `blocking_code`, and `blocking_reason`, validates again inside the decision endpoint, and reports active/actionable/blocked totals in a freshness-ordered Queue summary. Blocked cards are retained for diagnosis but excluded from the default review backlog. The maintenance audit command remains planned.

### Counts, Filters, And Paging

- `/api/queue` defaults to `state=actionable`; `state=blocked` returns only Needs Repair cards and `state=all` is the diagnostic union;
- every nonzero kind filter returns cards from the requested state;
- `total` counts retrievable items in the requested state, after deduplication;
- clients show loaded and total separately;
- Queue pages are bounded and use cursor/load-more behavior;
- a global badge and the open Queue must derive from the same server generation or reconcile immediately after load/decision.
- changing Queue kind, Review/Needs Repair state, sort, or daemon generation immediately replaces the old list/detail with an opaque loading state; each request captures its requested parameters and a late response may not overwrite a newer selection.

Blocked does not mean disposable. Missing payload, provenance, relation, or topology context remains inspectable under Needs Repair and cannot mutate knowledge. A maintenance flow may close a blocked item only when it records proof that the item is obsolete, already handled, or deterministically repaired; the default Queue never deletes it merely to reduce the visible count.

### Ordering And Confidence

Server-side sorts run before pagination:

- `retrieval`: most distinct retrieval exposure events;
- `priority`: risk/kind policy order;
- `newest`: creation/observation time.

Popularity is advisory only.

Confidence bands are:

- High: 85-100%, green plus check;
- Medium: 65-84%, amber plus neutral/minus cue;
- Low: below 65%, red plus warning.

Every band includes a numeric value and non-color cue.

### Decisions

Conflict decision semantics:

1. keep existing / reject candidate;
2. candidate wins / replace current state;
3. both true / coexist;
4. supports existing / union provenance only;
5. candidate current / supersede older state;
6. unsure / leave for later.

Historical symmetric comparisons use a different control because there is no trustworthy candidate/existing direction: numeric keys toggle facts, `select_facts` keeps any nonempty subset, and Enter applies the selection. Native numeric and Return shortcuts are attached to the actual SwiftUI controls so they work regardless of which Queue button currently owns focus. The older single `select_fact` and all-facts aliases remain API-compatible but are not the primary UI.

Numeric keys are context-specific and visible on controls. Batch actions may operate only over a homogeneous, relation-aware selection with a preview and undo/ledger record.

## Autonomy Settings

The native Settings view and `GET|PUT /api/settings/curation` expose:

| Mode | Config value | Minimum future auto-management confidence |
|---|---|---:|
| Review First | `strict` | 0.95 |
| Balanced | `balanced` | 0.80 |
| More Autonomy | `lenient` | 0.60 |

Applying a mode:

- writes `curation.strictness` and `minimum_auto_confidence` in local config;
- records `curation.updated_at` for the user-visible last-saved time;
- appends a versioned policy set;
- affects only actions proposed/decided in future jobs;
- does not rerun extraction/gardening, rebuild facts, mutate existing facts, or drain/reclassify the current Queue.

The native Settings surface does not expose the internal policy version. Its header shows `Last saved <date/time>`, and a successful write reports `Changes saved at <time>`. The API may retain `policy_version` for diagnostics and compatibility, but that implementation identifier is not user-facing state.

Hard boundaries do not move: contradictions, missing quote, invalid/fallback route, failed eval, cross-type merge, and large topology remain review work.

The same endpoint exposes independent `merge_aggressiveness` and `split_aggressiveness` values from 0.0 through 1.0. The native Settings view combines them into one Topology Bias slider: Prefer Splits on the left, Balanced in the center, and Prefer Merges on the right. Moving right raises merge admission and lowers split admission by the same amount; moving left does the inverse. Existing non-complementary API/config values are summarized into a bias for display and are not rewritten until the reviewer moves and applies the control. These values change deterministic candidate admission only, not whether an admitted action may auto-apply.

Anchor behavior is:

| Setting | Conservative `0.0` | Balanced `0.5` | Aggressive `1.0` |
|---|---:|---:|---:|
| Page merge path/evidence floors | 0.96 / 0.35 | 0.86 / 0.25 | 0.76 / 0.15 |
| Page merge alternate path/evidence floors | 0.70 / 0.55 | 0.60 / 0.45 | 0.50 / 0.35 |
| Fuzzy entity merge name/evidence floors | 0.96 / 0.25 | 0.88 / 0.20 | 0.80 / 0.15 |
| Page split minimums | 12 facts, 5 sections, 3 facts/section | 5 facts, 3 sections, 1 fact/section | 3 facts, 2 sections, 1 fact/section |

Exact normalized-name and punctuation/spacing-equivalent entity duplicates remain eligible at every merge setting because they are deterministic identity signals. Below the balanced midpoint, fuzzy name-containment pairs require progressively stronger name similarity and shared-source/fact evidence; this prevents broad names such as an organization from automatically matching a project or POC name. The split side currently governs page splits only; the gardener does not generate automatic entity-split candidates.

Applying the topology-bias control writes the underlying merge/split values to local config and affects only future gardener runs. A topology-only change does not append an autonomy policy version, run the gardener, rebuild facts, alter existing actions, or remove current Queue items. Changing autonomy mode and topology bias together still appends a policy version only for the autonomy change.

`brain cos reconcile-topology` is the explicit repair path for old merge/split proposals; it remains separate from the Settings write. Dry-run mode groups open rows by deterministic `candidate_key`, reports duplicates, and compares each unique candidate with current page/entity evidence and current topology settings. Apply mode retires no-longer-admitted candidates, reruns gardener judgment for survivors, prefers non-overlapping merges over splits touching the same page, refreshes one canonical action per candidate, dismisses duplicate rows, and sends every survivor through the current policy and critic path. Large topology remains L3 human-review work.

New gardener runs use the same sequence. Deterministic admission is followed by per-candidate gardener judgment and merge-first overlap arbitration; `shadow=False` proposals are policy-decided instead of being left as unclassified `proposed` rows. Page topology actions use a report-backed `{"suite": "topology"}` eval gate rather than a caller-supplied failed gate.

The live setting read during this verification was More Autonomy, policy v12, floor 0.60, with merge admission 0.80 and split admission 0.20. Policy v12 restored the intended profile after unscoped legacy audit findings had incorrectly raised every rule in v10 and v11 to L3.

## Review Volume

One-time W2a, pairwise conflict, policy, topology, extraction-evidence, and audit repairs reduced legacy residue. Their measured counts and exact apply results are chronology, not product invariants, and live in [Implementation Stream History](../archive/implementation-stream-history.md).

Schema migration 21 adds a durable `review_admissions` ledger and bootstrap marker. On first use, every existing approvable card is admitted so deploying the feature does not hide the current backlog. After bootstrap:

- ordinary future work admits at most 25 items per UTC day;
- ordinary active work has a 100-item ceiling;
- confirmed Conflict cards and all high-risk cards bypass both limits;
- excess ordinary work is durable and visible in the Deferred Queue state;
- resolving admitted work promotes deferred items in priority order when the next daily budget is available;
- blocked/incomplete cards remain in Needs Repair and do not consume deferred admission;
- missing admission metadata fails open for a card rather than hiding it.

Admission priority currently orders group/risk deterministically. Retrieval popularity remains a user-selectable Queue sort, but an `impact x uncertainty` score is not yet persisted as the admission priority.

Queue summaries expose active/actionable/blocked/deferred totals, per-group/raw-kind deferred counts, active limit, daily limit, and admissions used today. Today, menu bar, Queue, Ops, native, and browser clients consume the same server summary.

## Pending Controls

Not implemented:

- `impact x uncertainty` admission into the daily Queue;
- aging safe deferred classes through `auto_resolve_after`;
- relation-aware batch acceptance;
- historical comparison migration beyond the existing repair commands.

Contradictions must never age into automatic resolution.

## Eval And Audit Gates

The relations gate requires at least:

- contradiction recall >= 0.90;
- false-conflict rate <= 0.10.

Autonomy promotion also requires the relevant extraction, routing, topology, conflict, or retrieval suite. Sampled audit reviews auto-applied work and may demote policy.

## Acceptance

- All durable mutations use `cos_actions` and have an inverse.
- Re-proposing a candidate key produces one active review item.
- A stale merge retry cannot throw or reappear as work.
- Every visible filter count equals retrievable cards.
- Every approvable card contains enough evidence to decide.
- Every new conflict card names a resolver-confirmed, directly contradictory counterpart.
- Queue global badge and page total reconcile on load and after decisions.
- Popularity changes order only, not truth/policy.
- Settings change future policy only.
- W2/reconciliation commands are dry-run by default and require explicit apply mode.

Verification:

```bash
uv run brain eval run --suite relations --home <test-home>
uv run brain cos queue-summary --home <test-home>
uv run pytest tests/test_cos.py tests/test_fact_relations.py \
  tests/test_fact_review_volume.py tests/test_ui_endpoints.py -q
```
