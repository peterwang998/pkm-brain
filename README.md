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
- scheduled capture from Codex, Claude, OpenCode, and Hyprnote via macOS LaunchAgent

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
uv run brain wiki synthesize --dry-run
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

## Install On A New Mac

These instructions install the source repo, initialize a local brain workspace, and schedule both background jobs.

### 1. Install prerequisites

Install Xcode command line tools if needed:

```bash
xcode-select --install
```

Install `uv`:

```bash
brew install uv
```

If Homebrew is not installed, install it first from <https://brew.sh/>.

### 2. Clone the repo

Clone the project into the expected source directory:

```bash
cd ~
git clone https://github.com/peterwang998/pkm-brain.git
cd ~/pkm-brain
```

For a private repository, authenticate GitHub first with your preferred method, for example `gh auth login`.

### 3. Install Python dependencies

```bash
uv sync
```

Verify the CLI loads:

```bash
uv run brain doctor
```

### 4. Initialize the local brain workspace

Fresh workspace:

```bash
uv run brain init --home ~/brain
```

If migrating an existing brain from another Mac, restore or copy the old `~/brain` directory before running scheduled jobs. The repo is source code; the private runtime data lives outside git in `~/brain`.

Expected runtime layout:

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
  evals/
```

### 5. Smoke test the pipeline

Run a normal ingest:

```bash
uv run brain ingest --home ~/brain
```

Run core checks:

```bash
uv run brain index status --home ~/brain
uv run brain provenance check --home ~/brain
uv run brain wiki lint --home ~/brain
uv run brain memory audit --home ~/brain
```

Optional: preview local capture if Codex, Claude, OpenCode, or Hyprnote are installed on this Mac:

```bash
uv run brain capture agents --dry-run --home ~/brain
```

### 6. Install the 10-minute agent-log ingestion job

This job captures local Codex, Claude, and OpenCode session logs, then ingests the inbox. Hyprnote capture is optional and must be enabled explicitly.

```bash
cd ~/pkm-brain
uv run brain launch-agent install --interval 600 --home ~/brain
```

Verify it is loaded:

```bash
uv run brain launch-agent status
```

Expected status:

```text
loaded = true
run interval = 600 seconds
```

Logs:

```text
~/brain/logs/launchagent.out.log
~/brain/logs/launchagent.err.log
```

### 7. Install the nightly maintenance job

This job performs the broader self-healing pass:

```text
capture agents
ingest inbox
wiki synthesize with generated overwrite
index status
provenance check
wiki lint
memory audit
record automation run
```

Install it:

```bash
cd ~/pkm-brain
uv run brain launch-agent install-nightly --home ~/brain
```

Install it with unapproved Codex wiki proposals enabled:

```bash
cd ~/pkm-brain
uv run brain launch-agent install-nightly --home ~/brain --with-llm-wiki-proposals --provider codex
```

The LaunchAgent stores the provider/model choice and the absolute Codex CLI path. With `--provider codex`, no API key is stored by pkm-brain; Codex CLI uses its own local login.

Verify it is loaded:

```bash
uv run brain launch-agent nightly-status
```

Expected status:

```text
loaded = true
run interval = 3600 seconds
```

The nightly job wakes hourly and runs real work only when the last successful nightly run is more than 20 hours old:

```text
brain automation nightly --if-due --due-after-hours 20
```

This is better for laptops than a single fixed overnight time because the next hourly check after wake can catch up if the machine slept through the night.

Logs:

```text
~/brain/logs/nightly-maintenance.out.log
~/brain/logs/nightly-maintenance.err.log
```

### 8. Verify both scheduled jobs

```bash
uv run brain launch-agent status
uv run brain launch-agent nightly-status
tail -n 40 ~/brain/logs/launchagent.out.log
tail -n 40 ~/brain/logs/nightly-maintenance.out.log
```

The capture LaunchAgent is a deterministic local Python job. The nightly LaunchAgent is also deterministic unless installed with `--with-llm-wiki-proposals`, in which case it calls the configured LLM provider to create unapproved wiki proposals.

### 9. Uninstall scheduled jobs

```bash
uv run brain launch-agent uninstall
uv run brain launch-agent uninstall-nightly
```

## Agent Log Automation

PKM Brain can capture local agent session logs into the inbox, then ingest them through the normal pipeline. Hyprnote meeting capture is available as an opt-in source.

Supported local agent sources:

- Codex: `~/.codex/state_5.sqlite` plus rollout JSONL files
- Claude: `~/.claude/projects/**/*.jsonl`
- OpenCode: `~/.local/share/opencode/opencode.db`
- Hyprnote, opt-in only: `~/Library/Application Support/hyprnote/sessions/*/{_summary.md,_memo.md,transcript.json}`

Capture is intentionally routed through `~/brain/inbox`, not `~/brain/raw`:

```text
agent session stores
  -> brain capture agents
  -> ~/brain/inbox/agent_logs/<agent>/*.md
  -> ~/brain/inbox/documents/hyprnote/*.md
  -> brain ingest
  -> ~/brain/raw + SQLite + FTS5 + LanceDB
```

Preview capture without writing artifacts:

```bash
uv run brain capture agents --dry-run
```

Capture all supported agent logs:

```bash
uv run brain capture agents
uv run brain ingest
```

Capture Hyprnote explicitly:

```bash
uv run brain capture agents --agent hyprnote
uv run brain ingest
```

Run the scheduled-ingestion command manually:

```bash
uv run brain automation run-agent-log-ingest
```

Install a user-level macOS LaunchAgent that polls every 10 minutes:

```bash
uv run brain launch-agent install --interval 600
```

Install it with Hyprnote included:

```bash
uv run brain launch-agent install --interval 600 --include-hyprnote
```

Inspect or remove it:

```bash
uv run brain launch-agent status
uv run brain launch-agent uninstall
```

LaunchAgent logs are written to:

```text
~/brain/logs/launchagent.out.log
~/brain/logs/launchagent.err.log
```

This is scheduled polling, not a general-purpose filesystem watcher.

## Nightly Maintenance

PKM Brain also supports a separate nightly maintenance path for broader self-healing checks.

Run it manually:

```bash
uv run brain automation nightly
```

The nightly job runs:

- agent-log capture
- inbox ingestion
- generated wiki synthesis with generated-page overwrite
- index status collection
- provenance check
- wiki lint
- memory audit

Install the nightly macOS LaunchAgent:

```bash
uv run brain launch-agent install-nightly
```

The nightly LaunchAgent uses an hourly wake-check by default:

```text
com.pkm-brain.nightly-maintenance
StartInterval = 3600
command = brain automation nightly --if-due --due-after-hours 20
```

This pattern is intentional for laptops. If the machine sleeps through a fixed overnight time, the next hourly check after wake can run maintenance if the last successful run is old enough.

Inspect or remove the nightly job:

```bash
uv run brain launch-agent nightly-status
uv run brain launch-agent uninstall-nightly
```

Nightly logs are written to:

```text
~/brain/logs/nightly-maintenance.out.log
~/brain/logs/nightly-maintenance.err.log
```

## Wiki Synthesis

PKM Brain maintains two wiki layers:

- compiled semantic pages under `~/brain/wiki/projects/`, `concepts/`, `decisions/`, and `open_loops/`
- source-backed reference pages under `~/brain/wiki/references/<source_type>/`

The compiled pages are the intended human-readable wiki. They group evidence across sources, cite document IDs, and link related concepts with Obsidian-style wikilinks. Reference pages are provenance aids for inspecting a single ingested source.

Preview generated pages:

```bash
uv run brain wiki synthesize --dry-run
```

Create or update generated wiki pages:

```bash
uv run brain wiki synthesize
```

Generated semantic pages are written under:

```text
~/brain/wiki/index.md
~/brain/wiki/projects/
~/brain/wiki/concepts/
~/brain/wiki/decisions/
~/brain/wiki/open_loops/
```

Generated reference pages are written under:

```text
~/brain/wiki/references/<source_type>/
```

The V1 compiler is deterministic and conservative:

- updates generated semantic pages only when source evidence matches known compiler patterns
- cites source document IDs on generated semantic pages
- uses `[[path/to/page]]` wikilinks for related pages
- keeps noisy source excerpts in reference pages rather than concept pages
- skips hand-edited pages that do not contain the generated marker

## Unapproved Wiki Proposals

Agents and optional nightly LLM jobs can propose wiki changes without directly editing approved Markdown pages.

Proposal state lives in SQLite:

```text
wiki_change_batches
wiki_change_items
wiki_interviews
```

The workflow is:

```text
agent or nightly LLM proposes a batch
  -> status: proposed or needs_interview
  -> human runs an interview/review
  -> status: approved or rejected
  -> approved batch patches wiki files section-by-section
  -> status: applied
```

List and inspect proposals:

```bash
uv run brain wiki proposals list
uv run brain wiki proposals inspect <batch_id>
```

Interview, reject, or apply:

```bash
uv run brain wiki interview <batch_id>
uv run brain wiki proposals reject <batch_id>
uv run brain wiki apply <batch_id>
```

Generate proposals from recent sources with an LLM provider:

```bash
uv run brain wiki propose-from-sources --provider codex
uv run brain wiki propose-from-sources --provider openai
uv run brain wiki propose-from-sources --provider anthropic
uv run brain wiki propose-from-sources --provider ollama
```

Enable proposal generation during nightly maintenance:

```bash
uv run brain automation nightly --with-llm-wiki-proposals --provider codex
```

If `--with-llm-wiki-proposals` is set and the provider is not configured, the nightly run fails. Normal nightly maintenance without this flag remains deterministic local Python.

Agents can also create proposals through MCP with `propose_wiki_update`.

### LLM Provider Configuration

Check provider configuration without printing secrets:

```bash
uv run brain llm doctor --provider codex
uv run brain llm doctor --provider openai
uv run brain llm doctor --provider anthropic
uv run brain llm doctor --provider ollama
```

Codex CLI:

```bash
codex login
export PKM_BRAIN_LLM_PROVIDER=codex
export PKM_BRAIN_CODEX_MODEL=gpt-5.5
# Optional:
export PKM_BRAIN_CODEX_BIN=/path/to/codex
export PKM_BRAIN_CODEX_CWD=~/pkm-brain
export PKM_BRAIN_CODEX_TIMEOUT_SECONDS=900
```

The Codex provider runs `codex exec` in read-only, non-interactive mode and captures the final message as JSON. It can create unapproved wiki proposal batches, but it cannot directly patch approved wiki Markdown.

OpenAI-compatible:

```bash
export PKM_BRAIN_LLM_PROVIDER=openai
export OPENAI_API_KEY=...
export PKM_BRAIN_OPENAI_MODEL=gpt-5.5
# Optional:
export PKM_BRAIN_OPENAI_BASE_URL=https://api.openai.com/v1
```

Anthropic:

```bash
export PKM_BRAIN_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=...
export PKM_BRAIN_ANTHROPIC_MODEL=claude-sonnet-4-5
# Optional:
export PKM_BRAIN_ANTHROPIC_BASE_URL=https://api.anthropic.com
```

Ollama:

```bash
export PKM_BRAIN_LLM_PROVIDER=ollama
export PKM_BRAIN_OLLAMA_MODEL=llama3.1
# Optional:
export PKM_BRAIN_OLLAMA_BASE_URL=http://localhost:11434
```

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
- mechanical source-backed wiki reference synthesis
- unapproved wiki proposal batches with interview/apply workflow
- Codex CLI, OpenAI-compatible, Anthropic, and Ollama LLM provider adapters for wiki proposals
- memory proposal and audit commands
- ingestion run logs
- Codex, Claude, OpenCode, and Hyprnote capture
- macOS LaunchAgent scheduled polling
- hourly due-check nightly maintenance LaunchAgent
- MCP server wrapper

Not yet implemented:

- autonomous memory activation
- query expansion
- local reranking
- cloud embedding providers
- general-purpose background filesystem watcher
- HTTP API
- GUI or Obsidian plugin

## Development

Run tests and lint:

```bash
uv run pytest
uv run ruff check .
```

The project intentionally keeps private knowledge artifacts out of git. Do not commit `~/brain`, SQLite databases, LanceDB indexes, logs, or secrets.
