# Personal Knowledge Management / Agent Memory System Spec

Spec version: 0.1

Status: Draft for human review

Last updated: 2026-05-04

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

Raw files must not be mutated after ingestion. If content changes, create a new version with a new hash.

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
  content TEXT,
  confidence REAL,
  source_ids TEXT,
  status TEXT,
  created_at TEXT,
  updated_at TEXT,
  last_seen_at TEXT
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

Agent session logs:

```text
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

These steps do not call an LLM by default. They are local Python pipeline operations. LLM-assisted enrichment can be added later as an explicit stage.

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
selected_chunks
source_citations
related_memories
related_wiki_pages
confidence_notes
omitted_due_to_budget
```

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
brain wiki propose-from-sources --provider <openai|anthropic|ollama>
brain automation nightly --with-llm-wiki-proposals --provider <provider>
```

Required review/apply entrypoints:

```text
brain wiki proposals list
brain wiki proposals inspect <batch_id>
brain wiki interview <batch_id>
brain wiki proposals reject <batch_id>
brain wiki apply <batch_id>
```

The system must support OpenAI-compatible, Anthropic, and Ollama provider adapters. Provider configuration must be inspectable with `brain llm doctor` without printing secrets. If nightly LLM proposals are explicitly enabled and provider configuration is missing, the nightly run must fail.

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

Agents should propose important memories before activation unless explicitly configured otherwise.

Example:

```text
type: PreferenceMemory
scope: global
content: User prefers direct, implementation-focused engineering responses.
confidence: 0.85
sources: [agent_session_2026_05_04]
status: proposed
```

## 13. Agent Interface

Expose the system through MCP first.

Required MCP tools:

```text
search_knowledge(query, filters)
retrieve_context(task, project, repo, budget)
get_memories(scope, memory_type)
propose_memory(memory_type, scope, content, sources, confidence)
write_agent_session(summary, files_touched, commands_run, outcome)
get_project_context(project)
```

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

## 14. Feedback Loop

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

## 15. Execution Phases

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

## 16. Key Engineering Rules

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

## 17. Recommended First Implementation Task

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

## 18. Evaluation, Debugging, And Observability

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
