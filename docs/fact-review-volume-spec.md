# Fact Conflict Precision & Review-Volume Budget — Spec

**Status:** implementation spec for Codex
**Last verified:** 2026-07-09 against the live brain DB (read-only snapshot) and implementation commit `841ba3e`
**Author:** Claude, from Peter's report: "most of the 'conflicts' I'm asked about are both true — the candidate usually *supports* the existing fact" + "~900 items to review is too high a volume"
**Companions:** `docs/chief-of-staff-spec.md` (action/policy model — unchanged), `docs/extraction-payload-spec.md` (extraction contract — unchanged), `docs/macos-app-spec.md` M4 Queue (consumes these improvements)

---

## 1. Diagnosis (measured on the live brain, 2026-07-09)

### 1.1 The backlog is not mostly "conflicts" — it's mostly escalation policy

`open_questions` with `status='needs_human'` = **757**:

| kind | count | what it actually is |
|---|---|---|
| `policy_escalation` | 454 | actions the policy wouldn't auto-apply — the escalation *question text is just "matched policy policy_0621e9f4…"* with no human-readable reason |
| `unrouted_fact` | 216 | routing fallback residue (entity-layer R3 leftover) |
| `fact_conflict_review` | 79 | the actual "candidate may contradict" questions |
| `document_extraction_anomaly` | 8 | real anomalies |

`cos_actions` with `status='needs_human'` = **749**: `fact_upsert` 689, `synthesize_page` 60.

So the volume problem decomposes into three independent causes: (a) false conflicts, (b) blanket policy escalation of routine upserts and *derived* synthesis text, (c) unrouted-fact residue. Each gets its own fix below.

### 1.2 The false-conflict mechanism (Peter's "both true" observation)

The resolver precheck is **purely lexical**: `facts_directly_conflict()` (`wiki_facts.py:1429`) fires when `has_material_contradiction_cues()` (`wiki_facts.py:1500`) sees (i) negation asymmetry, (ii) *any* differing extracted numbers/dates when both sides have them, or (iii) hits from antonym `MATERIAL_CONTRAST_GROUPS` — combined with shared "anchor" tokens. Sampled live questions confirm the failure class: compatible statements about the same entity (job-search progress updates, two true descriptions of Unity Catalog) escalate with *"Resolver precheck says candidate **may** contradict existing **nearby** fact(s)"*.

Statements that differ in numbers, dates, or contrast words are usually **temporal succession, refinement, or complementary detail — not contradiction**. A lexical test cannot tell these apart. That is the root cause.

### 1.3 The fix already half-exists

`extraction.py:~1630` contains an LLM conflict-judge prompt with **exactly the right rubric** — *"Return 'no_conflict' for unrelated facts, complementary facts, different attributes of the same entity, same-topic facts that can both be true, or lexical cue matches caused only by words like before/after, not, rather than, high/low, or different numbers."* But it does not gate all residue creation, and **nothing ever re-examines residue that already exists**. The work below is mostly *placement and coverage*, not invention.

---

## 2. W1 — Typed fact-relation classifier (kill false conflicts at the source)

Replace the boolean conflict verdict with a typed relation between a candidate and each counterpart fact:

```
duplicate      same claim                       → merge evidence into existing (exists today)
supports       adds evidence for existing claim → attach provenance to existing fact: union source_ids,
               append the new quote to source_spans / a relation record. NEVER modify the existing fact's
               statement or primary evidence_quote (single-field schema — merging quotes would be lossy)
refines        same claim + more detail         → propose refine-merge (keep both linked in v1; see below)
updates        temporal succession              → auto-supersede older when both dated/timestamped
complementary  different aspects, both true     → both facts active, no question
contradicts    cannot both be true              → fact_conflict_review residue (the ONLY class that asks)
unrelated      precheck was noise               → proceed normally
```

- **Stage 1 (deterministic, free):** exact/normalized duplicate (exists); negation-refinement pattern ("not just X" vs "X" → `refines`); dated-value succession (same entity+attribute, both statements carry timestamps/observed_at ordering → `updates`).
- **Temporal truth model:** relation decisions compare facts over their time window, not just their words. A fact can be true and still be non-current. Every fact card should expose an inferred `temporal_scope`:
  - `event` — happened once and remains historically true.
  - `current_state` — intended to describe the current state and may be superseded.
  - `interval_state` — true across a known or implied range.
  - `stale_observation` — was observed at a point in time but should not be treated as current by default.
  - `atemporal_claim` — durable statement without meaningful time variance.
  `updates` means the candidate becomes the current state while the older fact is preserved as historical/superseded evidence; it is not a contradiction unless both claims assert the same attribute over the same time window.
- **Stage 2 (LLM):** extend the existing judge prompt into this 7-way classifier. Reuse the critic's parallel-worker infrastructure (`extraction.py` critic config: 4 workers, timeouts, block-rate anomaly guard). Per-candidate cards, same shape as today's `counterpart_cards`. Low/medium effort tier.
- **Classifier confidence floor** (config, default 0.7): below it → residue, honestly labeled "classifier unsure".
- **Placement:** one shared module (`fact_relations.py`), called from BOTH the upsert precheck path (`wiki_facts.py` resolver) and the extraction conflict path — the lexical `facts_directly_conflict` becomes a cheap *candidate-pair finder* (high recall is fine), never a verdict.
- **Outcome mechanics are all existing deterministic paths:** evidence union, supersession with inverse, coexist. `refines` in v1 attaches the candidate as `supports` + tags the pair `refine_candidate` (statement *rewriting* stays out of scope — that's a later, riskier feature).
- **Every classification is recorded** on the action (`relation`, `relation_confidence`, `relation_rationale`) for audit and eval mining.

## 3. W2 — One-time backlog reconciliation (the volume killer)

Two independently-gated passes — the failure modes differ, so they get separate dry-run → approve → apply cycles:

- **W2a (classifier-dependent):** the `fact_conflict_review` + `policy_escalation`-on-`fact_upsert` sweep below. Requires W1 eval gates green first.
- **W2b (policy-only, no classifier dependency):** `synthesize_page` auto-apply drain + unrouted-fact Inbox batching (§4). **May land immediately, before W1 exists** — pure policy-row changes through the existing ledger + sampled-audit machinery.

`brain cos reconcile-backlog [--dry-run|--apply]` (W2a):

1. Sweep all `needs_human` `fact_conflict_review` + `policy_escalation`-on-`fact_upsert` items (plus their 689 pending actions) through W1.
2. `duplicate/supports/complementary/updates/unrelated` → auto-resolve: apply the mapped mechanics, dismiss the question with `resolution` set to the relation, leave the action ledger trail (revertable, as always).
3. `contradicts` + low-confidence → stay in the queue, now honestly labeled.
4. **Dry-run first, always**: report per-class counts + 20 random samples per class for Peter to spot-check in the Queue; `--apply` only after his go-ahead. Post-apply, the existing sampled-audit stage reviews a slice of the auto-resolutions; audit failures demote per the existing policy-demotion machinery.
5. `synthesize_page` needs_human items (60) are resolved separately by W3's policy change, not by the classifier.

**Target:** ≤ ~100 genuinely-human items survive out of today's ~900.

## 4. W3 — Strictness knobs & a review budget (keep it low forever)

### 4.1 Curation strictness presets (policy-engine native)

`config/local/config.yaml`:

```yaml
curation:
  strictness: balanced        # strict | balanced | lenient
  review_budget_per_day: 20
  conflict_confidence_floor: 0.7
```

Presets compile to **versioned `cos_policy` rows** (no new enforcement mechanism — the policy engine and its audit/demotion loop already exist):

- **strict** — today's behavior: escalate clean upserts, critic-gate everything.
- **balanced (default/recommended)** — clean `fact_upsert` with `relation in {duplicate, supports, complementary, unrelated}` auto-applies (critic-gated as in policy v4); `updates` auto-applies when dated; only `contradicts` asks. `synthesize_page` **auto-applies at L2 with sampled audit** — synthesis is derived, hash-stamped, revertable text; it should never have been blanket-escalated (this alone removes 60 items + the recent escalation stream).
- **lenient** — auto-apply everything except `contradicts` and extraction anomalies; audit sampling rate doubles.

Settings → Curation in the app exposes the preset + budget; changes write a new policy version (auditable, revertable).

### 4.2 Review budget + priority ranking

- At most `review_budget_per_day` new items surface into the Queue per day; the rest go to a **deferred pool**, ranked by `impact × uncertainty` (impact ∝ entity mention count + page centrality + confidence delta; uncertainty ∝ classifier confidence distance from floor).
- Deferred items use the existing `auto_resolve_after` column: safe classes age into policy auto-resolution; `contradicts` never auto-resolves — it waits, ranked.
- The Today digest and menu bar show "N in queue (M deferred)" so backlog growth stays visible without being a demand.

### 4.3 Unrouted-fact relief (the 216)

Fallback-routed facts stop asking individually: they auto-file to the target entity page's `Inbox` section (rendered, visible, provenance intact) and surface as **one weekly batch question per page** ("12 facts filed to Inbox on projects/hightouch.md — sweep them?"). Genuinely entity-less facts still ask.

## 5. W4 — Evals (gate everything above)

- **Fixture mining:** Peter's answered/dismissed questions (50 answered, 59 dismissed, incl. `both_true` decisions) + the W2 dry-run samples become the labeled set for a `relations` eval suite alongside the existing conflict suite.
- **Gates:** contradiction **recall ≥ 0.90** on labeled contradictions (never silently swallow a real conflict) and **false-conflict rate ≤ 0.10** on labeled compatible pairs, before W1 may gate residue in `--apply` mode or any preset looser than `strict` activates.
- **Ongoing:** weekly digest line — asked vs auto-resolved counts by relation; sampled-audit failures on auto-resolutions demote the policy version automatically (existing machinery).

## 6. W5 — Queue UX for what remains

- Conflict cards orient by **destination before candidate text**. The card title must identify the mapped entity/page/section/time frame, not simply repeat the candidate sentence. Required fields in `/api/queue` for fact review items: `orientation.entity_label`, `orientation.entity_key`, `orientation.page_hint`, `orientation.section_hint`, `orientation.candidate_observed_at`, `orientation.existing_observed_at`, `orientation.temporal_scope`, `orientation.currentness`, `orientation.relation`, and `orientation.relation_rationale`.
- Conflict-card header format: `{entity_label} / {section_hint}` plus chips for page path, relation, candidate observed time, existing observed time, and currentness. The candidate sentence remains visible in the candidate panel, but it is not the primary title.
- Decision keys gain relation-aware semantics and must be visible on the buttons, but conflicts use left-to-right numeric keys for speed:
  - `1` = keep existing / reject candidate.
  - `2` = candidate wins / replace current state.
  - `3` = both true / coexist.
  - `4` = supports existing — merge provenance only; never rewrite the existing statement or primary `evidence_quote`.
  - `5` = candidate current — the candidate describes the current state; existing fact(s) are marked superseded/historical.
  - `6` = unsure / skip for later.
  Number keys are context-sensitive: conflict cards use `1-6` for decisions; unrouted cards use `1-5` for route candidates.
- Group the queue by relation class with batch-accept per group ("accept all 14 supports-class items").
- Escalation cards must state the *reason* in words (replace "matched policy policy_0621e9f4…" with the rule's human label + the relation + rationale from W1).

## 7. Build order & acceptance

Sequencing rationale: the queue grew **308 → 757 needs-human items in two days** (2026-07-07 → 2026-07-09). Relief that needs no classifier must not wait for the classifier.

1. **Immediately, in parallel (no W1 dependency): W2b** — synthesize_page auto-apply at L2 + sampled audit; unrouted-fact Inbox batching (W3.3); human-readable escalation reasons (W5). Own dry-run report → Peter approves → apply. *Accept:* synthesize/unrouted backlog (~280 items) drains; new synthesize escalations stop; escalation cards state reasons in words.
2. **W1 classifier + W4 eval suite** (pure Python; eval-only — changes no live behavior until gates pass). *Accept:* relations suite green at the §5 gates on the mined fixture set; classifier fields recorded on actions; metrics reported to Peter.
3. **W1 activation for new residue** — new items ask only for `contradicts`/unsure. *Accept:* daily new-question rate drops to near the true-contradiction rate; missed-contradiction spot-audit clean.
4. **W2a reconciliation** — dry-run report with per-class samples → Peter approves → apply → sampled audit. *Accept:* needs_human ≤ ~100; audit sample of auto-resolutions ≥ 95% ok; every auto-resolution revertable from the ledger.
5. **W3 presets + W3.2 budget/deferred pool + remaining W5 UX.** *Accept:* balanced preset compiles to policy rows; new-item rate ≤ budget on a normal week; queue batch-accept works keyboard-only.

Hard rules (inherited): mechanics stay deterministic; the classifier only *decides*, existing action paths *apply*; every auto-resolution goes through the ledger with an inverse; contradictions and anomalies always reach a human; eval gates precede autonomy, and audit demotion can always walk a preset back to `strict`.
