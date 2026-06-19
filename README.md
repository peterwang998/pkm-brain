# PKM Brain

[![CI](https://github.com/peterwang998/pkm-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/peterwang998/pkm-brain/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

PKM Brain is a local-first personal knowledge management and agent memory system for Codex, Claude Code, and other MCP-aware tools.

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

## Highlights

- Local-first runtime layout: private knowledge lives in `~/brain`, while the source repo stays clean.
- Hybrid retrieval: SQLite FTS5, LanceDB vectors, reciprocal-rank fusion, source-aware reranking, and bounded context packets.
- Agent access through MCP tools for search, context retrieval, memory proposals, wiki proposals, and session logging.
- Review-gated memory and wiki workflows, so agents can propose durable knowledge without silently approving it.
- Scheduled capture for Codex, Claude Code, OpenCode, and Hyprnote logs, with optional Primary/Secondary sync.

## Quickstart

```bash
uv sync
uv run brain init --home ~/brain
uv run brain ingest --home ~/brain
uv run brain search "sqlite metadata" --debug --home ~/brain
uv run brain mcp --home ~/brain
```

Runtime data is stored outside the repo in `~/brain` by default.

## Privacy Model

PKM Brain is designed to keep private knowledge artifacts out of git. Raw sources, SQLite databases, LanceDB indexes, logs, local config, and secrets belong in the runtime workspace, not the public repository.

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
  - Brain opens this database in WAL mode and applies a short busy timeout plus five incremental retries for transient writer-lock contention from overlapping scheduled jobs.
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

For a multi-device setup with a laptop as the canonical Brain and a LAN-only secondary machine as a mirror/outbox node, see [Primary / Secondary Brain Sync Spec](docs/primary-secondary-brain-sync-spec.md).

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
uv run brain memory approve <memory_id>
uv run brain memory reject <memory_id> --reason "not durable enough"
uv run brain memory archive <memory_id>
```

Agents interact through MCP:

```bash
uv run brain mcp
```

The MCP server exposes tools for:

- `search_knowledge`
- `retrieve_context`
- `record_context_feedback`
- `get_memories`
- `propose_memory`
- `propose_wiki_update`
- `list_wiki_proposals`
- `inspect_wiki_proposal`
- `write_agent_session`
- `get_project_context`

Agents should use these tools instead of reading SQLite or LanceDB directly.

`retrieve_context` returns reviewed `active_memories` separately from unreviewed `candidate_memories`. Active memories are trusted operational guidance. Candidate memories are proposed hypotheses and should not be treated as authoritative until a human approves them through the local CLI. MCP agents can propose memories with `propose_memory`, but they cannot approve, reject, or archive memories.

`search_knowledge` and `retrieve_context` use source-aware reranking after BM25/vector fan-out. Meetings, notes, transcripts, web clips, and working documents get a positive default signal for normal knowledge queries. Agent session logs are still searchable, but they are downranked for general knowledge and can rank highly for agent/session/tool/implementation-history queries.

`retrieve_context` uses bounded retrieval so noisy sources do not consume the whole agent context. The default mode uses an 8,000-token context budget, source-specific caps, and excerpts oversized chunks. Agent session logs are compressed more aggressively than curated notes or meeting sources. The MCP tool intentionally keeps a simple `task`/`project` surface and uses the default bounded mode. The CLI and service layer also support `--mode compact`, `--mode broad`, and `--mode inspect` for manual or scripted use. Returned chunks include `retrieval_score`, `selection_reasons`, `suppressed`, `suppress_reasons`, `retrieval_noise_reasons`, `original_token_count`, `returned_token_count`, `omitted_tokens`, and `excerpted` metadata.

Context feedback is explicit and advisory:

```bash
uv run brain context feedback chunk chunk_<id> --useful --note "good source for this topic"
uv run brain context feedback document doc_<id> --not-useful --note "wrong project"
```

Exposure-only lineage is recorded for returned chunks, active memories, and wiki pages, but exposure does not improve ranking. Explicit useful/not-useful feedback and repeated stable-ID references from independent agent sessions act only as capped tie-breakers.

### Codex Persistent Memory

For Codex, use MCP as the live data-access layer and the bundled `brain-memory` skill as the activation policy.

Configure the MCP server:

```bash
codex mcp add pkm-brain -- ~/pkm-brain/.venv/bin/brain mcp --home ~/brain
codex mcp get pkm-brain
```

Install the Codex skill:

```bash
mkdir -p ~/.codex/skills
cp -R ~/pkm-brain/skills/brain-memory ~/.codex/skills/
```

After restarting Codex, the skill teaches Codex when to query Brain, when to skip retrieval, how to follow `raw_context` links, and when to create unapproved memory or wiki proposals. The skill does not store private knowledge; it only contains instructions for using the local Brain tools.

### Claude Code Persistent Memory

For Claude Code, use the same MCP server plus the bundled local Claude plugin.

Configure the MCP server:

```bash
claude mcp add -s user pkm-brain -- ~/pkm-brain/.venv/bin/brain mcp --home ~/brain
claude mcp get pkm-brain
```

Install the Claude Code plugin:

```bash
claude plugin marketplace add ~/pkm-brain/claude-marketplace --scope user
claude plugin install pkm-brain-memory@pkm-brain-local --scope user
claude plugin list
```

The plugin provides a `/brain-memory` skill and model-invoked guidance for when Claude should query Brain. If Claude Code is not logged in, start an interactive Claude Code session and run `/login`.

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
uv run brain index doctor --home ~/brain
uv run brain provenance check --home ~/brain
uv run brain wiki lint --home ~/brain
uv run brain memory audit --home ~/brain
```

Optional: preview local capture if Codex, Claude, OpenCode, or Hyprnote are installed on this Mac:

```bash
uv run brain capture agents --dry-run --home ~/brain
```

### 6. Enable Codex persistent memory access

Give Codex live access to the local Brain MCP server:

```bash
codex mcp add pkm-brain -- ~/pkm-brain/.venv/bin/brain mcp --home ~/brain
codex mcp get pkm-brain
```

Install the `brain-memory` skill so Codex knows when and how to call Brain:

```bash
mkdir -p ~/.codex/skills
cp -R ~/pkm-brain/skills/brain-memory ~/.codex/skills/
```

Restart Codex after installing the skill.

### 7. Enable Claude Code persistent memory access

Give Claude Code live access to the local Brain MCP server:

```bash
claude mcp add -s user pkm-brain -- ~/pkm-brain/.venv/bin/brain mcp --home ~/brain
claude mcp get pkm-brain
```

Install the local Brain Memory plugin:

```bash
claude plugin marketplace add ~/pkm-brain/claude-marketplace --scope user
claude plugin install pkm-brain-memory@pkm-brain-local --scope user
claude plugin list
```

Restart Claude Code after installing the plugin. If Claude Code is not logged in, start an interactive Claude Code session and run `/login`.

### 8. Install the 10-minute agent-log ingestion job

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

### 9. Install the nightly maintenance job

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

Install it with unreviewed Codex failure-memory proposals enabled:

```bash
cd ~/pkm-brain
uv run brain launch-agent install-nightly --home ~/brain --with-llm-memory-proposals --provider codex
```

The same `com.pkm-brain.nightly-maintenance` LaunchAgent is rewritten when rerun with proposal flags; no separate memory proposal LaunchAgent is created. The LaunchAgent stores the provider/model choice and the absolute Codex CLI path. With `--provider codex`, no API key is stored by pkm-brain; Codex CLI uses its own local login.

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

### 10. Verify both scheduled jobs

```bash
uv run brain launch-agent status
uv run brain launch-agent nightly-status
tail -n 40 ~/brain/logs/launchagent.out.log
tail -n 40 ~/brain/logs/nightly-maintenance.out.log
```

The capture LaunchAgent is a deterministic local Python job. The nightly LaunchAgent runs LLM semantic wiki compilation by default through the `codex` provider, so generated wiki pages keep compounding from new sources. Use `--no-llm-wiki` to disable that default. Extra proposal/memory jobs remain controlled by `--with-llm-wiki-proposals` and `--with-llm-memory-proposals`.

### 11. Uninstall scheduled jobs

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

Agent session logs are retained as the latest captured snapshot per session. When the same captured session file changes, ingest replaces the prior agent-session document, removes its old chunks/FTS/vector rows, and deletes the superseded raw copy so scheduled polling does not duplicate growing session logs.

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
- generated wiki synthesis with default LLM semantic compilation
- index status collection
- conservative LanceDB optimization when index bloat crosses maintenance thresholds
- provenance check
- wiki lint
- memory audit
- optional unapproved wiki proposals when `--with-llm-wiki-proposals` is set
- optional unreviewed failure-memory proposals when `--with-llm-memory-proposals` is set

Install the nightly macOS LaunchAgent:

```bash
uv run brain launch-agent install-nightly
```

The nightly LaunchAgent uses an hourly wake-check by default:

```text
com.pkm-brain.nightly-maintenance
StartInterval = 3600
command = brain automation nightly --if-due --due-after-hours 20 --provider codex
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

- LLM-compiled semantic pages under `~/brain/wiki/projects/`, `concepts/`, `decisions/`, `people/`, `open_loops/`, and `timelines/`
- source-backed reference pages under `~/brain/wiki/references/<source_type>/`

This follows [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): raw sources remain immutable, while the LLM maintains a persistent, interlinked Markdown wiki that compounds over time. The compiled pages are the intended human-readable wiki. They group evidence across sources, cite document IDs, and link related concepts with Obsidian-style wikilinks. Reference pages are provenance aids for inspecting a single ingested source.

Preview generated pages:

```bash
uv run brain wiki synthesize --dry-run
```

The LLM compiler asks the LLM to select the source documents for each semantic synthesis pass instead of simply taking the latest documents. It sends bounded candidate cards with a soft preference for user-supplied/manual sources, meetings, notes, transcripts, web clips, and working documents. Agent logs remain available when they are directly relevant, such as implementation history, workflow preferences, or failure patterns. Dry-run output includes `llm_compile.source_selection` with candidate counts, selected source IDs, selected/dropped counts by type, selector rationale, and selector warnings.

Create or update generated wiki pages with the default Codex-backed LLM compiler:

```bash
uv run brain wiki synthesize
```

Disable the LLM compiler for a deterministic reference/index/log pass:

```bash
uv run brain wiki synthesize --no-llm
uv run brain automation nightly --no-llm-wiki
```

Generated semantic pages are written under:

```text
~/brain/wiki/index.md
~/brain/wiki/log.md
~/brain/wiki/projects/
~/brain/wiki/concepts/
~/brain/wiki/decisions/
~/brain/wiki/people/
~/brain/wiki/open_loops/
~/brain/wiki/timelines/
```

Generated reference pages are written under:

```text
~/brain/wiki/references/<source_type>/
```

The default compiler is LLM-backed and conservative:

- routes through the local `codex` provider unless another provider is specified
- creates or updates generated semantic pages automatically when confidence is high enough
- sends low-confidence, human-edited, or otherwise risky page replacements to the wiki proposal workflow
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
  -> approved batch patches wiki sections or whole generated pages
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

LLM semantic wiki compilation is already on by default during nightly maintenance. `--with-llm-wiki-proposals` is still available as an extra proposal-only pass; if that flag is set and the provider is not configured, the nightly run fails.

Agents can also create proposals through MCP with `propose_wiki_update`.

## Reviewed Failure Memories

`AgentFailurePatternMemory` records capture durable lessons from agent failures, such as repeated tool misuse, skipped verification, or bad assumptions. They live in the same SQLite `memories` table as other typed memories.

The lifecycle is review-gated:

```text
agent or nightly LLM proposes a memory
  -> status: proposed
  -> human reviews locally
  -> status: active, rejected, or archived
```

Review commands:

```bash
uv run brain memory list --status proposed
uv run brain memory inspect <memory_id>
uv run brain memory approve <memory_id>
uv run brain memory reject <memory_id> --reason "too speculative"
uv run brain memory archive <memory_id>
```

Generate failure-memory proposals from recent agent sessions, logs, retrieval events, and existing memories:

```bash
uv run brain memory propose-from-sources --provider codex
```

Generate broader memory proposals from repeated lineage signals:

```bash
uv run brain memory propose-from-lineage --provider codex
```

Lineage-based proposals require independent evidence by default: at least three distinct agent sessions, or two sessions plus explicit useful feedback, or two sessions plus a later stable-ID re-reference. Agent-log popularity is review input, not truth. The job should propose only durable, actionable memories, and every generated memory remains `status: proposed` until a human approves it.

Enable the same synthesis during nightly maintenance:

```bash
uv run brain automation nightly --with-llm-memory-proposals --provider codex
uv run brain launch-agent install-nightly --with-llm-memory-proposals --provider codex
```

This creates only `proposed` memories. Future agents should use `active_memories` as trusted guidance and treat `candidate_memories` as unreviewed lower-priority candidates. Lineage data is advisory, rebuildable, and auditable; human memory approval remains the trust boundary.

### LLM Provider Configuration

Normal capture, ingest, search, MCP retrieval, and sync do not call an LLM. Wiki semantic compilation is LLM-backed by default, and the provider is also used by commands that generate unapproved proposal drafts, such as:

```bash
uv run brain wiki synthesize
uv run brain automation nightly
uv run brain memory propose-from-sources
uv run brain memory propose-from-lineage
uv run brain wiki propose-from-sources
uv run brain automation nightly --with-llm-wiki-proposals
uv run brain automation nightly --with-llm-memory-proposals
```

`codex` is the default provider when no provider is specified. That path shells out to the local Codex CLI. If the Codex CLI is signed in with ChatGPT, usage is handled by the ChatGPT/Codex account rather than by an `OPENAI_API_KEY`. The `openai` and `anthropic` providers call paid API endpoints directly; use them only when API billing is intended. `ollama` stays local.

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
export PKM_BRAIN_CODEX_MODEL_FALLBACKS=gpt-5.4,gpt-5.3-codex,gpt-5.2,gpt-5
export PKM_BRAIN_CODEX_BIN=/path/to/codex
export PKM_BRAIN_CODEX_CWD=~/pkm-brain
export PKM_BRAIN_CODEX_TIMEOUT_SECONDS=900
```

The Codex provider runs `codex exec` in read-only, non-interactive mode and captures the final message as JSON. It can create unapproved wiki proposal batches, but it cannot directly patch approved wiki Markdown. If the selected model is unavailable to the local Codex account, Brain tries the fallback list in order.

OpenAI-compatible:

```bash
export PKM_BRAIN_LLM_PROVIDER=openai
export OPENAI_API_KEY=...
export PKM_BRAIN_OPENAI_MODEL=gpt-5.5
# Optional:
export PKM_BRAIN_OPENAI_MODEL_FALLBACKS=gpt-5.4,gpt-5.4-mini,gpt-5
export PKM_BRAIN_OPENAI_BASE_URL=https://api.openai.com/v1
```

The OpenAI provider uses `OPENAI_API_KEY` and is billed through the OpenAI API platform. ChatGPT Plus/Pro subscription usage does not cover these calls.

Anthropic:

```bash
export PKM_BRAIN_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=...
export PKM_BRAIN_ANTHROPIC_MODEL=claude-sonnet-4-5
# Optional:
export PKM_BRAIN_ANTHROPIC_MODEL_FALLBACKS=<alternate-model-1>,<alternate-model-2>
export PKM_BRAIN_ANTHROPIC_BASE_URL=https://api.anthropic.com
```

Ollama:

```bash
export PKM_BRAIN_LLM_PROVIDER=ollama
export PKM_BRAIN_OLLAMA_MODEL=llama3.1
# Optional:
export PKM_BRAIN_OLLAMA_MODEL_FALLBACKS=<alternate-local-model>
export PKM_BRAIN_OLLAMA_BASE_URL=http://localhost:11434
```

## Minimal Local Smoke Test

```bash
uv sync
uv run brain init
uv run brain ingest
uv run brain search "sqlite metadata" --debug
uv run brain mcp
```

Runtime data is stored outside the repo in `~/brain` by default.

## Index Maintenance

SQLite stores canonical document and chunk metadata. LanceDB under `~/brain/indexes/lancedb` is a rebuildable vector index and can accumulate old table versions during continuous capture, ingest, and sync.

Inspect index health:

```bash
uv run brain index doctor --home ~/brain
```

Prune old LanceDB versions without touching SQLite, raw files, wiki pages, or memories:

```bash
uv run brain index optimize --home ~/brain
```

The default cleanup window is conservative for scheduled maintenance. For a one-time manual cleanup when no long-running Brain readers are active, use:

```bash
uv run brain index optimize --home ~/brain --cleanup-older-than-days 0
```

If the vector index becomes inconsistent with SQLite, rebuild it from canonical chunks:

```bash
uv run brain index rebuild-vectors --home ~/brain
```

`rebuild-vectors` keeps the previous LanceDB directory as a timestamped backup unless `--delete-backup` is passed after successful verification.

If older agent-session logs contain oversized chunks, preview and then regenerate bounded overlapping chunks from the preserved raw files:

```bash
uv run brain db reindex-chunks --home ~/brain --source-type agent_session_log --dry-run
uv run brain db reindex-chunks --home ~/brain --source-type agent_session_log
```

Reindexing rewrites SQLite chunks, FTS rows, and LanceDB vectors for affected documents only. It does not delete raw files, wiki pages, memories, or source evidence.

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
- Codex `brain-memory` skill for persistent-memory activation through MCP
- Claude Code `pkm-brain-memory` plugin for persistent-memory activation through MCP
- reviewed memory proposal, audit, approve, reject, and archive commands
- `AgentFailurePatternMemory` proposal synthesis for agent failure-learning loops
- ingestion run logs
- Codex, Claude, OpenCode, and Hyprnote capture
- macOS LaunchAgent scheduled polling
- hourly due-check nightly maintenance LaunchAgent
- local Web UI with token-authenticated JSON API for status, setup, sync, jobs, logs, and memory review
- Primary/Secondary sync setup, scheduler commands, transport, and acceptance preflight
- MCP server wrapper

Not yet implemented:

- model-independent autonomous memory activation outside the Codex skill
- query expansion
- local reranking
- cloud embedding providers
- general-purpose background filesystem watcher
- packaged desktop GUI or Obsidian plugin

## License

PKM Brain is released under the MIT License. See [LICENSE](LICENSE) for the full license text.

## Contributing And Security

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, testing, and privacy expectations.

For vulnerability reports or data-exposure concerns, use the guidance in [SECURITY.md](SECURITY.md).

## Development

Run tests and lint:

```bash
uv run pytest
uv run ruff check .
```

The project intentionally keeps private knowledge artifacts out of git. Do not commit `~/brain`, SQLite databases, LanceDB indexes, logs, or secrets.
