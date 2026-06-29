---
name: brain-memory
description: "Use when a task may need local PKM Brain memory: prior context, decisions, preferences, wiki pages, meeting transcripts, docs, or agent history."
---

# Brain Memory

## Purpose

Use the local PKM Brain as a persistent memory layer. Brain is local-first: raw sources are evidence, wiki pages are synthesized knowledge, memories are curated claims, and indexes are rebuildable retrieval artifacts.

The skill is an activation policy. The actual memory access should happen through the `pkm-brain` MCP tools. Do not read Brain's SQLite database or LanceDB index directly unless the user explicitly asks for low-level debugging.

## When To Query Brain

Query Brain early when the task involves:

- prior discussions, meeting notes, transcripts, or working documents
- user preferences, durable decisions, project context, or open loops
- business, customer, product, or research context that may have been discussed before
- ambiguous instructions where personal context could change the answer
- agent-session history, previous implementation choices, or repo-specific memory

Skip Brain for trivial commands, purely mechanical local edits, or questions fully answered by the current repository context.

## Primary Workflow

1. Use MCP `retrieve_context` first.
   - `task`: restate the user task as a retrieval-oriented question.
   - `project`: include it when the project is clear.

2. Read the returned context as evidence, not as guaranteed truth.
   - Treat `active_memories` as reviewed operational guidance.
   - Treat `candidate_memories` as proposed, unreviewed hypotheses. Never treat them as authoritative.
   - Prefer higher-scoring selected chunks and wiki pages.
   - Use `selection_reasons` to understand why each chunk was returned.
   - Use `raw_context` pointers when the full underlying source artifact matters.

3. If the first result is too narrow, use MCP `search_knowledge`.
   - Use precise keywords, names, project terms, and quoted phrases.
   - Prefer this for known entities or exact terms.

4. If the task is about durable preferences or facts, use MCP `get_memories`.
   - Filter by `scope` or `memory_type` when known.
   - Prefer the default active/reviewed status. If you intentionally request proposed memories, treat them as candidates only.
   - Treat active memories as curated claims that still benefit from source evidence when the stakes are high.

5. Cite memory provenance in your reasoning when it affects the answer.
   - Mention document IDs, chunk IDs, wiki page paths, or raw source paths when useful.
   - Do not imply that Brain found evidence when it did not.

## Writing Back

Do not silently create approved memories or modify wiki pages. Agents may propose memories, including `AgentFailurePatternMemory`, but must not approve, reject, archive, or otherwise activate them.

Use MCP `propose_memory` only when:

- the memory is durable beyond the current task
- the claim is specific and useful for future agents
- there is source evidence or clear user confirmation
- failure-pattern memories are actionable lessons from concrete agent failures, not generic advice

Do not use legacy wiki proposal tools. The current MCP surface does not expose wiki mutation; report source-backed wiki-change recommendations to the user instead.

Use MCP `write_agent_session` at the end of substantial work when the session produced reusable context, touched important files, made decisions, or left open issues.

## CLI Fallback

If MCP is unavailable, use the local CLI from the pkm-brain repo:

```bash
uv run brain retrieve-context --task "<task>" --mode default --home ~/brain
```

For debugging noisy retrieval:

```bash
uv run brain retrieve-context --task "<task>" --mode compact --debug --home ~/brain
```

For an explicit broad survey:

```bash
uv run brain retrieve-context --task "<task>" --mode broad --home ~/brain
```

For direct search:

```bash
uv run brain search "<query>" --debug --home ~/brain
```

Prefer MCP when available because it gives Codex structured tool results without shell parsing.
