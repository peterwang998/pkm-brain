# Personal Knowledge Management / Agent Memory System Spec

Spec version: 0.1

Status: Draft for human review

Last updated: 2026-05-25

Current status note (2026-06-25): this v0.1 spec is historical/foundational. The Chief-of-Staff facts/actions/pages architecture in `docs/chief-of-staff-spec.md` supersedes legacy wiki proposal workflow sections. `wiki_change_*` tables remain archived compatibility/audit data; active UI, CLI, MCP, and nightly paths no longer create, apply, absorb, or search wiki proposal batches.

## 1. Purpose

Build a local-first personal knowledge management system that serves two roles:

1. A second brain for the user: searchable, inspectable, and durable.
2. A context memory layer for coding agents and LLM workflows.

The system should combine Karpathy-style durable wiki synthesis with scalable local retrieval infrastructure and explicit typed memory records for agents.

Core principle:

```text
Raw sources are evidence.
Wiki pages are synthesized knowledge.
Memories are curated, typed claims derived from evidence.
Indexes are rebuildable derived artifacts.
```

## 2. Design Goals

The system must be:

- Local-first and runnable on a MacBook Pro.
- Cloud-LLM compatible, but not cloud-dependent for storage.
- Transparent and inspectable through files and metadata.
- Batch-indexed by default, with real-time ingestion optional later.
- Designed for agent access through MCP and/or HTTP.
- Able to ingest unstructured material such as notes, transcripts, working documents, and coding-agent logs.
- Able to distinguish raw documents from durable memory.
- Rebuildable from source data if chunking, embeddings, or metadata strategies change.

## 3. Non-Goals For V1

Do not build these in the first version:

- Fancy custom UI.
- Graph database.
- Fine-tuned embedding models.
- Fully autonomous unreviewed memory writes.
- Real-time indexing.
- Complex ontology.
- Multi-user permissions.
- Production cloud deployment.

## 4. System Architecture

```text
Filesystem Inbox
  ↓
Raw Source Store
  ↓
Ingestion Pipeline
  ↓
Canonical Metadata Store
  ↓
Chunking + Enrichment
  ↓
Hybrid Retrieval Index
  ↓
Wiki Synthesis Layer
  ↓
Typed Memory Layer
  ↓
Agent Interface: MCP / HTTP
```

Recommended V1 stack:

```text
Language: Python
Raw storage: local filesystem
Metadata DB: SQLite
Retrieval index: LanceDB
Lexical search: LanceDB BM25 if available, otherwise SQLite FTS5
Embeddings: local bge/nomic model or cloud embedding provider
Reranker: local bge-reranker-base or cloud reranker
Agent interface: MCP server first, HTTP API second
Orchestration: cron or Makefile initially
Human frontend: Obsidian / VSCode / filesystem
```

### 4.1 Installation Wizard

The guided installation wizard is implemented. `brain init` runs the direct workspace initializer; `brain setup` and `brain init --wizard` run the interactive flow.

Required command:

```text
brain setup
```

Acceptable alias:

```text
brain init --wizard
```

The wizard should prompt for:

```text
Brain home path
workspace initialization or validation
agent capture sources to enable
MCP setup guidance for supported agents
scheduled capture job installation
nightly maintenance job installation
optional LLM proposal provider configuration
optional Primary / Secondary Brain sync setup
```

If the user chooses Primary / Secondary sync setup, the wizard must hand off to the sync configuration flow described in `docs/primary-secondary-brain-sync-spec.md`.

The wizard must support `--dry-run`, `--json`, and non-interactive flags for scripted installs, but the default human path should be guided prompts.

### 4.2 Local Web UI

An optional local Web UI is implemented as a human control plane for Brain status, setup, scheduled jobs, sync validation, and memory review. V1 uses stdlib `http.server`, not FastAPI. `brain ui service install/status/uninstall` for running the UI as a managed service is deferred.

Default behavior:

```text
brain ui --host 127.0.0.1 --port 8765
```

The UI must bind to loopback by default, start on demand, avoid logging raw document contents, and require a local token or equivalent local authentication gate. LAN-visible mode must be explicit and authenticated.

Required areas:

```text
status dashboard
setup wizard
capture and ingestion jobs
Primary / Secondary sync status
memory review and validation
recent logs and errors
```

The Web UI must remain a thin layer over the same service operations as the CLI. It must not create a separate data model, memory store, sync engine, or approval path. Memory approval remains local and human-operated.

For the detailed Primary / Secondary access model, SSH tunnel flow, scheduler abstraction, and Web UI sync requirements, see `docs/primary-secondary-brain-sync-spec.md`.

## 5. Filesystem Layout

Use a project directory such as:

```text
~/brain/
  inbox/
  raw/
  wiki/
  memory/
  indexes/
  db/
  logs/
  config/
```

Directory responsibilities:

```text
inbox/
  Temporary landing zone for new files.

raw/
  Immutable normalized source captures.

wiki/
  LLM-maintained markdown synthesis layer.

memory/
  Curated typed memory files, preferably also mirrored in SQLite.

indexes/
  LanceDB and other derived indexes.

db/
  SQLite metadata database.

logs/
  ingestion logs, retrieval logs, agent-session logs.

config/
  prompts, schemas, source adapters, model settings.
```

## 6. Raw Source Model

Every ingested item becomes a raw source record.

Required fields:

```text
id
source_type
title
source_path
created_at
ingested_at
content_hash
origin_uri
project
tags
version
status
```

Supported initial source types:

```text
markdown_note
meeting_transcript
agent_session_log
working_document
code_snippet
web_clip
manual_entry
```

Raw files must not be mutated after ingestion. If content changes, create a new version with a new hash unless a source type has an explicit retention policy.

Implemented exception: captured `agent_session_log` sources retain only the latest/final snapshot per agent session. A changed snapshot for the same captured session path replaces the prior document, chunks, FTS rows, vector rows, and raw copy. This saves storage and avoids duplicate indexing of long-running session logs produced by scheduled polling.

## 7. SQLite Schema

Initial tables:

```text
documents
chunks
entities
relations
memories
wiki_pages
ingestion_runs
automation_runs
retrieval_events
context_lineage_events
agent_sessions
forget_events
```

Minimum document schema:

```sql
documents(
  id TEXT PRIMARY KEY,
  source_type TEXT,
  title TEXT,
  source_path TEXT,
  raw_path TEXT,
  content_hash TEXT,
  origin_node_id TEXT,
  logical_source_key TEXT,
  created_at TEXT,
  ingested_at TEXT,
  project TEXT,
  tags TEXT,
  version INTEGER,
  status TEXT
)
```

`raw_path` is the immutable normalized copy under `~/brain/raw/<source_type>/`. `origin_node_id` and `logical_source_key` support multi-machine sync: rows captured on a Secondary preserve the Secondary's node id and the canonical local path, so re-ingestion deduplicates by (origin, logical key) rather than by per-machine path.

Minimum chunk schema:

```sql
chunks(
  id TEXT PRIMARY KEY,
  document_id TEXT,
  chunk_index INTEGER,
  text TEXT,
  heading_path TEXT,
  start_offset INTEGER,
  end_offset INTEGER,
  token_count INTEGER,
  content_hash TEXT,
  created_at TEXT
)
```

Minimum memory schema:

```sql
memories(
  id TEXT PRIMARY KEY,
  memory_type TEXT,
  scope TEXT,
  content TEXT,
  confidence REAL,
  source_ids TEXT,
  status TEXT,
  created_at TEXT,
  updated_at TEXT,
  last_seen_at TEXT,
  reviewed_at TEXT,
  review_reason TEXT
)
```

Memory statuses:

```text
proposed
active
superseded
rejected
archived
```

Minimum automation run schema:

```sql
automation_runs(
  id TEXT PRIMARY KEY,
  job_name TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  summary TEXT,
  error TEXT
)
```

Automation run records should track scheduled jobs separately from ingestion runs. A nightly maintenance run may include capture, ingestion, wiki synthesis, audits, and status checks in one job summary.

Minimum lineage event schema:

```sql
context_lineage_events(
  id TEXT PRIMARY KEY,
  target_type TEXT,
  target_id TEXT,
  event_type TEXT,
  retrieval_event_id TEXT,
  agent_session_id TEXT,
  query TEXT,
  weight REAL,
  metadata TEXT,
  created_at TEXT
)
```

`target_type` is one of `memory`, `chunk`, `document`, or `wiki_page`. `event_type` is one of `exposed`, `explicit_useful`, `explicit_not_useful`, `agent_referenced_id`, or `memory_proposed_from_lineage`. Lineage data is advisory, auditable, and rebuildable; approved memories remain the durable trust boundary.

Archived legacy wiki proposal schema:

```sql
wiki_change_batches(
  id TEXT PRIMARY KEY,
  title TEXT,
  rationale TEXT,
  author TEXT,
  source TEXT,
  status TEXT,
  confidence REAL,
  source_ids TEXT,
  created_at TEXT,
  reviewed_at TEXT,
  applied_at TEXT,
  error TEXT
)

wiki_change_items(
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  order_index INTEGER,
  target_path TEXT,
  operation TEXT,
  section_name TEXT,
  proposed_markdown TEXT,
  rationale TEXT,
  source_ids TEXT,
  confidence REAL
)

wiki_interviews(
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  questions TEXT,
  answers TEXT,
  disposition TEXT,
  provider TEXT,
  model TEXT,
  created_at TEXT
)
```

Wiki proposal statuses:

```text
proposed
needs_interview
approved
rejected
applied
superseded
failed
```

Minimum forget event schema (pending; not yet created by migration):

```sql
forget_events(
  id TEXT PRIMARY KEY,
  target_kind TEXT,
  target_id TEXT,
  content_hash TEXT,
  reason TEXT,
  requested_by TEXT,
  requested_at TEXT,
  applied_at TEXT,
  affected_counts TEXT,
  cloud_egress_followup TEXT
)
```

Allowed forget target kinds:

```text
source
session
memory
pattern
range
```

`content_hash` is populated for source and session targets and is used as the re-ingestion tombstone key. `affected_counts` is a JSON object summarizing how many chunks, vectors, memories, wiki pages, and proposal batches were updated by the forget operation. `cloud_egress_followup` records the count of prior retrieval events and automation runs that previously sent the target to a cloud provider, so the user can follow up with provider-side deletion.

## 8. Chunking Strategy

Use source-specific chunking.

Markdown / prose:

```text
- Respect heading hierarchy.
- Target 300-800 tokens.
- Include heading path in chunk metadata.
- Use modest overlap, around 50 tokens.
```

Meeting transcripts:

```text
- Preserve timestamps.
- Preserve speakers.
- Chunk by speaker turns or topic segments.
- Generate meeting-level summary.
```

Hyprnote meeting captures:

```text
- Source type: hyprnote_meeting.
- Opt-in only; default "all" capture must not scan Hyprnote.
- Capture _summary.md, _memo.md, and transcript.json from each session directory.
- Preserve calendar title, session id, source path, and meeting time range from _meta.json.
- Do not copy audio files or local model artifacts in V1.
- Hash text artifacts so unchanged sessions are skipped by scheduled polling.
```

Agent session logs:

```text
- Retain only the latest/final captured snapshot per agent session.
- Preserve the raw captured Markdown under raw_path for auditability.
- Build derived chunks, FTS rows, vectors, and lineage from sanitized text.
- Preserve user request.
- Preserve commands run.
- Preserve files touched.
- Preserve errors.
- Preserve final outcome.
- Extract decisions and unresolved issues.
- Strip system prompts, permission blocks, retrieved-context dumps, citation snapshots,
  MCP/CLI JSON blobs, and large tool outputs from the indexed representation.
```

Code snippets:

```text
- Prefer function/class boundaries.
- Use tree-sitter later if needed.
- Preserve file path and symbol names.
```

Each chunk should store enough provenance to reconstruct where it came from.

## 9. Enrichment Pipeline

The nightly indexing job should run these stages:

```text
1. Discover new or changed files.
2. Hash content.
3. Normalize into raw source format.
4. Create document records.
5. Chunk documents.
6. Generate summaries and metadata.
7. Generate embeddings.
8. Build or update BM25/lexical index.
9. Build or update vector index.
10. Propose wiki updates.
11. Propose memory updates.
12. Log ingestion run.
```

### 9.1 Nightly Maintenance Automation

The nightly maintenance job is a self-healing wrapper around the local pipeline. It should be broader than the frequent agent-log polling job.

Frequent job:

```text
com.pkm-brain.agent-log-ingest
StartInterval = 600
capture agents
ingest inbox
```

Nightly job:

```text
com.pkm-brain.nightly-maintenance
StartInterval = 3600
brain automation nightly --if-due --due-after-hours 20
```

The nightly job should use an hourly due-check instead of only `StartCalendarInterval`. This makes it laptop-friendly: if the machine sleeps through the intended overnight window, the next hourly check after wake can run maintenance when the last successful run is older than the threshold.

Current nightly maintenance runs these tasks:

```text
1. capture agents
2. ingest inbox
3. run Chief-of-Staff extraction/gardener shadow stages
4. collect index status and run conservative index maintenance
5. run Chief-of-Staff sampled audit (stub unless an auditor provider is configured)
6. run provenance check
7. run wiki lint
8. run memory audit
9. record automation run summary
```

Nightly does not currently run a wiki synthesis command. The `--llm-wiki/--no-llm-wiki` automation option is retained as a compatibility flag for existing LaunchAgents. Current wiki maintenance is fact-ledger driven through `brain wiki migrate-to-facts`, `brain wiki curate-facts`, and `brain wiki promote-curation`.

Automation run persistence must keep normal status summaries useful but cap fields named `error`, `errors`, `stderr`, or `traceback` before writing `automation_runs`. Failed provider calls must not persist full LLM prompts, full stderr streams, or retrieved context dumps inline; compacted error text should include enough prefix/suffix detail plus a digest to correlate with logs.

Optional nightly LLM memory proposal stages remain separate from direct generated-page maintenance:

```text
brain automation nightly --with-llm-memory-proposals --provider <provider>
```

`--with-llm-memory-proposals` analyzes recent agent logs, structured `agent_sessions`, unresolved issues, failed or suspicious command history, retrieval events, context lineage events, and existing memories. It may create only `proposed` memories and must deduplicate against existing proposed or active memories. Failure-source synthesis should still produce `AgentFailurePatternMemory` records; lineage synthesis may propose other durable memory types only after conservative independent-evidence thresholds pass. It uses the existing `com.pkm-brain.nightly-maintenance` LaunchAgent; no separate memory proposal LaunchAgent should be created.

### 9.2 Future Work: Tension Audit

A future version should add an optional tension audit that detects possible contradictions, stale claims, unresolved assumptions, and competing interpretations across recent sources, generated wiki pages, and active memories.

The tension audit should behave like the existing proposal systems: nightly automation may create durable `proposed` tension items, but it must not directly mutate approved wiki Markdown or active memories. Human review may happen asynchronously after multiple nightly runs, so tension items must be stored with stable ids, source evidence, creation time, originating automation run id, confidence, status, and deduplication keys.

Tension items should cite both sides of the possible issue. Examples include a recent source that conflicts with an active memory, a wiki page that appears stale against newer evidence, or two sources that preserve a meaningful disagreement that should not be smoothed into a single synthesized narrative.

Potential tension statuses:

```text
proposed
accepted
dismissed
resolved
superseded
archived
```

Potential human review actions:

```text
dismiss as false positive
accept as real tension
convert to open loop
propose wiki update
propose memory update
mark older memory or wiki claim superseded
link to an existing decision
```

The audit should deduplicate against unresolved prior tension items so repeated nightly runs do not create duplicate review work. Agents must not treat proposed tension items as authoritative context; they are review candidates until a human resolves them.

Nightly maintenance status rules:

```text
If --if-due is set and the last successful run is younger than due_after_hours, exit successfully without running work.
If another nightly run is active, exit successfully as skipped.
If any check reports errors, record the automation run as failed.
Warnings should be recorded but should not fail the job by default.
```

LLM enrichment may extract:

```text
summary
entities
projects
decisions
preferences
open loops
topics
hypothetical questions
```

All enrichment must be cacheable by content hash.

## 10. Retrieval Design

Query pipeline:

```text
User or agent query
  ↓
Optional query expansion
  ↓
BM25 search
  ↓
Dense vector search
  ↓
Optional hypothetical-question vector search
  ↓
Reciprocal Rank Fusion
  ↓
Rerank top candidates
  ↓
Apply source-aware and capped lineage tie-breakers
  ↓
Apply retrieval policy and source-specific caps
  ↓
Excerpt or compress oversized chunks
  ↓
Expand neighboring chunks
  ↓
Deduplicate
  ↓
Assemble context packet
```

Use RRF instead of hand-weighted score blending for V1 because BM25 and vector scores are not directly comparable.

`search_knowledge` and `retrieve_context` should share the same reranking model: broad BM25/vector fan-out, reciprocal-rank candidate collection, source-aware reranking, and top-N selection with recall backfill from lower-ranked candidates when filtering leaves too few results. Meetings, notes, transcripts, web clips, and working documents receive a positive source signal for normal knowledge queries. Agent session logs are downranked for normal knowledge queries but may rank highly for agent/session/tool/implementation-history queries.

Context packet format (as returned by `service.retrieve_context`):

```text
task
project
budget
retrieval_mode
supporting_chunks
citation_snapshots
citations                  # alias of citation_snapshots for back-compat
active_memories
candidate_memories
relevant_wiki_pages
open_questions
omitted_due_to_budget
retrieval_event_id
```

Debug mode additionally returns `retrieval_policy` and `retrieval_debug`. The previously named fields `selected_chunks`, `source_citations`, `related_wiki_pages`, and `confidence_notes` have been renamed or replaced by the fields above; consumers should treat the field names listed here as canonical.

`active_memories` are reviewed and trusted. `candidate_memories` are proposed, unreviewed hypotheses surfaced separately for awareness; agents must not treat them as authoritative operational instructions.

Retrieval modes:

```text
compact: small packet for routine agent context checks
default: bounded general-purpose packet
broad: explicit opt-in for larger surveys
inspect: explicit source-inspection mode with larger caps
```

The default packet must be bounded for usefulness, not model maximum context size. Reranking decides which chunks deserve attention; retrieval policy decides how much text each source type may consume. No selected chunk, including the first chunk, may exceed the remaining context budget.

The default context budget is 8,000 tokens. `retrieval_policy` and detailed reranking diagnostics should be returned only when debug output is explicitly requested, because MCP responses should stay compact for normal agent use.

Debug output should include `retrieval_score`, `selection_reasons`, `suppressed`, `suppress_reasons`, `retrieval_noise_reasons`, and raw-context pointers. Lineage boosts must be capped at `+2.0` as a tie-breaker, decay with a 90-day half-life, and never treat exposure-only events as positive feedback. Explicit useful feedback is strongest, explicit not-useful feedback is negative, and repeated stable-ID re-references from independent agent sessions are weak.

Chunk records should include:

```text
text
original_token_count
returned_token_count
omitted_tokens
excerpted
source_token_cap
raw_context
selection_reasons
```

Noisy source types such as `agent_session_log` must use lower default caps and should strip or downsample session metadata, system prompts, tool traces, repeated raw events, and giant command outputs. Curated wiki pages and typed memories may be returned more densely because they are already synthesized.

## 11. Wiki Synthesis Layer

The wiki is the human-readable, agent-maintained synthesis layer inspired by Karpathy's second brain pattern.

The wiki is not the same thing as raw retrieval, source indexing, or one-page-per-document summarization. It is the compiled knowledge layer. Raw documents are evidence. Reference pages are provenance aids. The wiki's primary user-facing pages are durable topic pages that accumulate understanding across many sources.

The wiki should contain:

```text
wiki/index.md
wiki/projects/
wiki/people/
wiki/concepts/
wiki/decisions/
wiki/open_loops/
wiki/timelines/
wiki/references/
wiki/log.md
```

Agents may update wiki pages only from cited sources.

### 11.1 Wiki Layers

The wiki has two generated layers:

```text
Compiled synthesis pages
  Human-readable pages for projects, concepts, decisions, people, open loops, and timelines.
  These pages merge evidence from many raw documents and should be pleasant to read directly.

Reference pages
  One-source provenance pages used for inspection and citation.
  These are allowed to be mechanical and are not the main reading interface.
```

Generated reference pages must live under:

```text
wiki/references/<source_type>/
```

Generated compiled pages must live under their semantic folder:

```text
wiki/projects/
wiki/concepts/
wiki/decisions/
wiki/open_loops/
wiki/people/
wiki/timelines/
```

The command `brain wiki synthesize` must do both:

```text
1. Maintain source-backed reference pages for provenance.
2. Compile or update semantic wiki pages from the current corpus.
3. Maintain wiki/index.md as the human and agent entrypoint.
4. Maintain wiki/log.md as the chronological synthesis log.
5. Insert links between related semantic pages using Obsidian-style wikilinks.
6. Cite source document ids for every generated page.
```

The command must not silently overwrite hand-edited human pages. A generated page is safe to update only if it contains the generated marker. If a target page exists without the marker, the system must skip it and report the skip.

LLM semantic compilation must use an LLM-guided source selection pass instead of simply passing the latest N documents. The default `llm_source_limit` is 12 selected sources. The selector receives bounded candidate cards and a soft preference for user-supplied/manual sources, meetings, notes, transcripts, web clips, and working documents. Agent logs remain eligible when they are directly relevant, such as implementation history, workflow preferences, or failure patterns. Dry-run diagnostics must report candidate counts, selected source IDs, selected/dropped counts by type, selector rationale, and selector warnings.

Source previews should be cleaned by type. Meetings, articles, and notes may include richer summaries and relevant chunks. Agent-log previews must strip system prompts, permission blocks, tool traces, long command output, raw event noise, and should be aggressively capped.

### 11.2 Compiled Page Requirements

Compiled pages should read like a small personal Wikipedia article, not like a copied source excerpt.

Each compiled page should:

```text
Summarize the stable current understanding.
Group related evidence across multiple sources.
Use short bullets for key points.
Link to related concepts, projects, decisions, and open loops.
Keep raw excerpts short and only in Source Evidence or provenance sections.
Represent uncertainty explicitly in Open Questions.
Prefer durable concepts over session-specific details.
```

Compiled pages should not:

```text
Dump raw agent logs.
Create one semantic page per source by default.
Treat a source summary as a concept page.
Invent facts not present in cited sources.
Erase source provenance.
```

Reference pages may be noisy. Compiled pages must be human-readable.

Each wiki page must follow a predictable Markdown schema so humans and agents can inspect, update, lint, and retrieve wiki content consistently.

Wiki pages should use YAML frontmatter followed by typed Markdown sections.

Required frontmatter for every wiki page:

```text
title
page_type
id
status
created_at
updated_at
source_ids
related
tags
```

Allowed page types:

```text
index
project
concept
decision
person
open_loop
timeline
reference
```

Allowed page statuses:

```text
draft
active
stale
superseded
archived
```

Required frontmatter format:

```yaml
---
title: Human-readable page title
page_type: concept
id: concept-example
status: active
created_at: 2026-05-04
updated_at: 2026-05-04
source_ids:
  - document_id_or_chunk_id
related:
  - concepts/related-page
tags:
  - example
---
```

Required sections for every wiki page:

```text
# Title

## Summary

## Key Points

## Source Evidence

## Related Pages

## Open Questions
```

Page-type-specific required sections:

```text
ProjectPage
  ## Current State
  ## Goals
  ## Decisions
  ## Open Loops
  ## Timeline

ConceptPage
  ## Definition
  ## Why It Matters
  ## How It Works
  ## Related Decisions

DecisionPage
  ## Context
  ## Decision
  ## Rationale
  ## Alternatives Considered
  ## Consequences

PersonPage
  ## Role
  ## Relevant Projects
  ## Interaction History

OpenLoopPage
  ## Question
  ## Current Understanding
  ## Needed Evidence
  ## Next Review

TimelinePage
  ## Events
  ## Current Status
  ## Source Evidence

ReferencePage
  ## Notes
  ## Extracted Facts
  ## Source Evidence
```

### 11.3 Links And Indexing

Wiki body links should use Obsidian-compatible wikilinks:

```text
[[concepts/wiki-synthesis-layer]]
[[projects/pkm-brain]]
[[decisions/use-sqlite-for-canonical-metadata]]
```

Frontmatter `related` values should use the same path without brackets:

```yaml
related:
  - concepts/wiki-synthesis-layer
  - projects/pkm-brain
```

`wiki/index.md` is the required entrypoint. It must list generated project, concept, decision, open-loop, person, and timeline pages with one-line summaries and links.

The index is content-oriented, not chronological. Chronological activity belongs in logs.

### 11.4 Synthesis Workflow

Current status note: this section is historical target architecture. The active implementation does not expose `brain wiki synthesize`; current wiki maintenance uses the Chief-of-Staff fact/page curation commands.

The wiki compiler should process sources in this order:

```text
1. Read latest ingested documents and chunks.
2. Generate or update reference pages for each source.
3. Extract candidate concepts, projects, decisions, people, and open loops.
4. Merge candidates with existing generated semantic pages.
5. Update semantic pages with source-backed summaries, links, and open questions.
6. Update wiki/index.md.
7. Append a chronological summary to wiki/log.md.
8. Run wiki lint.
```

The V1 compiler should use the default LLM provider for semantic compilation, following [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): raw sources are immutable evidence, generated Markdown is the maintained knowledge layer, `index.md` is content-oriented, and `log.md` is chronological. The deterministic local path is a fallback for reference pages, index maintenance, logging, and explicit `--no-llm` runs; it should not be the normal semantic compiler.

Generated semantic page maintenance may directly create or update pages only when all of these are true:

```text
1. The page is source-cited.
2. The compiler confidence meets the configured auto-apply threshold.
3. The target page is new or already contains the generated marker.
4. The update can preserve the required frontmatter and page sections.
```

If the target page exists without the generated marker, the compiler must skip direct overwrite and leave the page for human/chief-of-staff review. Lower-confidence or riskier changes should not silently mutate human-owned pages.

Legacy note: older versions used LLM-written wiki proposal batches with interview/apply commands. That path has been superseded by the Chief-of-Staff facts/actions/pages model and is no longer an active UI, CLI, MCP, or nightly workflow. The old `wiki_change_*` tables remain only as archived compatibility/audit data.

Current memory proposal entrypoints:

```text
brain memory propose-from-sources --provider <codex|openai|anthropic|ollama>
brain memory propose-from-lineage --provider <codex|openai|anthropic|ollama>
brain automation nightly --with-llm-memory-proposals --provider <provider>
```

Memory review entrypoints:

```text
brain memory list --status proposed
brain memory inspect <memory_id>
brain memory approve <memory_id>
brain memory reject <memory_id>
```

The system must support Codex CLI, OpenAI-compatible, Anthropic, and Ollama provider adapters. The Codex adapter should use `codex exec` in read-only, non-interactive mode so pkm-brain can use the user's local Codex login instead of a separate API key. Provider configuration must be inspectable with `brain llm doctor` without printing secrets. If nightly LLM memory proposals are explicitly enabled and provider configuration is missing, the nightly run must fail.

### 11.5 Retrieval Role

Retrieval should use both the compiled wiki and raw chunks.

At query time:

```text
Search compiled wiki pages first for stable synthesized context.
Search raw chunks for supporting evidence and recent details.
Return both in context packets when relevant.
Do not force callers to choose between wiki and hybrid search.
```

The wiki answers "what do we currently understand?" Raw retrieval answers "where did that come from, and what exact source text supports it?"

Example decision page:

```markdown
---
title: Use SQLite For Canonical Metadata
page_type: decision
id: decision-use-sqlite-metadata
status: active
created_at: 2026-05-04
updated_at: 2026-05-04
source_ids:
  - document:personal-knowledge-management-spec-v0.1
related:
  - concepts/local-first-storage
  - projects/pkm-system
tags:
  - sqlite
  - architecture
---

# Use SQLite For Canonical Metadata

## Summary

SQLite is the canonical metadata store for the local-first PKM system.

## Key Points

- SQLite stores document metadata, chunk metadata, memory lifecycle data, and retrieval logs.
- Raw artifacts remain on the filesystem by default.

## Context

The system needs a local, inspectable, low-operational-burden metadata store.

## Decision

Use SQLite as the V1 canonical metadata database.

## Rationale

SQLite is portable, simple to back up, and sufficient for a single-user local-first workload.

## Alternatives Considered

- Postgres with pgvector.
- SQLite-only with vector extensions.
- SQLite plus LanceDB.

## Consequences

This keeps V1 simple but may require a dedicated vector index later if retrieval scale grows.

## Source Evidence

- document:personal-knowledge-management-spec-v0.1

## Related Pages

- concepts/local-first-storage
- projects/pkm-system

## Open Questions

- Should V1 use SQLite-only retrieval or SQLite plus LanceDB?
```

Wiki lint rules:

```text
Every wiki page must have valid YAML frontmatter.
Every wiki page must have a unique id.
Every wiki page must use an allowed page_type.
Every wiki page must use an allowed status.
Every wiki page must include required common sections.
Every page-type-specific required section must be present.
Every decision page must include Source Evidence.
Every source_id must resolve to a known document, chunk, memory, or external source record.
Every related page link should resolve to an existing wiki page or be reported as missing.
Pages with no source_ids should remain draft unless explicitly marked as reference or index pages.
```

Wiki rule:

```text
The wiki summarizes and connects knowledge.
It does not replace raw sources or typed memories.
```

## 12. Typed Memory Layer

Typed memory is for agent personalization and operational context.

Initial memory types:

```text
PreferenceMemory
ProjectMemory
DecisionMemory
BehaviorMemory
RepoInstructionMemory
OpenLoopMemory
FactMemory
AgentFailurePatternMemory
```

Each memory must include:

```text
content
scope
confidence
source evidence
status
created_at
updated_at
```

Scopes:

```text
global
project
repo
agent
topic
```

Reviewed memory lifecycle:

```text
proposed
active
rejected
archived
superseded
```

Agents and nightly jobs may create `proposed` memories. Only local human review through the CLI or local Web UI may activate, reject, or archive them. MCP must expose memory proposal but must not expose approval.

`AgentFailurePatternMemory` is for operational lessons from agent failures: repeated bad assumptions, missed verification, tool misuse, or patterns that should change future agent behavior. It is authoritative only after status becomes `active`.

Example:

```text
type: PreferenceMemory
scope: global
content: User prefers direct, implementation-focused engineering responses.
confidence: 0.85
sources: [agent_session_2026_05_04]
status: proposed
```

## 13. Provider Privacy Boundaries

The system is local-first, but some optional maintenance paths may call external model providers. V1 should keep provider use explicit and inspectable without adding per-document or per-memory classification tiers.

Provider classes:

```text
local   ollama, local hash embedding, local SentenceTransformer
cloud   anthropic, openai-compatible, codex, cloud embedding providers
```

The Codex provider is treated as cloud because requests flow through a remote model even though the CLI is local.

Required rules:

```text
1. `brain llm doctor` must identify the configured provider and must not print tokens,
   API keys, private keys, or raw prompts.
2. Optional cloud-backed wiki and memory proposal commands must be explicit operator
   actions or explicitly enabled automation flags.
3. Automation run summaries and errors must cap large prompts, stderr streams, and
   provider payloads before writing to SQLite.
4. Capture and setup paths must redact secret-shaped values before writing agent logs,
   setup output, or run summaries.
```

Privacy remains a source-capture and provider-boundary concern in V1. The schema does not model tiered content controls.

## 14. Forgetting And Redaction

The system captures personal material automatically and must provide a defined path to remove it. Rebuilding from raw is necessary but not sufficient when the goal is to delete the raw itself.

Forget target kinds:

```text
source       A single ingested document, by id, content_hash, or source_path.
session      A captured agent session, by agent and session id.
memory       A specific memory id.
pattern      All documents whose text matches a regex or substring filter.
range        All documents ingested between two timestamps.
```

Propagation rules. When a target is forgotten, the system must, in order:

```text
1. Remove the raw artifact under ~/brain/raw/<source_type>/... .
2. Remove any remaining copy in ~/brain/inbox.
3. Delete the documents row, cascading to chunks via the existing ON DELETE CASCADE.
4. Delete corresponding rows from chunk_fts.
5. Delete corresponding vectors from the LanceDB chunks table.
6. Update memories that cite only the forgotten source: set status='archived' with
   review_reason='source forgotten <document_id>'. Memories that cite the forgotten
   source plus other surviving sources must have the forgotten source_id removed and
   must be flagged in the next memory audit.
7. Update wiki pages that cite the forgotten source: remove the source_id from
   frontmatter. If a reference page loses its last source_id, delete the page. If a
   semantic page loses its last source_id, set status='stale' and surface in wiki
   lint.
8. Update wiki_change_batches and wiki_change_items: pending proposals that cite only
   the forgotten source must be moved to status='superseded' with
   error='source forgotten'.
9. Update retrieval_events: redact returned/selected operational chunk ids that
   no longer resolve, redact citation_snapshots for forgotten sources, and
   append a tombstone marker to the event's debug payload.
10. Update capture_sources: mark the corresponding capture as status='forgotten' so
    the next polling pass does not re-import the same session.
11. Insert a forget_events row recording the operation.
```

Tombstones and re-ingestion:

```text
- A forget_events row prevents re-ingestion of the same content_hash. The normal
  ingest path must skip a candidate file whose content_hash matches a forget tombstone.
- Re-ingesting a forgotten source must require an explicit --force-reingest flag and
  must record a new forget_events row resolving the prior tombstone.
```

Required commands:

```text
brain forget source <document_id> [--reason <text>]
brain forget source --content-hash <hash>
brain forget session <agent>:<session_id>
brain forget memory <memory_id>
brain forget pattern <regex> [--dry-run|--commit]
brain forget range --from <iso> --to <iso> [--dry-run|--commit]
brain forget list                       # show recent forget_events
brain forget inspect <forget_event_id>  # show propagation summary
brain forget undo <forget_event_id>     # best-effort restore from raw if still on disk
```

Dry-run rules:

```text
- pattern and range forget operations must default to --dry-run. The committing form
  requires --commit and must print a confirmation summary of affected counts before
  executing.
- Single-target forgets (source, session, memory) may run without --dry-run but must
  print a confirmation summary before executing.
```

Scope rules:

```text
- Forgetting a source does not delete memories whose content is durable beyond the
  source. Memories survive unless they cite only the forgotten source.
- Forgetting a memory does not delete its source documents.
- Forgetting a session removes only the agent_session_log document for that session
  and the matching capture_sources row, not unrelated memories, wiki pages, or other
  sessions from the same agent.
```

Audit:

```text
- brain provenance check must detect dangling source_ids in memories, wiki pages, and
  retrieval_events. Each dangling reference must either point to a known forget_event
  or be reported as an integrity error.
- Every forget operation must be logged to ~/brain/logs/forget.log in addition to the
  forget_events table.
- brain memory audit must surface memories that lost source_ids due to a forget
  operation and prompt for review.
```

Cloud propagation:

```text
- Forgetting a source does not retroactively recall content already sent to a cloud
  LLM provider in a prior automation run or proposal. The user must request deletion
  through that provider's own retention controls.
- brain forget must surface the count of prior retrieval_events and automation_runs
  that sent the forgotten content to a cloud provider, written to
  forget_events.cloud_egress_followup, so the user knows where else to follow up.
```

Forget rule:

```text
The user must be able to remove personal material they captured, and removal must
propagate predictably across every derived artifact.
```

## 15. Agent Interface

Expose the system through MCP first.

Required MCP tools:

```text
search_knowledge(query, limit)
retrieve_context(task, project)
record_context_feedback(target_type, target_id, useful, note)
get_memories(scope, memory_type, status)
propose_memory(memory_type, scope, content, sources, confidence)
write_agent_session(summary, files_touched, commands_run, outcome, unresolved_issues)
get_project_context(project)
```

`retrieve_context` must return `active_memories` and `candidate_memories` as separate fields. `get_memories` may support status filtering, but memory approval must remain a local human action through the CLI or local Web UI. Memory approval tools and wiki mutation tools are intentionally not exposed over MCP; agents may only propose memories. The CLI exposes additional retrieval knobs (`--budget`, `--mode`, `--debug`) that are not surfaced on MCP to keep agent calls compact.

Optional HTTP endpoints:

```text
POST /query
POST /retrieve-context
POST /ingest
GET /memory
POST /memory/propose
POST /session
```

Agents should receive structured context, not raw search dumps.

## 16. Feedback Loop

Log every retrieval event:

```text
query
timestamp
caller
returned_chunk_ids
selected_chunk_ids
citation_snapshots
context_lineage_events
agent_outcome
```

Use this later for:

```text
recency boosts
source quality scores
retrieval tuning
agent behavior analysis
memory refinement
```

V1 records exposure events for returned chunks, active memories, and wiki pages. Exposure-only events must not improve ranking. Explicit context feedback is recorded through `brain context feedback <target-type> <target-id> --useful/--not-useful --note ...` and MCP `record_context_feedback`. Agent-log ingestion may create weak lineage only for explicit stable IDs such as `mem_...`, `chunk_...`, `doc_...`, `document:...`, and wiki relative paths; V1 must not use fuzzy text matching.

Repeated agent-log popularity is review input, not truth. Memory proposals generated from lineage require independent evidence by default: at least three distinct agent sessions, or two sessions plus explicit useful feedback, or two sessions plus a later stable-ID re-reference. Generated memories remain `status='proposed'` until human approval.

## 17. Execution Phases

### Phase 1: Local Archive And Search

Build:

```text
filesystem layout
SQLite metadata DB
basic ingestion
source hashing
chunking
LanceDB index
BM25/vector hybrid search
CLI query command
```

Success criteria:

```text
Can ingest markdown, transcripts, and agent logs.
Can search with citations.
Can rebuild indexes from raw data.
```

### Phase 2: Agent Context Layer

Build:

```text
MCP server
retrieve_context tool
agent session logging
typed memory schema
memory proposal flow
```

Success criteria:

```text
An agent can retrieve relevant prior context.
An agent can log its session.
An agent can propose durable memories.
```

### Phase 3: Wiki Synthesis

Build:

```text
wiki page generator
project pages
concept pages
decision pages
source-linked summaries
wiki lint command
```

Success criteria:

```text
New source material can update relevant wiki pages.
Wiki pages cite source documents.
Broken links and orphan pages are detectable.
```

### Phase 4: Retrieval Quality

Build:

```text
query expansion
RRF fusion
local reranker
neighbor expansion
hypothetical-question embeddings
retrieval evaluation set
```

Success criteria:

```text
Top results improve on manually tested queries.
Context packets are concise and useful for agents.
```

## 18. Key Engineering Rules

Agents implementing this system should follow these rules:

```text
Never mutate raw source files.
Every derived artifact must be rebuildable.
Every memory must cite evidence.
Every answer from retrieved context should include source IDs.
Prefer simple local components before adding services.
Keep retrieval, wiki synthesis, and memory separate.
Use content hashes for caching.
Log ingestion and retrieval events.
```

## 19. Recommended First Implementation Task

Start with the smallest useful vertical slice:

```text
1. Create ~/brain layout.
2. Create SQLite schema.
3. Ingest markdown files from inbox/.
4. Store documents and chunks.
5. Embed chunks.
6. Index chunks in LanceDB.
7. Add CLI search.
8. Return structured citation snapshots.
```

Do not start with the wiki or memory system until raw ingestion and retrieval are working end to end.

## 20. Evaluation, Debugging, And Observability

The system must be debuggable at every layer: ingestion, chunking, indexing, retrieval, context assembly, wiki synthesis, and memory writing.

The goal is to make failures attributable to a specific pipeline stage instead of only judging the final LLM answer.

Core debugging rule:

```text
A bad answer must be traceable to a pipeline stage.
```

Required debug commands:

```text
brain doctor
brain ingest --dry-run
brain inspect document <document_id>
brain inspect chunks <document_id>
brain index status
brain search <query> --debug
brain wiki lint
brain memory audit
brain provenance check
brain runs list
brain runs inspect <run_id>
```

### Ingestion Checks

The system should verify that source artifacts are discovered, hashed, normalized, and stored correctly.

Checks:

```text
source file exists
content hash computed
source_type detected
document record created
source_path resolves
duplicate detection works
changed files are re-ingested as new versions
failed files are reported with actionable errors
```

Useful commands:

```text
brain doctor
brain ingest --dry-run
brain inspect document <document_id>
brain list documents --status failed
```

### Chunking Debug View

The system should expose chunking output for any document.

Command:

```text
brain inspect chunks <document_id>
```

Output should include:

```text
chunk_id
chunk_index
heading_path
token_count
text preview
start_offset
end_offset
content_hash
```

The chunking debug view should answer:

```text
Are chunks coherent, correctly sized, and traceable back to the source?
```

### Index Health Checks

The system should report whether all expected chunks are searchable.

Command:

```text
brain index status
brain index doctor
brain index optimize
brain index rebuild-vectors
brain db reindex-chunks
brain db reindex-chunks --all-documents
```

Output should include:

```text
document count
chunk count
lexical index row count
embedded chunk count
missing embedding count
LanceDB row count
LanceDB version count
LanceDB data file count
LanceDB retained version bytes
embedding model
last index run
failed index jobs
```

LanceDB is a derived index cache and may be optimized or rebuilt from SQLite chunks. Routine optimization must not delete SQLite rows, raw files, wiki pages, memories, or source evidence. Scheduled optimization should use a conservative cleanup window; one-time manual cleanup may pass `--cleanup-older-than-days 0` when no long-running readers are active. Full vector rebuilds must verify the rebuilt row count against SQLite chunk count and retain the previous LanceDB directory as a timestamped backup unless explicitly requested otherwise.

Oversized chunks should be automatically split into bounded overlapping chunks during ingest. Existing oversized documents may be reindexed from raw files; this rewrites derived SQLite chunks, FTS rows, and LanceDB vectors without deleting source evidence. `--all-documents` intentionally rewrites every document of the selected source type, which is useful after sanitizer or chunking-policy changes.

The index health check should answer:

```text
Did everything that should be searchable actually get indexed?
```

### Retrieval Debug Mode

Search should support a verbose debug mode.

Command:

```text
brain search "query" --debug
```

Debug output should include:

```text
original query
query expansion variants
BM25 top results
vector top results
optional hypothetical-question vector results
RRF fused results
reranked results
final selected chunks
neighbor chunks added
deduplicated chunks removed
context budget used
```

Retrieval debug mode should make it clear whether a poor answer came from:

```text
search miss
bad fusion
bad reranking
bad context assembly
LLM reasoning error
```

### Golden Query Eval Set

The system should maintain a small, local, corpus-specific retrieval evaluation set.

Recommended file:

```text
~/brain/evals/golden_queries.yaml
```

Example:

```yaml
- id: eval-001
  query: "Why did we choose SQLite for metadata?"
  expected_sources:
    - document:personal-knowledge-management-spec-v0.1
  expected_terms:
    - SQLite
    - metadata
    - local-first
  tags:
    - architecture
    - decision
```

Required retrieval metrics:

```text
recall@5
recall@10
MRR
expected source found
expected wiki page found
expected memory found
```

The V1 eval set should measure retrieval quality before measuring full LLM answer quality.

### Answer Quality Review

Selected queries should be reviewed for final answer quality separately from retrieval quality.

Review dimensions:

```text
groundedness
citation correctness
missing relevant context
incorrect claims
stale memory use
```

Manual review is sufficient for V1. LLM-assisted answer grading may be added later.

### Wiki Linting

The wiki schema must be lintable.

Command:

```text
brain wiki lint
```

Checks:

```text
valid YAML frontmatter
unique page ids
allowed page_type
allowed status
required common sections present
page-type-specific sections present
source_ids resolve
related links resolve
decision pages include evidence
stale pages reported
orphan pages reported
```

### Memory Audit

Typed memories must be auditable because agents may treat active memories as operational context.

Commands:

```text
brain memory list --status proposed
brain memory inspect <memory_id>
brain memory approve <memory_id>
brain memory reject <memory_id> --reason <reason>
brain memory archive <memory_id>
brain memory audit
```

Checks:

```text
memory has source evidence
source_ids resolve
confidence is present
scope is valid
status is valid
memory has not been superseded
old memories are marked stale or archived
conflicting memories are flagged
```

### Provenance Checks

The system should validate source chains across raw documents, chunks, wiki pages, memories, and retrieval events.

Command:

```text
brain provenance check
```

Checks:

```text
chunks point to valid documents
wiki pages cite valid documents, chunks, memories, or external source records
memories cite valid sources
retrieval events cite valid chunks
agent sessions cite related documents or files where applicable
```

Provenance rule:

```text
Everything important should trace back to evidence.
```

### Pipeline Run Logs

Ingestion and indexing runs should be inspectable.

Commands:

```text
brain runs list
brain runs inspect <run_id>
```

Each run should track:

```text
started_at
finished_at
duration
documents discovered
documents changed
documents skipped
chunks created
embeddings created
wiki pages proposed
memories proposed
errors
warnings
```

V1 should prioritize simple local observability over a full evaluation platform.

## 21. Implementation Status And Drift Audit

Audit date: 2026-05-25.

Historical note, 2026-07-03: this section is retained as a point-in-time V1 audit. The current implementation status for facts/actions/pages, extraction, entity identity, gardener, synthesis, and regeneration now lives in `docs/chief-of-staff-spec.md`, `docs/architecture-code-guide.md`, `docs/extraction-payload-spec.md`, `docs/entity-layer-spec.md`, and `docs/cos-regeneration-tasklist.md`.

This spec is the target design for a single-user local-first PKM/agent-memory system. The codebase implements most of the V1 surface; some sections (Forgetting, Tension audit, retrieval-quality polish) are partially or not yet implemented. This section lists the current state so the spec stays honest about what ships today.

### 21.1 Implemented

Workspace, ingestion, retrieval:

- Filesystem layout creation under `~/brain`.
- SQLite schema with `documents`, `chunks`, `entities`, `relations`, `memories`, `wiki_pages`, `wiki_change_batches`, `wiki_change_items`, `wiki_interviews`, `ingestion_runs`, `sync_runs`, `automation_runs`, `retrieval_events`, `context_lineage_events`, `agent_sessions`, and `capture_sources` tables, with versioned migrations.
- Markdown / plain-text / agent-log ingestion with `agent_session_log` latest-snapshot retention.
- Source-specific chunking with provenance, including Hyprnote meeting captures and sanitized derived indexing for agent-session logs.
- LanceDB vector index plus SQLite FTS5 lexical search.
- Default deterministic hash embedding provider; optional `SentenceTransformer` (`BAAI/bge-small-en-v1.5`) provider when installed.
- Reciprocal-rank fusion between BM25 and vector candidates.
- Source-aware reranking with capped lineage tie-breakers.
- Retrieval modes (`compact`, `default`, `broad`, `inspect`) with bounded token budgets and per-source caps.
- Citation snapshots, retrieval event logging, and context lineage exposure events.

Wiki, memory, agent interface:

- `brain wiki migrate-to-facts`, `brain wiki curate-facts`, and `brain wiki promote-curation` for Chief-of-Staff fact/page curation.
- Chief-of-Staff fact/page curation with archived legacy `wiki_change_*` compatibility tables.
- `brain wiki lint` and `brain provenance check`.
- Memory propose / list / inspect / approve / reject / archive / audit / export-all / import.
- `AgentFailurePatternMemory` proposal synthesis from agent logs and lineage-driven proposal synthesis with independent-evidence thresholds.
- `record_context_feedback` (CLI + MCP) and exposure-aware lineage scoring.
- MCP server with `search_knowledge`, `retrieve_context`, `record_context_feedback`, `get_memories`, `propose_memory`, `write_agent_session`, `get_project_context`.

Operability:

- `brain init`, `brain setup`, `brain init --wizard` guided installer with `--dry-run`, `--json`, `--yes`.
- `brain ui` loopback Web UI with token auth and pages for status, setup, sync, jobs, logs, and memory review (stdlib `http.server`).
- `brain doctor`, `brain inspect document|chunks`, `brain index status|doctor|optimize|rebuild-vectors`, `brain db reindex-chunks`, `brain runs list|inspect`.
- `brain capture agents` for Codex, Claude Code, OpenCode, and opt-in Hyprnote.
- `brain automation nightly --if-due` with optional `--with-llm-memory-proposals`.
- Automation run storage caps nested error/stderr/traceback payloads to avoid persisting full prompts or provider failure streams in SQLite.
- macOS LaunchAgent install/render/status for the frequent capture job and the nightly maintenance job.
- Primary/Secondary sync: config model, role init, peer add, SSH host-key pinning, rsync transport, outbox export, staged pull/push with manifest verification, `sync_runs` log, `brain sync status|conflicts|acceptance|mirror-hash|rebuild-mirror-index`.
- `brain scheduler` adapter with macOS LaunchAgent support (systemd and cron stubs intentionally not-yet-implemented).

### 21.2 Partially Implemented

- **Retrieval quality (Section 10 / Phase 4):** RRF, source-aware rerank, lineage tie-breakers, neighbor caps, and excerpting ship. Query expansion, hypothetical-question vector search, neighbor-chunk expansion, and a true cross-encoder local reranker are not yet implemented.
- **Embeddings (Section 4 stack):** local hash embedding + optional `SentenceTransformer` ship. Cloud embedding providers and a configurable provider toggle are not yet implemented.
- **Memory audit (Section 20 Memory Audit):** validates `memory_type`, `status`, `scope`, `source_ids` presence, and `confidence` presence. Duplicate detection, staleness flagging, conflict detection, and broken-source surfacing are pending.
- **Run logs (Section 20 Pipeline Run Logs):** `ingestion_runs` tracks discovered/changed/skipped/chunks/embeddings/errors/warnings. `wiki pages proposed` and `memories proposed` per-run counters are not yet populated.

### 21.3 Not Yet Implemented

- **Section 14 Forgetting And Redaction.** `forget_events` table does not exist. None of `brain forget source|session|memory|pattern|range|list|inspect|undo` is implemented. There is no tombstone-based re-ingestion guard. `brain provenance check` does not yet detect dangling references caused by forgets.
- **Section 9.2 Tension Audit.** Explicitly future work; no implementation.
- **Section 20 Golden Query Eval Set.** `~/brain/evals/golden_queries.yaml` is created at workspace init, but there is no `brain eval` runner producing `recall@5`, `recall@10`, `MRR`, or expected-source/page/memory metrics.
- **Section 20 `brain list documents --status failed`.** No `brain list documents` command exists at all yet.
- **Section 15 HTTP API.** The optional HTTP endpoints (`POST /query`, `POST /retrieve-context`, `POST /ingest`, `GET /memory`, `POST /memory/propose`, `POST /session`) are not implemented; only MCP and the local Web UI are.
- **`brain ui service install/status/uninstall`** for running the Web UI as a managed service.
- **Non-macOS schedulers.** systemd user timers and cron adapters intentionally raise a clear not-yet-implemented error.
- **Structured `agent_sessions` import from Secondary** and **Secondary read-only MCP mode** (also called out in the sync spec).
- **`relations` table.** Present in the schema but no writer/reader uses it. Entity identity and fact-entity links are now implemented in the Chief-of-Staff/entity layer; typed relation edges remain deferred.

### 21.4 Spec Drift Notes

- The retrieval context packet field names returned by `service.retrieve_context` were originally specified as `selected_chunks`, `source_citations`, `related_wiki_pages`, and `confidence_notes`. The implementation uses `supporting_chunks`, `citation_snapshots` (with `citations` as a back-compat alias), and `relevant_wiki_pages`. Section 10 has been updated to match.
- The MCP `retrieve_context` signature in the spec previously included a `repo` parameter. The implementation accepts only `task` and `project`. Section 15 has been updated. Per-call `budget` and `mode` knobs are CLI-only by design to keep MCP payloads compact.
- The `documents` table schema in Section 7 has been expanded to match the live schema (`raw_path`, `origin_node_id`, `logical_source_key`, `version`). These columns are required to support Primary/Secondary sync.
- The Web UI is built on stdlib `http.server` rather than FastAPI; this is an intentional V1 dependency tradeoff also noted in the sync spec.
