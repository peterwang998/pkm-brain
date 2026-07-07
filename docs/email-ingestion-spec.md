# Email Ingestion & Telemetry Retention — Spec

**Status:** Phase 0 implemented; Phases 1-3 remain planned
**Last verified:** 2026-07-07 after Phase 0 implementation; live-brain dry-run/optimization completed
**Goal:** make email a first-class evidence source (searchable within a day of arrival) without letting it degrade fact quality, flood the human-review queue, pollute the entity graph, or blow up storage — and fix the telemetry-growth problem that email would otherwise accelerate. Evidence first, facts narrowly, telemetry bounded.

Companion decisions: `wiki/decisions/pkm-brain-retrieval-provenance-reset.md` (retrieval history is rebuildable; snapshots make pruning safe), `docs/entity-layer-spec.md` (mention gating, recurrence promotion), `docs/extraction-payload-spec.md` (claim-class gate).

---

## 1. Why — measured evidence (2026-07-06)

The live brain's growth problem today is telemetry, not knowledge:

- `brain.sqlite` = 842 MB, of which actual content is ~150 MB (chunks 32 MB text, facts ~1 MB statements, raw sits outside at 106 MB).
- `retrieval_events` = **334 MB (40% of the DB)** from ~2,947 events — ≈113 KB/event, dominated by `citation_snapshots` (83 MB of frozen chunk text, 2.6× the entire chunk corpus). No retention policy exists.
- FTS amplification: `chunk_fts_*` ≈ 197 MB + `retrieval_fts_*` ≈ 103 MB for a 32 MB corpus (~6× observed; likely compactable toward ~2× with an FTS `optimize` merge).
- LanceDB = 491 MB for ~3.4k vectors (~5 MB of live data) — version churn idling just under the 512 MB auto-optimize threshold.
- `~/brain/db/` also holds ~2.9 GB of stale May `.bak.gz` files superseded by `brain-runtime-backups/`.

Pipeline constants from the rebuild: ~13 facts/doc on meetings; critic ≈ 18s/fact (÷4 with parallel workers); unrouted+conflict residue ran 12–17% of accepted facts on *curated* transcripts.

Email projections (30k emails/yr, quote-stripped thread-canonical ≈ 10 KB avg): ~300 MB/yr raw text → worst-case ~2 GB/yr through chunks+FTS+vectors (≤1 GB with FTS compaction). Linear and cheap. The non-linear risks are (a) telemetry per query growing with corpus size, (b) extraction volume: full-corpus email extraction breaks both the ~20-minute nightly budget and the review queue, so the aperture must be policy-controlled, and (c) entity/one-off-correspondent pollution.

---

## 2. Decisions

### D1 — Telemetry retention is a precondition, not part of email
Strip heavy payloads (`citation_snapshots`, `debug`) from `retrieval_events` rows older than a configurable window (default **90 days**), keeping the row itself (`id`, `query`, `timestamp`, `caller`, `returned_chunk_ids`, `selected_chunk_ids`) so eval history and `context_lineage_events` joins keep working. This deletes no documents, no chunks, no vectors, no facts — only the frozen copies of what old queries returned. Sanctioned by the provenance-reset decision. Ship with `--dry-run`, run in nightly maintenance.

### D2 — Local-first capture source: Maildir/mbox, no OAuth in pkm-brain
The adapter reads a local Maildir (or mbox) directory kept in sync by an external tool (mbsync/imapsync/offlineimap — operator's choice, documented not implemented). pkm-brain stays deterministic and offline-capable; a Gmail-API fetcher is deferred (Phase 4). The upstream mailbox remains the true source of record, which is what makes aggressive cleaning at capture safe: `raw/` stores the adapter-rendered artifact, same as agent-session snapshots.

### D3 — One canonical document per thread, snapshot-replaced
Per-message docs would store the same quoted paragraphs N times across an N-message thread — bloating storage *and* re-feeding identical content into extraction windows (duplicate-fact pressure). Instead: group messages by thread (`X-GM-THRID` when present, else `References`/`In-Reply-To` chain + normalized subject), render one chronological Markdown doc (minimal `From/Date` header per message + cleaned body), rebuilt from the individual messages (never trusting reply-quoting, which is lossy). `logical_source_key` = thread key; re-ingest replaces the prior snapshot exactly like `agent_session_log` docs (generalize `remove_superseded_agent_session_snapshots` to be source-type-driven).

### D4 — Classify bulk vs. human at capture, deterministically
Headers decide: `List-Unsubscribe`, `Precedence: bulk/list`, `Auto-Submitted`, noreply-pattern senders → `source_type: email_bulk`; everything else → `email_thread`. Both are captured and searchable; they differ in retrieval weight and extraction eligibility. Misclassification is low-stakes (weight only). Weights join the existing map in `service.py` (`meeting_transcript: 2.0 … agent_session_log: -5.0`): `email_thread: 1.0`, `email_bulk: -4.0`.

### D5 — Redaction at capture is a security requirement
`raw/` is immutable, so anything captured lives forever. Before the inbox file is written, redact: OTP/verification codes (contextual numeric patterns), URLs carrying auth/reset/unsubscribe tokens (strip query strings on token-bearing params or drop the URL, keep domain), and conservative account-number patterns. Same posture as the existing agent-log sanitization (`sanitize_agent_session_log`, `chunking.py`). Ship with a redaction test corpus; this cannot be retrofitted.

### D6 — Attachments: text never binaries
Record filename/type/size as a metadata line in the thread doc. Never copy attachment binaries into `raw/`. Attachment text extraction (PDF etc.) is deferred.

### D7 — Extraction default-off; opt-in by signal, not by corpus
`cos_llm.yaml` extraction source policy: `email_bulk: extract never`; `email_thread: extract signal` — a new policy mode where only docs carrying opt-in signals are eligible: **user replied in the thread**, **starred/flagged**, or **sender allowlist**. The adapter stamps these as document metadata at capture. Everything else remains chunk-searchable evidence only — the `agent_session_log` precedent. The ~20-minute nightly budget enforces the same aperture (~10–20 threads/day max); add per-run doc/window caps so the budget is config, not luck.

### D8 — Ephemeral claims never become facts
Email is logistics-heavy ("can we move the call to Tuesday", "flight AA123 departs 6am") — quote-backed, entailed, critic-passable, and worthless in a month. The brain has no time-based fact lifecycle, so gate at extraction: add `logistics_ephemeral` to the claim-class enum, route it to `NON_CLAIM_CLASSES` (`extraction.py:115/124`) so it is dropped at validation, and report the drop count per run. Auto-expiry via `effective_at`/`page_contracts.freshness_policy` is deferred (Phase 4); dropping is the safe v1, consistent with under-create bias — raw is durable, anything dropped is re-derivable.

### D9 — Entity link-only for email sources
Email mints named entities relentlessly (every recruiter, vendor, correspondent). For email-sourced facts, `resolve_entity` runs in **link-only** mode: exact/alias matches link to existing entities; unresolved named mentions do *not* create entity rows — they are recorded in fact metadata as candidate mentions. A recurrence-promotion pass (mention across ≥N distinct threads → propose entity creation through the gardener) is Phase 4; the entity spec already sketches this pattern for concepts. Config: `entity.create_for_source_types` with email excluded by default.

### D10 — Residue caps per run
The human queue must not scale with mail volume. Per-run cap on new `needs_human` items originating from email extraction (default **10**); overflow becomes auditable auto-reject with rationale (re-derivable, same posture as critic reject-mode). Cap and counts surfaced in the run summary; a sustained-overflow signal means the aperture (D7) is too wide — tighten config, don't raise the cap.

---

## 3. Build plan

### Phase 0 — Telemetry retention + storage housekeeping (independent; implemented 2026-07-07)
- `brain db compact-retrieval-events --older-than-days 90 --dry-run/--apply` implementing D1; wire into nightly maintenance alongside index checks. Apply the same idea to `automation_runs` (strip `summary` bodies older than ~180 days, keep status/error).
- FTS compaction: expose `brain index optimize --fts` issuing FTS5 `optimize` merges for `chunk_fts` and `retrieval_fts`; run once manually, then threshold-gated in maintenance.
- LanceDB: one forced `index optimize --cleanup-older-than-days 0` pass; consider lowering `retained_bytes_threshold` (currently 512 MB) to 256 MB.
- Stale `~/brain/db/*.bak.gz` (~2.9 GB, May vintage): **flag for human deletion — do not auto-delete.** They are superseded by `brain-runtime-backups/`.

*Acceptance:* dry-run reports bytes reclaimable; after apply, `retrieval_events` ≤ ~10% of DB; eval bundle + provenance check still green; lineage boosts unaffected (test); DB footprint roughly halves before any email lands.

### Phase 1 — Capture adapter, evidence-only (extract: false everywhere)
- `EmailCapture` following the `AgentLogAdapter` protocol in `capture.py`: Maildir/mbox reader (path from config), incremental via `capture_sources` hashes, thread grouping + canonical doc rendering (D3), per-message cleaning (HTML→text, quote-strip "On … wrote:" chains, signature/disclaimer heuristics, tracking-URL stripping), redaction (D5), header classification (D4), attachment metadata lines (D6), opt-in signal stamping (replied/flagged/allowlist) as doc metadata for D7.
- Ingest integration: `detect_source_type` additions; source-type-driven snapshot replacement; source weights + a `clean_context_text` branch for email types; `cos_llm.yaml` defaults `email_thread`/`email_bulk` → `extract: false`.
- `brain capture email --dry-run` preview like `capture agents`.

*Acceptance:* a synced mailbox becomes searchable thread docs; a growing thread re-ingests as one replaced doc (no duplicates); the redaction corpus passes (OTP codes, reset URLs, account numbers never reach `inbox/`); bulk mail ranks below human threads for a general query; storage growth matches the §1 model (spot-check after ~1 week of capture).

### Phase 2 — Retrieval + eval integration
- Add email-era golden fixtures (2–3 known-topic cases expecting thread docs) and 2 email-flavored negative controls to `retrieval_fixtures.py`; watch `noise_rate` — a larger lexical surface must not degrade verdict calibration or negative controls.

*Acceptance:* full eval bundle green with the email corpus ingested; negative-control pass stays 1.0.

### Phase 3 — Narrow extraction opt-in (shadow first, then policy — existing CoS discipline)
- Implement `extract: signal` mode (D7) + per-run caps; `logistics_ephemeral` claim class (D8) with prompt line, validation routing, and report counts; entity link-only mode (D9); residue cap (D10).
- Shadow run on a week of opted-in threads; inspect route quality, ephemeral-drop counts, zero entity creations; then enable under policy v4+ (critic reject-mode, sampled audit) — no new autonomy level needed.

*Acceptance:* nightly with the email stage completes ≤20 min; email-sourced facts pass sampled audit at parity with meeting facts; new residue/run ≤ cap; `SELECT COUNT(*) FROM entities` unchanged by email runs; ephemeral claims appear only as drop counts, never as facts.

### Phase 4 — Deferred
Recurrence-based entity promotion via the gardener; Gmail-API/IMAP fetcher inside pkm-brain; date-based fact auto-expiry (`freshness_policy`); attachment text extraction; per-sender trust learning.

---

## 4. Caveats
- Quote-strip and signature heuristics will lose fragments of legitimate text; acceptable because the upstream mailbox remains the source of record (D2) and capture is re-runnable.
- Thread grouping by subject fallback can merge distinct topics under "Re: quick question"; prefer header-chain grouping, accept imperfection — it affects doc granularity, not correctness.
- Bulk classification by headers is imperfect in both directions; consequences are weight-only (both types remain searchable).
- Email increases query lexical surface; if `noise_rate` trends up post-Phase-1, tune `email_bulk` weight before touching packet budgets.
- Cloud extraction over email bodies is a sensitivity step up from meeting transcripts (third-party words, financial/legal threads). The per-role provider config supports pointing the extractor at a local model for email-only runs; decide posture explicitly at Phase 3, not by drift.

## 5. Non-goals
- No OAuth/IMAP client inside pkm-brain v1; no sending email; no full-corpus email extraction ever by default; no attachment binaries in `raw/`; no per-message documents; no new autonomy levels.

## 6. Code touchpoints
`capture.py` (`EmailCapture`, cleaning/redaction/classification, signal stamping) · `service.py` (source weights map, `detect_source_type`, source-type-driven snapshot replacement, `clean_context_text` branch, `compact-retrieval-events` service path) · `chunking.py` (email text prep if needed) · `extraction.py` (`extract: signal` policy mode, `logistics_ephemeral` in claim classes, per-run caps, residue cap) · `entities.py` (link-only mode, `create_for_source_types`) · `indexes.py`/`cli.py` (`index optimize --fts`, `db compact-retrieval-events`, `capture email`) · `automation.py` (retention + email stages in nightly) · `retrieval_fixtures.py` (email fixtures + negative controls) · `llm.py`/`cos_llm.yaml` (email source-type policy defaults) · tests: redaction corpus, thread replacement, retention/lineage preservation, signal gating, link-only entities, residue caps.

## 7. Verification bundle
```bash
uv run ruff check .
uv run pytest -q
uv run brain db compact-retrieval-events --dry-run --home ~/brain   # Phase 0 gate
uv run brain capture email --dry-run --home ~/brain                 # Phase 1 gate
uv run brain eval run --home ~/brain                                # bundle green at every phase
uv run brain provenance check --home ~/brain
# Phase 3: shadow report showing route quality, ephemeral drops, zero entity creations, residue ≤ cap
```
