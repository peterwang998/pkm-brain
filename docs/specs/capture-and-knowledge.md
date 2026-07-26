# Capture And Knowledge

**Status:** canonical living feature spec
**Last verified:** 2026-07-16 against the temporal-cognition working tree based on rollback commit `d5405b9`; the parity-first contract targets migrations 22-24 and extractor v12 in the isolated Brain v2 home. The live Brain home was not migrated, backfilled, or modified.
**Owns:** evidence connectors, ingest, source normalization, durable-knowledge extraction, facts, entities, routing, gardener topology, and wiki projection

## Feature Boundary

This feature turns private source material into durable, reviewable knowledge:

```text
connector/manual input -> inbox -> raw/document/chunks
  -> candidate facts -> validated provenance -> actions
  -> facts/entities -> managed wiki pages
```

Capture adapters and model roles never bypass ingest or write knowledge tables directly.

Operational awareness is a separate consumer of approved evidence. Calendar and Gmail can also feed operational observations, reconciliation, and a briefing without turning those observations into facts. The lifecycle and persistence contract for that path lives in [Chief-of-Staff Operations](chief-of-staff-operations.md).

## Capture Contract

Current capture adapters wrap Codex, Claude, OpenCode, Hyprnote, and filesystem input. Gmail, Calendar, and Slack are registered as `auth_only` capture connectors: they expose account authorization but have no inbox discovery, capture scheduling, or knowledge-ingestion behavior. Separately approved Calendar evaluation and scheduled Gmail operational-mirror paths consume bounded evidence through [Chief-of-Staff Operations](chief-of-staff-operations.md); those paths do not make either connector capture-capable. A capture-capable connector:

- writes normalized Markdown or text under its `inbox/<connector_id>/` namespace;
- advances `capture_sources` state only after a successful unit;
- reports health independently so one failed connector does not abort the tick;
- does not write `raw/`, facts, wiki rows, indexes, or action rows.

Agent-session snapshots are latest-state sources. Origin and logical source path prevent collisions across machines. The connector registry is extensible, but loading third-party code from outside the package is not implemented.

Connector lifecycle is explicit:

- `active`: scheduled or manually runnable capture;
- `passive`: an inbox landing surface such as Files, with no external source to poll;
- `auth_only`: account authorization exists, but capture cannot be enabled or run.

Codex sessions whose title or user message begins with the pkm-brain provider-wrapper prompt are self-generated workflow traffic and are excluded during capture. Historical generated reference pages may predate this guard; they remain inspectable source artifacts but are never valid fact-routing destinations or Queue route candidates.

Hyprnote capture is speaker-aware. Transcript words from independently stored channels are ordered by absolute transcript start plus word time, grouped into bounded turns, and rendered with stable `Speaker N` labels. Known calendar participants are retained as context but never treated as a speaker-name mapping by themselves. Provider speaker hints may separate multiple voices on one channel. Legacy recordings whose channels reuse the same synthetic word clock cannot be interleaved safely; those sources carry an explicit transcript note and remain grouped by speaker track instead of presenting invented turn order. The Hyprnote renderer version participates in capture state so a rendering-contract change recaptures an otherwise unchanged source.

Hyprnote remains opt-in because it scans private meeting data under the application's support directory. Files does not supersede it: Files only represents artifacts already placed under `inbox/documents/`, while the Hyprnote adapter creates normalized meeting artifacts from Hyprnote session folders. Previously captured Hyprnote artifacts remain ingestible when scheduled Hyprnote capture is disabled.

Email capture and durable-knowledge ingestion are not implemented; their proposed contract is isolated under [Future Gmail And Email Adapter](#future-gmail-and-email-adapter). The Gmail operational lane and encrypted local archive are independent evidence paths and do not write documents, chunks, facts, or wiki pages.

## Ingest Contract

Ingest copies or normalizes source artifacts into `raw/`, creates `documents` and `chunks`, maintains FTS, and attempts vector writes through the configured embedding provider.

Rules:

- content-derived IDs and hashes make repeat ingest idempotent;
- migration 20 source size/mtime statistics allow unchanged files to skip re-hashing;
- source artifacts remain inspectable and are never replaced by facts;
- vector failure does not block document, chunk, or FTS writes;
- quarantine and retry paths preserve failed inputs for diagnosis;
- indexes can be rebuilt from SQLite and raw files.

Chunking currently targets about 1,200 approximate tokens. The sentence-transformer vector path embeds a heading-prefixed, model-bounded representation; it does not silently change chunk boundaries.

## Extraction Selection

Extraction is document-scoped and windowed. Changed documents that pass source-type policy are split into windows with:

- up to 6 chunks per window;
- 1 chunk of overlap;
- all chunks covered at least once;
- bounded worker concurrency;
- low-information windows removed before an LLM call.

An explicit document-ID filter is available for bounded repair/reprocessing runs. It composes with source-type policy and never broadens selection beyond the named active documents.

`agent_session_log` is skipped by default. Ordinary notes, meetings, transcripts, web clips, and unknown non-noisy sources are eligible unless local policy says otherwise.

The source policy lives in `config/local/cos_llm.yaml`. The verified local extractor deployment on 2026-07-10 is Codex `gpt-5.6-luna` at low effort with four workers. Provider configuration remains user-overridable.

An in-place prompt migration may explicitly fence already-processed documents while leaving Gmail Knowledge extraction disabled:

```yaml
extraction:
  compatible_terminal_prompt_versions:
    - extractor-evidence-units-v12-parity-recovery
  source_types:
    gmail_thread:
      extract: false
```

The compatibility list is empty by default. It accepts only exact, older extractor prompt identities and treats only their successful `ok` or `ok_with_rejections` watermarks as terminal under the current v15 selector. Empty, invalid, current, future, malformed, and unlisted prompt watermarks are still revisited. Operators should add an older prompt only after deciding that preserving its successful extraction is safer than replaying it during that migration. Source policy is independent: `source_types.gmail_thread.extract: false` prevents eligible Gmail Knowledge documents from entering fact extraction at all.

This selector answers whether a source should enter the durable fact pipeline. It is not an operational-relevance classifier. A thread can be ineligible for facts and still contain an important reservation, renewal, delivery, deadline, cancellation, or payment signal.

## Extractor Payload

One call receives one source window:

```json
{
  "document": {
    "document_id": "doc_...",
    "title": "Document title",
    "source_type": "markdown_note",
    "source_id": "document:doc_...",
    "content_hash": "normalized:..."
  },
  "window": {
    "window_id": "doc_...:window:0",
    "chunks": [
      {
        "chunk_id": "chunk_...",
        "heading_path": "",
        "units": [
          {
            "unit_id": "u0",
            "text": "Deterministic source unit.",
            "speaker": "Speaker 1"
          }
        ]
      }
    ]
  },
  "routing_hints": [
    {
      "page_hint": "concepts/example.md",
      "canonical_entity": "Example",
      "page_scope": "What belongs here",
      "retrieval_purpose": "Why this page is retrieved"
    }
  ]
}
```

The response proposes a `facts` array. Each fact includes:

- atomic `statement`;
- known `chunk_id` and one or more `evidence_unit_ids`;
- closed-enum `claim_class`;
- preliminary page/section route;
- entity mentions with surface, closed type, mention kind, and primary flag;
- extraction, routing, and truth confidence;
- optional predicate-valid time when the proposition itself has an explicit world-valid interval;
- at most one optional `event_time` object when the fact is primarily about one named event entity.

The extractor must not author `evidence_quote`, `source_spans`, or offsets. It also never receives the existing fact table.

`speaker` is optional deterministic metadata. A stable label may be attributed to a person only when the source window directly establishes the mapping through self-identification or direct address. A known-participant list alone is insufficient; unresolved attribution remains `Speaker N`.

## Deterministic Validation

Validation is per proposed fact, even though a window response is bulk.

Accepted provenance is constructed from cited source units:

- unit IDs must belong to the cited chunk;
- at least one unit is required;
- citations are bounded at five units rather than allowed to grow without limit;
- adjacent units form one source span; separated units remain multiple spans;
- `evidence_quote` is cached from exact source text;
- genuine quantities in the statement must be supported after numeric normalization;
- entity-surface support is advisory and may consider the full window.

Invalid durable candidates receive one narrow repair attempt when the payload defect is repairable. Statement-faithfulness failures are terminal. Accepted candidates from both attempts are deterministically deduplicated.

Non-durable classes are dropped without retry:

- `event_metadata`
- `transcript_mechanic`
- `pleasantry`
- `boilerplate`
- `non_claim`

"Dropped" here means excluded from the durable fact ledger. The raw evidence remains searchable, and an independently approved operational detector may represent time-bound logistics in `ops.sqlite`.

Durable classes include decisions, commitments, preferences, roles/responsibilities, project state, factual updates, and open questions.

Mechanical meeting headings, capture frontmatter, known-participant lists, explicit no-summary/no-memo/no-transcript placeholders, and wrapped claims that merely restate those placeholders do not count as extractable content. A metadata-only meeting terminates as `extracted_empty` without a provider call.

Watermarks distinguish:

- `ok`: accepted semantic LLM candidates existed with no rejection residue;
- `ok_with_rejections`: accepted semantic LLM candidates existed alongside bounded rejection residue;
- `extracted_empty`: extraction succeeded but found nothing durable;
- `invalid`: durable attempts failed validation.

Both `ok` and `extracted_empty` suppress unchanged re-extraction. `invalid` remains retryable.

Each source window is an independent provider boundary. JSON repair retains the complete original prompt—including source evidence at its tail—and preserves required top-level keys. If the extractor still returns malformed or schema-incomplete JSON after bounded retries, that window records an `extractor_provider_error`, contributes no candidates, and leaves the document watermark `invalid`; other windows and downstream nightly stages continue. A substantive window that returns an empty `facts` array receives one simpler coverage retry. A document with accepted semantic LLM candidates plus only deterministic non-retryable rejections is terminal `ok_with_rejections`, preventing already-applied candidates from replaying. A structured event projection does not count as semantic extractor success and cannot mask a failed LLM pass. Programming errors still fail the run rather than being mislabeled as model output.

The v12 parity-recovery prompt intentionally revisits older watermarks once for the same normalized content and extractor model. Its complete contract is reused by initial, JSON-repair, validation-retry, and coverage-retry paths. Unsupported durable claim labels normalize to the nearest fixed class (or `factual_update`), while malformed optional entity annotations are stripped with diagnostics instead of discarding their evidence-backed base fact. Current v12 `ok`, `ok_with_rejections`, and genuine `extracted_empty` rows remain terminal; invalid/provider-failed work remains retryable.

## Fact Ledger

A fact is an atomic claim with lossless provenance and lifecycle behavior. It earns its place over a raw chunk only when:

1. its quote and source spans are traceable to evidence; and
2. active/superseded/conflicted/retracted state changes retrieval behavior.

`facts` carries statement, source IDs/spans, evidence quote, route, confidence fields, entity cache, extraction metadata, timestamps, and status. Exact duplicate/source-union mechanics are deterministic. Semantic equivalence, refinement, temporal update, and contradiction use the relation/policy flow in [Curation And Review](curation-and-review.md).

## Temporal Fact Contract (Parity-First Brain v2)

Temporal cognition enriches the existing fact ledger; it does not redefine what counts as a fact. Extractor v12 keeps the original atomic-claim, evidence, routing, and confidence contract unchanged while making malformed claim-class/entity annotations recoverable metadata errors. A base fact that passed the evidence gates remains eligible even when an optional entity or temporal proposal is absent, incomplete, or malformed.

Migrations 22-23 retain optional predicate-valid fields and assertion revision lineage. Migration 24 adds one narrow event-time shape to the same row. No migration creates a second fact ledger, canonical events table, graph database, daemon, or model role.

### Universal Provenance And Knowledge Clocks

Every fact participates in two deterministic clocks:

| Clock | Fields | Meaning |
|---|---|---|
| Source observation | `observed_at` | When the source asserted or published the claim. The field is null only when trustworthy source-native time is unavailable. |
| Brain knowledge | `created_at`, `knowledge_to` | When Brain first persisted the exact assertion and, if applicable, when Brain stopped accepting that revision as correct. |

`created_at` is always assigned by persistence. `knowledge_to` stays null while an exact revision is open. Before a semantic fact update, the current row and entity links are copied to an immutable closed revision whose `knowledge_to` is the update time; the stable fact ID then advances to the next open revision. Processing, extraction, reconciliation, or job time must never masquerade as `observed_at`.

### Optional Predicate Validity

`valid_from`, `valid_to`, `valid_time_precision`, `temporal_expression`, `temporal_confidence`, and the compatibility `temporal_kind` describe when a proposition is true in the world. They are optional enrichment, appropriate for explicit state intervals such as “served as CEO from 2021 through 2024.” They are not required for ordinary durable claims, and the extractor should omit them rather than force every fact into a temporal class.

When present, bounded intervals use `[valid_from, valid_to)`, normalized values use ISO 8601, and source precision is preserved. Existing `effective_at` remains a compatibility mirror, not another clock. Migration 22 never guesses validity from legacy rows.

### Events Are Entities With One Optional Event Time

An event is an ordinary `entities` row with `entity_type='event'`; there is no parallel canonical `events` table. A fact that is primarily about exactly one event may add:

```json
{
  "event_time": {
    "kind": "actual",
    "start_at": "2026-07-15T09:00:00-07:00",
    "end_at": "2026-07-15T09:45:00-07:00",
    "precision": "exact",
    "expression": "July 15 from 9:00 to 9:45 AM PT"
  }
}
```

The contract is intentionally small:

- `kind` is `actual` or `planned`;
- `actual` requires `start_at` and may add `end_at`; `planned` requires at least one of the two ISO bounds; when both exist, start precedes end;
- `precision` is `exact|day|month|year`;
- exact timestamps are canonicalized to UTC for persistence and identity while partial dates retain their source precision;
- optional `expression`, when present, preserves the literal source phrase supporting the normalized value;
- the containing fact must have exactly one primary `event` entity;
- one atomic fact has at most one `event_time`; multiple schedules or revisions are separate facts/revisions.

`actual` covers occurrence. `planned` covers schedules and may encode an absolute deadline with only `end_at`, an absolute not-before boundary with only `start_at`, or a planned window with both. Relative relationships such as “before Data + AI Summit” remain ordinary source-backed facts unless a later query requirement justifies a separate structured relation.

Migration 24 stores the object additively as nullable `event_time_kind`, `event_start_at`, `event_end_at`, `event_time_precision`, and `event_time_expression` columns. The API/extractor shape remains nested; storage is flattened only to keep SQLite filtering and revision copying simple. Brain does not rewrite an event entity around one mutable canonical date: actual and planned timings remain separately cited facts. Status, participants, location, prerequisites, and outcomes remain ordinary facts linked through `fact_entities`.

Operational Calendar items remain in `ops.sqlite` and do not become event entities or durable facts automatically. A trusted structured meeting source may deterministically project an event entity, but scheduled bounds alone prove only `planned`. The projection upgrades to `actual` only when the trusted underlying Hyprnote artifact was updated at or after the event start (or a future projector supplies stronger direct occurrence evidence); capture or ingestion time is never occurrence evidence. The source-update field and event bounds remain cited provenance.

Structured occurrence identity includes the normalized title and canonical start, so two same-title sessions on one date with disjoint starts resolve to different event entities and pages. Within one extraction batch, exact same-title, same-kind, same-interval structured projections coalesce before policy or critic actions and union every source ID, span, document, window, session, and evidence unit. `planned` and `actual` never coalesce merely because their bounds match.

### Fail-Open Temporal Validation

Extractor v12 proposes the complete base fact first. Temporal fields are optional enrichments validated independently:

- unsupported kinds, impossible bounds, false precision, ungrounded expressions, or a missing/ambiguous primary event cause only the malformed temporal enrichment to be discarded;
- the accepted base fact, evidence, entities, routing, and confidence survive unchanged;
- ambiguous relative phrases are retained in the ordinary statement rather than resolved from source or job time;
- model-supplied assertion time is ignored; `observed_at` is derived only from trustworthy source metadata;
- diagnostics record dropped temporal enrichment so quality can be measured without lowering fact recall.

This is the governing safety rule: temporal parsing fails open for fact retention and fails closed for temporal claims. Extractor v12 revisits older watermarks once to repair the identified contract failures; subsequent unchanged v12 terminal watermarks are preserved. A full historical rebuild remains an explicit isolated operation rather than an implicit prompt migration.

## Entity Identity

Entity identity and page routing are separate.

- `entities` stores canonical name, aliases, closed type, status, and merge lineage.
- `fact_entities` is the source of truth for fact-to-entity links.
- `facts.entity_id` is a denormalized cache of the primary link.
- entity types are `person|organization|product|project|concept|place|event|other`.
- mention kind defaults to under-creation: only admitted named mentions create entities unless local policy admits concepts.
- exact normalized name/alias matches require no LLM.
- ambiguous resolution may ask a configured resolver to choose only from a closed candidate list.

Reversible `entity_merge` updates all links and records an exact inverse. Type-incompatible merges are blocked. Large or uncertain merges remain policy-gated. `entity_split` restores prior entities and links from the inverse.

## Routing And Managed Pages

Routing hints are selected per source window by relevance. Valid automatic destinations are managed canonical pages:

- normalize an accidental `wiki/` prefix;
- exclude `references/*` and `agent_session_log/*`;
- reject `reference`/`index` page types and internal provider-prompt titles at review-route boundaries;
- fuzzy-snap near-duplicate canonical routes;
- allow a new canonical path only after a duplicate check;
- convert invalid/fallback destinations into unrouted residue.

An invalid or fallback candidate receives one bounded second-stage resolver pass before it becomes human residue. The resolver sees source identity/date, ranked active destinations, routed sibling examples, and same-source route shares. It should prefer a plausible coherent sibling route, may create one concise canonical page when the source clearly establishes a missing durable topic, and uses human review only for material route ambiguity or insufficient topic/identity context. Route acceptance uses the active future-job autonomy floor: 0.95 in Review First, 0.80 in Balanced, and 0.60 in More Autonomy. Invalid paths, reference/inbox destinations, unknown claimed existing pages, omitted decisions, and malformed output still fail closed.

Resolver model calls use compact batch-local indexes and resend the complete prompt after malformed JSON; schema-repair truncation may not remove routing cards. A named organization cannot be routed to another organization's company page, and a genuinely new organization subtopic collapses to the canonical company path unless an active topical/company namespace already exists. These deterministic checks constrain model judgment without forcing a same-source route when the fact clearly changes topic.

After all windows for one document are extracted, high-confidence sibling routes provide a bounded document-coherence prior. Only facts routed at confidence 0.75 or greater contribute. The prior may reroute an invalid, fallback, or explicitly uncertain candidate below 0.65 when either:

- at least two siblings and 60% of eligible sibling facts favor the page and the candidate has lexical support for it; or
- a strong document prior has at least three siblings and a 75% share.

The coherence bonus is capped at 6 ranking points. It is a preference, not an absolute rule: a valid high-confidence outlier remains on its own route, split-topic documents do not force a majority, and a fact may still route outside the document's dominant topic when its evidence says so. Reclaim and Inbox candidate ranking use the same active-fact prior, resolving source documents through fact metadata or chunk provenance. The UI identifies routes supported by sibling facts from the same source.

Future extractor payloads must report extraction, routing, and truth confidence explicitly. Missing confidence is a validation failure with a bounded retry rather than a silent `0.5` assignment. Source time also comes from source provenance: event start, source creation, capture, and document-ledger dates precede any fact-level fallback, so extraction or reconciliation time cannot masquerade as the source date.

The July 2 corrected shadow run routed 218 of 234 accepted facts to canonical pages, left 16 unrouted, and produced no reference/log destinations.

Managed pages are projections of active facts under page contracts. Human-authored content is preserved outside managed regions. Optional synthesis is derived prose, cites fact IDs, and can be deleted without losing canonical knowledge. Page snapshots support audit and guarded topology changes.

## Gardener

The gardener deterministically proposes page/entity topology candidates, then an optional LLM disposes each candidate in a bounded per-candidate call. Failures isolate to that candidate and force review.

Current safeguards:

- deterministic `candidate_key` identity;
- suppressed/open keys are not re-proposed;
- LLM drops and truncated kept candidates remain auditable;
- effort is selected per candidate: low for high-certainty exact/compact entity merges, medium for ordinary medium-risk work, and xhigh for fuzzy, cross-type, or large topology;
- local `merge_aggressiveness` independently adjusts fuzzy entity/page-merge admission, while exact normalized/compact-name identity signals remain eligible;
- local `split_aggressiveness` adjusts page-split fact, section, and per-section density floors; there is no automatic entity-split generator;
- local `topology_review_threshold` sets the affected-fact/page size at which otherwise-safe future candidates become large-topology L3 work; each candidate records the threshold used;
- all topology settings are read when a future gardener run starts and never rewrite already-proposed actions;
- commit remains deterministic and reversible through `cos_actions`.

The verified local gardener model is `gpt-5.6-luna`. Page/entity candidate volume is still bounded before proposal; expanding judgment coverage is future, eval-gated work.

## Current Gaps

- Historical duplicate topology rows are hidden on the active Queue but have no explicit auditable cleanup command.
- Legacy page consolidation remains a supervised migration concern, although the source regeneration reduced its practical value.
- Typed relation edges are intentionally absent; natural-language facts plus entity links remain the knowledge graph.
- Native Wiki rendering/provenance interaction is incomplete; see [App And Operations](app-and-operations.md).

## Future Calendar Evidence Adapter

Status: the manual read-only operational shadow adapter is implemented; Calendar capture/retrieval ingestion remains unimplemented.

The initial provider is Google Calendar with a separate narrowly scoped account grant. Event snapshots enter the evidence boundary with stable account, calendar, event, recurrence, and revision identity. The adapter requests no mutation scope and does not infer a task merely because an event exists.

Normalization must preserve timed versus all-day semantics, source timezone, organizer/attendee role, RSVP, recurrence master and exception identity, cancellation/deletion, visibility, and update revision while minimizing retained private payload. Replaying an unchanged revision is a no-op. A changed, moved, or cancelled event creates a new immutable operational observation and reconciles the same canonical item instead of creating a duplicate.

Calendar is operationally useful without LLM extraction. Its source evidence remains eligible for normal retrieval only under an approved capture/privacy policy; the current item lifecycle and briefing behavior belong to [Chief-of-Staff Operations](chief-of-staff-operations.md).

## Future Gmail And Email Adapter

Status: the read-only Gmail operational mirror, local analysis queue, and separate encrypted Gmail history archive are implemented; capture, retrieval indexing, and durable-knowledge ingestion remain paused.

The Gmail account card can hold the separately approved `gmail.readonly` grant for the operational mirror and Shadow evaluation. It stores secrets and tokens in macOS Keychain, but the capture registry remains `auth_only`: its capture subsystem cannot discover mail into `inbox/`, schedule knowledge capture, index mail for retrieval, or run durable-fact ingestion. A separate provider-sync job may update only the rebuildable operational mirror. Any future knowledge adapter must receive a separate product decision and revise this capture plan. Its safety constraints remain:

The encrypted archive is not that future knowledge adapter. It keeps exact Gmail `RAW` messages, attachments, and parsed text encrypted in a separate local store. One state machine performs a fixed 90-day backfill and then Gmail history incrementals; an expired cursor restarts the scan, and provider deletion retains local ciphertext. V1 `search_mail` and `get_mail_thread` return bounded evidence through the daemon only. Archive sync itself invokes no LLM, fact extraction, Knowledge Curation, operational detection, or Gmail mutation; an explicit agent tool call may place only its bounded returned content in that agent's context.

- no mail sender, label mutation, deletion, attachment retrieval, or full-corpus knowledge capture in the operational shadow phase;
- local Maildir/mbox remains an eligible evidence input alongside a future Gmail API adapter;
- one snapshot-replaced document per thread;
- deterministic bulk-versus-human classification;
- redaction before writing `inbox/` or `raw/`;
- attachment metadata only, never binary copies;
- retrieval indexing, durable-fact extraction, and operational detection are separately permissioned and budgeted lanes;
- durable-fact extraction is opt-in by human/evidence policy rather than mailbox-wide;
- ephemeral logistics stay out of facts but may become source-backed operational items;
- email mentions may link to known entities but may not create entities by default;
- strict per-run extraction and residue caps.

An isolated 2026-07-13 experiment used a separate existing Google Workspace credential, not the Brain connector grant, to normalize and index 90 days of Gmail in a private test Brain. It measured 6,650 threads, 21,458 chunks, and 319.5 MB of allocated Brain growth. Deterministic bulk/transactional filtering admitted 318 threads (4.8%) to fact eligibility. A production-policy three-day sample averaged 551,224 total tokens and 172,976 uncached input tokens per selected day; scaling observed per-document cost to the 90-day mean eligible volume projects about 448,792 total tokens/day, while an all-mail full-fact-pipeline counterfactual is about 9.43 million tokens/day. The measurement and limitations are recorded in [Gmail Indexing Benchmark](../audits/gmail-indexing-benchmark-2026-07-13.md).

These results narrow the future contract but do not authorize capture. The 9.43-million-token counterfactual applies the complete extractor/resolver/critic fact pipeline to every thread; it is not the cost of a single-stage operational scan. A production adapter must classify before durable extraction, default attachment payloads out, remove quoted-history duplication, use replaceable thread snapshots, and carry explicit daily document/token budgets.

The operational lane is orthogonal to the 4.8% fact gate. It may inspect changed bulk, transactional, and human mail with one low-cost structured pass per changed thread or failure-isolated batch. It must not inherit the fact critic or candidate resolver loop. Exact thread/message lineage drives reconciliation first; ambiguous cross-thread matches remain provisional. Marketing/noise can still be suppressed, so the operational set is not literally the complement of the fact set.

## Future Slack Adapter

An identity-only Slack OpenID shell requests `openid`, `profile`, and `email`. It stores credentials in macOS Keychain and cannot request workspace-message scopes, enumerate channels, discover messages, or write inbox artifacts. Message and thread normalization, channel/DM privacy boundaries, retention, edits/deletes, reactions, attachments, bot filtering, and volume controls require an approved preprocessing spec before capture is implemented.

## Acceptance

- Re-ingesting unchanged sources is idempotent and uses source statistics.
- Every accepted LLM fact has exact unit-derived source spans and a quote cache.
- A real unsupported quantity is rejected; identifiers such as `B2B` do not trigger the quantity gate.
- Empty/template windows make no provider call and terminate with `extracted_empty`.
- Multi-channel meeting words render in actual timestamp order; overlapping synthetic track clocks remain grouped and explicitly labeled as non-turn order.
- Every transcript evidence unit preserves its stable speaker label, including later sentences in the same turn.
- Reference/log pages can never be automatic fact destinations.
- Uncertain routing uses bounded same-document coherence without overriding explicit high-confidence outliers.
- Fact identity groups by entity, not page route.
- Entity merge/split round-trips all links and denormalized primary IDs.
- Managed pages rebuild from active facts without treating synthesis as evidence.

Primary verification:

```bash
uv run pytest tests/test_capture.py tests/test_cos.py tests/test_entities.py \
  tests/test_gardener.py tests/test_fact_relations.py -q
uv run brain provenance check --home <test-home>
uv run brain eval run --suite extraction --home <test-home>
uv run brain eval run --suite routing --home <test-home>
```
