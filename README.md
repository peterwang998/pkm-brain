# PKM Brain

Local personal knowledge management and agent memory tool.

V1 provides:

- filesystem workspace at `~/brain`
- SQLite canonical metadata store
- Markdown/plain-text ingestion
- chunking with provenance
- SQLite FTS5 lexical search
- LanceDB vector index with local deterministic embeddings by default
- CLI commands for search, inspection, wiki linting, memory audit, and run logs
- MCP server tools for agent access

## Quickstart

```bash
uv sync
uv run brain init
uv run brain ingest
uv run brain search "sqlite metadata" --debug
uv run brain mcp
```

Runtime data is stored outside the repo in `~/brain` by default.
