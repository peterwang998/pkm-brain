# Chief-of-Staff Wiki Curation

## Goal

The wiki review loop should feel like a chief of staff maintaining a durable
knowledge base, not like a human approving hundreds of proposed Markdown
patches. The system should extract source-backed facts, dedupe obvious
duplicates, route facts to the right page, author managed wiki pages, and ask
the human only when the available evidence contains a real factual conflict or
an important missing answer.

## Product Shape

The human review unit is a factual question, not a wiki diff. Most proposals
should disappear into an internal fact ledger. The UI should surface:

- Open factual questions that block confident curation.
- The source-backed options behind each question.
- The current managed-page draft produced from active facts.
- Recent facts the system accepted, superseded, or marked conflicted.

The UI should not ask the reviewer whether to keep duplicates, which page to
put an obvious fact on, or whether old low-confidence duplicates should be
discarded. Those are curator responsibilities.

## Architecture

The old proposal backlog was a bootstrap input and has been drained. The current
curation flow is:

1. Extract candidate facts from recent source documents, memories, agent
   sessions, and other raw evidence.
2. Normalize and dedupe those facts into a fact ledger.
3. Route facts to entity keys and page hints.
4. Resolve stacks automatically:
   - Exact duplicates merge.
   - Near-duplicates and compatible paraphrases merge after source/provenance
     union.
   - Additive non-conflicting facts stay active.
   - Newer high-confidence source-backed replacements supersede older facts.
   - Low-confidence or source-ambiguous replacements become conflicts only
     after the equivalence pass proves they are materially incompatible.
5. Create or update managed wiki pages from active facts.
6. Ask the human only for unresolved factual conflicts.
7. When the human answers, mark facts confirmed or superseded and regenerate
   the affected managed page.

## Data Model

`facts` stores the durable review primitive:

- `id`: fact identifier.
- `statement`: concise factual statement.
- `entity_key`: semantic entity or topic bucket.
- `page_hint`: preferred wiki path.
- `section_hint`: target wiki section when known.
- `source_ids`: JSON list of evidence IDs.
- `observed_at`: source/proposal timestamp.
- `confidence`: curator confidence.
- `status`: `active`, `superseded`, `conflicted`, `needs_confirmation`, or
  `retracted`.
- `supersedes_id`: older fact displaced by this fact, when applicable.
- `conflict_group_id`: shared group for mutually conflicting facts.
- `confirmed_by_user`: true after explicit human answer.
- `metadata`: source operation, batch IDs, rationale, target path, and other
  provenance.
- `created_at`, `last_seen_at`: ledger timestamps.

`open_questions` stores only human-worthy blockers:

- `kind`: `conflict` or `insufficient_evidence`.
- `entity_key`, `page_hint`: where the question applies.
- `fact_ids`: facts involved in the question.
- `question`: direct factual question.
- `options`: source-backed answer options.
- `status`: `open`, `answered`, or `dismissed`.
- `answer`: selected fact ID or free-form human answer.
- `context`: machine-readable provenance.

`wiki_pages` gets two curation columns:

- `managed`: true for pages the chief-of-staff writer owns.
- `fact_ids`: JSON list of facts used in the current page.

`wiki_curation_runs` records fact curation and page-authoring runs.

## LLM Responsibilities

The LLM should be aggressive about curation and conservative only about truth:

- Extract atomic facts from source evidence.
- Group facts by semantic topic/entity across time.
- Merge duplicates without asking.
- Merge paraphrases and largely similar facts without asking, preserving all
  source IDs and the most informative wording.
- Route facts to canonical pages without asking.
- Prefer newer high-confidence source-backed facts over older stale facts.
- Preserve additive facts unless contradicted.
- Ask the human only when two plausible facts cannot both be true, or when a
  missing user-owned fact is required to author the page correctly.
- Produce concise managed-page Markdown from active facts.

The prompt should explicitly prohibit questions like "Should we keep both
duplicates?" or "Which page should this go on?" The model should make those
decisions and include its rationale in metadata.

## MVP Implementation

The forked implementation builds the first working slice:

- Add migration `006_create_wiki_fact_curation`.
- Add a `wiki_facts` module for ledger writes, conflict
  resolution, question answering, and managed-page drafting.
- Add a `wiki_fact_migration` module for one-time backfill from existing
  semantic wiki pages into the fact ledger. This is transitional bootstrap
  code; future ingestion should create facts directly from source evidence
  instead of reparsing rendered wiki prose.
- Add UI endpoints:
  - `GET /api/wiki/facts`
  - `POST /api/wiki/facts/migrate-wiki`
  - `POST /api/wiki/facts/reconcile`
  - `POST /api/wiki/questions/<id>/answer`
- Add a "Chief of Staff" browser view showing open questions, options, active
  facts, conflicted facts, and recent curation runs.
- Retire packet-level absorption and proposal approval UI after the legacy
  backlog is drained; archived `wiki_change_*` rows remain readable in SQLite
  but are no longer an application workflow.

The legacy wiki migration imports existing non-reference wiki sections as
lower-confidence additive facts with `migration: wiki_fact_backfill_v1`
metadata. Reruns are idempotent for exact same statements. The resolver compares
these imported facts against newer replacement facts and asks the human only
when there is a direct material contradiction, while avoiding fake conflicts
between additive bullets on the same page.

## Later Phases

1. Replace deterministic extraction with an LLM fact extractor that receives
   source chunks, current wiki pages, and proposal candidates.
2. Add embedding-assisted entity resolution so related facts across different
   target paths collapse into one semantic entity.
3. Add page-level undo by storing before/after snapshots for every managed
   write.
4. Add stale-question decay and "ask me later" scheduling.
5. Make managed wiki pages the default output of nightly synthesis and reduce
   raw proposal generation to an escape hatch.
