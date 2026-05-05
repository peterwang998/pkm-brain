# PKM Brain

PKM Brain is a local-first personal knowledge management and agent memory tool.

The goal is to create a practical "second brain" that can store messy personal working knowledge, make it searchable, and expose useful context to coding agents through MCP.

It is designed for source material such as:

- session notes
- meeting transcripts
- working documents
- coding-agent logs
- Markdown notes
- plain-text captures

The project follows one core rule:

```text
Raw sources are evidence.
Wiki pages are synthesized knowledge.
Memories are curated, typed claims derived from evidence.
Indexes are rebuildable derived artifacts.
```

## Architecture

PKM Brain separates source code from private runtime data:

```text
~/pkm-brain/     source code repo
~/brain/         private local knowledge workspace
```

The local workspace is initialized as:

```text
~/brain/
  inbox/         new files waiting to be ingested
  raw/           immutable copied source artifacts
  wiki/          human-readable synthesized Markdown pages
  memory/        optional memory files
  indexes/       LanceDB and other rebuildable indexes
  db/            SQLite canonical metadata database
  logs/          operational logs
  config/        local settings
  evals/         local retrieval evals
```

V1 uses:

- filesystem workspace at `~/brain`
- SQLite canonical metadata store at `~/brain/db/brain.sqlite`
- SQLite FTS5 lexical search
- LanceDB vector index under `~/brain/indexes/lancedb`
- deterministic local hash embeddings by default, so the tool works offline
- Markdown/plain-text ingestion
- chunking with provenance
- CLI commands for search, inspection, wiki linting, memory audit, and run inspection
- MCP server tools for agent access

The runtime data flow is:

```text
inbox files
  -> raw copied artifacts
  -> SQLite document records
  -> text chunks with provenance
  -> SQLite FTS5 lexical index
  -> LanceDB vector index
  -> CLI search / context packets / MCP tools
```

SQLite is the system of record. LanceDB is a derived retrieval index and can be rebuilt from SQLite chunks.

## Human And Agent Interfaces

Humans interact through the CLI:

```bash
uv run brain init
uv run brain ingest
uv run brain search "sqlite metadata" --debug
uv run brain inspect chunks <document_id>
uv run brain wiki lint
uv run brain memory audit
```

Agents interact through MCP:

```bash
uv run brain mcp
```

The MCP server exposes tools for:

- `search_knowledge`
- `retrieve_context`
- `get_memories`
- `propose_memory`
- `write_agent_session`
- `get_project_context`

Agents should use these tools instead of reading SQLite or LanceDB directly.

## Quickstart

```bash
uv sync
uv run brain init
uv run brain ingest
uv run brain search "sqlite metadata" --debug
uv run brain mcp
```

Runtime data is stored outside the repo in `~/brain` by default.

## Current V1 Scope

Implemented:

- local workspace initialization
- SQLite schema creation
- Markdown and plain-text ingestion
- source hashing and duplicate detection
- raw artifact copying
- chunking with document provenance
- SQLite FTS5 search
- LanceDB vector search
- reciprocal-rank fusion for hybrid retrieval
- structured context retrieval
- wiki schema linting
- memory proposal and audit commands
- ingestion run logs
- MCP server wrapper

Not yet implemented:

- automatic wiki synthesis
- autonomous memory activation
- query expansion
- local reranking
- cloud embedding providers
- background file watcher
- HTTP API
- GUI or Obsidian plugin

## Development

Run tests and lint:

```bash
uv run pytest
uv run ruff check .
```

The project intentionally keeps private knowledge artifacts out of git. Do not commit `~/brain`, SQLite databases, LanceDB indexes, logs, or secrets.
