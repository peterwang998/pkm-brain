# Personal Knowledge Management / Agent Memory System Spec

Spec version: 0.1

Status: Draft for human review

Last updated: 2026-05-17

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

### 4.1 Pending Installation Wizard

The current V1 CLI initialization path is a direct workspace initializer, not a guided installation wizard. A publishable install flow should add an interactive wizard as a pending implementation item.

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

### 4.2 Pending Local Web UI

Add an optional local Web UI as a human control plane for Brain status, setup, scheduled jobs, sync validation, and memory review.

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
sensitivity
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
  content_hash TEXT,
  created_at TEXT,
  ingested_at TEXT,
  project TEXT,
  tags TEXT,
  sensitivity TEXT,
  status TEXT
)
```

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
  sensitivity TEXT,
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

Minimum wiki proposal schema:

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

Minimum forget event schema:

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
- Preserve user request.
- Preserve commands run.
- Preserve files touched.
- Preserve errors.
- Preserve final outcome.
- Extract decisions and unresolved issues.
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

Nightly maintenance should run these local deterministic tasks:

```text
1. capture agents
2. ingest inbox
3. synthesize generated wiki pages with generated-page overwrite
4. collect index status
5. run provenance check
6. run wiki lint
7. run memory audit
8. record automation run summary
```

These steps do not call an LLM by default. They are local Python pipeline operations. LLM-assisted enrichment must be enabled explicitly with proposal flags.

Optional nightly LLM proposal stages:

```text
brain automation nightly --with-llm-wiki-proposals --provider <provider>
brain automation nightly --with-llm-memory-proposals --provider <provider>
```

`--with-llm-memory-proposals` analyzes recent agent logs, structured `agent_sessions`, unresolved issues, failed or suspicious command history, retrieval events, and existing failure memories. It may create only `proposed` `AgentFailurePatternMemory` records and must deduplicate against existing proposed or active failure memories. It uses the existing `com.pkm-brain.nightly-maintenance` LaunchAgent; no separate memory proposal LaunchAgent should be created.

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

Context packet format:

```text
query
retrieval_mode
selected_chunks
source_citations
active_memories
candidate_memories
related_wiki_pages
confidence_notes
omitted_due_to_budget
```

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
4. Insert links between related semantic pages using Obsidian-style wikilinks.
5. Cite source document ids for every generated page.
```

The command must not silently overwrite hand-edited human pages. A generated page is safe to update only if it contains the generated marker. If a target page exists without the marker, the system must skip it and report the skip.

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

The wiki compiler should process sources in this order:

```text
1. Read latest ingested documents and chunks.
2. Generate or update reference pages for each source.
3. Extract candidate concepts, projects, decisions, people, and open loops.
4. Merge candidates with existing generated semantic pages.
5. Update semantic pages with source-backed summaries, links, and open questions.
6. Update wiki/index.md.
7. Run wiki lint.
```

The V1 compiler may use deterministic extraction rules and templates.

LLM-written wiki maintenance must use a two-state workflow:

```text
Unapproved state
  Agents and optional nightly LLM jobs may create wiki proposal batches.
  These proposals live in SQLite and do not mutate approved Markdown files.

Approved state
  A human review/interview approves a proposal batch.
  Approved batches patch wiki Markdown files section-by-section.
```

Agents and nightly jobs may propose changes, but they must not directly write approved wiki pages. This keeps the workflow close to the LLM Wiki pattern while preserving reviewability and rollback.

Required proposal entrypoints:

```text
MCP propose_wiki_update(...)
brain wiki propose-from-sources --provider <codex|openai|anthropic|ollama>
brain automation nightly --with-llm-wiki-proposals --provider <provider>
brain memory propose-from-sources --provider <codex|openai|anthropic|ollama>
brain automation nightly --with-llm-memory-proposals --provider <provider>
```

Required review/apply entrypoints:

```text
brain wiki proposals list
brain wiki proposals inspect <batch_id>
brain wiki interview <batch_id>
brain wiki proposals reject <batch_id>
brain wiki apply <batch_id>
```

The system must support Codex CLI, OpenAI-compatible, Anthropic, and Ollama provider adapters. The Codex adapter should use `codex exec` in read-only, non-interactive mode so pkm-brain can use the user's local Codex login instead of a separate API key. Provider configuration must be inspectable with `brain llm doctor` without printing secrets. If nightly LLM proposals are explicitly enabled and provider configuration is missing, the nightly run must fail.

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

## 13. Sensitivity And Egress Controls

Personal knowledge is mixed-sensitivity. The system must let the user mark sources and memories with a sensitivity tier and must enforce those tiers at retrieval, synthesis, and proposal time.

Sensitivity tiers (stored on `documents.sensitivity` and `memories.sensitivity`):

```text
public      Shareable freely. Eligible for any retrieval, synthesis, or proposal target.
normal      Default. Eligible for all local operations and for cloud-LLM-backed
            enrichment.
private     Local-only. Must not be sent to any cloud LLM provider for synthesis,
            proposal, or retrieval-time prompting. May still be returned by local CLI
            search and by local MCP retrieve_context.
secret      Excluded from automatic retrieval. Must be opted in per call via an explicit
            filter. Must never be sent to a cloud LLM provider. Must never be embedded
            against a cloud embedding API.
```

Provider classification for egress purposes:

```text
local   ollama, local hash embedding, local SentenceTransformer
cloud   anthropic, openai-compatible, codex, cloud embedding providers
```

The Codex provider is treated as cloud because requests flow through a remote model even though the CLI is local.

Required egress rules:

```text
1. Any code path that calls a cloud provider for completion or embedding must filter
   out documents, chunks, and memories with sensitivity in {private, secret} before
   constructing the prompt or embedding payload.
2. Nightly automation that uses a cloud provider must abort with a clear error if any
   candidate source carries sensitivity=secret, unless --include-secret is passed
   explicitly with a reason recorded on the automation run.
3. Wiki and memory proposal generation must record the effective sensitivity filter
   and the count of sources excluded by tier on every run.
4. The local hash embedding provider and the local SentenceTransformer provider may
   embed all tiers.
```

Default sensitivity inference at ingest:

```text
- agent_session_log captures default to normal unless the capture adapter detected
  secret-shaped material during redaction; those documents must be marked private at
  ingest time.
- markdown_note and meeting_transcript default to normal.
- hyprnote_meeting defaults to private. The user must promote a meeting explicitly to
  send it to a cloud provider.
- manual_entry defaults to normal.
```

Retrieval rules:

```text
- retrieve_context must include the effective sensitivity of each returned chunk,
  wiki page, and memory so callers can decide whether to forward content to a cloud
  LLM in their own pipeline.
- search_knowledge must accept a max_sensitivity filter. The default is normal for
  MCP callers and unrestricted for local CLI callers.
- Retrieval events must record the effective sensitivity filter applied, the counts
  excluded by tier, and the caller's transport.
```

Required commands:

```text
brain set-sensitivity document <document_id> <tier> [--reason <text>]
brain set-sensitivity memory <memory_id> <tier> [--reason <text>]
brain list documents --sensitivity <tier>
brain list memories --sensitivity <tier>
brain doctor                          # must report configured egress policy and
                                       # provider classifications
brain egress preview --provider <p>   # show counts of sources eligible vs excluded
                                       # for a given provider, without sending data
```

Sensitivity is mutable and audited:

```text
- Every sensitivity change must be logged with old tier, new tier, reason, and actor.
- Promoting a document from private or secret to normal or public must require an
  explicit --confirm flag.
- Demoting from public to a more restrictive tier may happen without confirmation.
```

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
9. Update retrieval_events: redact selected and cited chunk ids that no longer
   resolve, and append a tombstone marker to the event's debug payload.
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
search_knowledge(query, filters)
retrieve_context(task, project, repo)
get_memories(scope, memory_type, status)
propose_memory(memory_type, scope, content, sources, confidence)
write_agent_session(summary, files_touched, commands_run, outcome)
get_project_context(project)
```

`retrieve_context` must return `active_memories` and `candidate_memories` as separate fields. `get_memories` may support status filtering, but memory approval must remain a local human action through the CLI or local Web UI.

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
cited_chunk_ids
user_feedback
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

V1 only needs logging. Ranking feedback can come later.

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
8. Return cited chunks.
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
```

Output should include:

```text
document count
chunk count
lexical index row count
embedded chunk count
missing embedding count
embedding model
last index run
failed index jobs
```

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
