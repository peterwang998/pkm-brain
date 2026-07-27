# Agent lookup resilience — 2026-07-27

## Trigger

Luna's Sierra lookup succeeded only after several avoidable failures:

- invalid temporal retrieval fields were rejected without a useful local
  contract;
- `search_mail` accepted a requested limit that the daemon later rejected;
- retrieval evidence was found, but its ancillary telemetry write encountered
  a SQLite lock;
- a fallback Gmail search used a different account scope;
- `write_agent_session` returned HTTP 500.

The originating report is
`/Users/Peter/Documents/Codex/2026-07-27/base/outputs/brain-lookup-improvement-report.md`.

## Confirmed root cause

The two HTTP 500 responses were SQLite writer starvation, not a failed daemon or
failed evidence lookup.

- read-only Brain calls completed while the incident was active;
- a separate `BEGIN IMMEDIATE` failed with `database is locked`;
- the isolated `capture_tick` worker had held the writer for more than an hour
  at high CPU;
- the SQLite WAL grew to roughly 2.2 GB;
- the run scanned 9,465 sources even though only two documents changed.

`BrainService.ingest` held one SQLite write transaction while it:

1. scanned every source;
2. rewrote document and FTS metadata even for stat-identical sources;
3. chunked changed sources; and
4. generated and wrote vector embeddings.

Retrieval itself remained readable under WAL. It failed only after selecting
evidence, when `search` or `retrieve_context` attempted to record
`retrieval_events` and exposure lineage through a new writer. Session capture
failed for the same reason.

## Remediation

### Ingestion availability

- Stat-identical documents now bypass metadata and FTS writes when their source
  identity is unchanged.
- Large source scans commit in bounded batches so they yield SQLite's single
  writer.
- Vector deletion and embedding run after the document/chunk transaction has
  committed.
- The ingestion run is finalized in a short second transaction.

### Retrieval availability

- Retrieval telemetry uses a 50 ms best-effort writer.
- Only `SQLITE_BUSY` and `SQLITE_LOCKED` fail open.
- Evidence returns normally with a structured `evidence_unaffected` warning and
  a null retrieval event ID.
- The event and its exposure lineage remain atomic; a lock cannot leave partial
  telemetry.

### MCP contract and errors

- `search_mail.limit` publishes its `1..5` bound and intended expansion flow.
- `event_kind` publishes the `actual | planned` enum.
- `temporal_mode` publishes the supported temporal views.
- Invalid enum and bound values fail during MCP argument validation, before an
  HTTP request.
- Cross-field temporal errors preserve the daemon's actionable message instead
  of surfacing a generic HTTP 400.

### Gmail scope and credentials

- Mail responses identify their configured local archive account through
  `source_scope`.
- Agent sessions sanitize credentials before persistence.
- Credential coverage now includes full temporary-password lines and common
  Slack, GitHub, Stripe, Google, and AWS token forms.
- Gmail output continues to be sanitized before truncation and recursively at
  the final model boundary.

## Regression coverage

The focused suite exercises:

- evidence retrieval under a real held SQLite writer lock;
- atomic fail-open telemetry with no partial lineage;
- acquiring a new SQLite writer while embedding is running;
- a truly no-op second ingest for unchanged files;
- published MCP bounds and enums;
- actionable daemon validation errors;
- local Gmail account scope;
- credential sanitization at the agent-session persistence boundary; and
- expanded provider-token and temporary-password masking.

The release is version `0.2.4`, app build `10`.
