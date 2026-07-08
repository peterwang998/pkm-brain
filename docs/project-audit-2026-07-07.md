# PKM Brain — Full Project Audit & Improvement Task List

**Status:** audit + prioritized task list for Codex
**Last verified:** 2026-07-07 against commit `c736f11` plus the dirty working tree (uncommitted UI v2 work)
**Author:** Claude (full repo + runtime audit)

Scope: spec drift, bloat reduction, and improvements toward the project's stated goals — a local-first second brain that stores messy personal knowledge, makes it searchable, and exposes *useful, bounded* context to agents over MCP.

Baseline health at audit time: `uv run pytest -q` → **352 passed in 23s**; `uv run ruff check .` → clean; `brain doctor` healthy (sentence-transformer live); CI (ruff + pytest) green-shaped.

How to read this: items are ordered P0 → P3. Each has evidence, an action, and acceptance criteria. Items marked **[needs Peter]** involve deletion/policy decisions — build the tooling and report, do not delete silently.

---

## P0 — Broken or actively wasteful now

### 1. MCP context packets are so large they defeat their own purpose
**Evidence:** A live `retrieve_context` call (task about pkm-brain priorities) returned **75,543 chars**; `get_project_context("pkm-brain")` returned **61,583 chars** — both overflowed the calling agent's tool-result limit and were dumped to files instead of being usable context. The packet self-reports `budget: 8000` (tokens), but the budget only counts chunk text. Measured composition of the 61KB packet: `supporting_chunks` 19.6KB (4 chunks), `citation_snapshots` 14.2KB, `citations` **another identical 14.2KB** (see item 2), `relevant_wiki_pages` 6.7KB (8 pages with summaries, source_id lists, selection_reasons, matched-term arrays).
**Why it matters:** the entire point of `retrieve_context` (spec v0.1 §10: "bounded for usefulness"; README: "bounded retrieval so noisy sources do not consume the whole agent context") is defeated when the default packet is ~15–19k tokens of JSON.
**Action:**
- Count the *serialized packet*, not just chunk text, against the mode budget; shrink selection until the packet fits (chunks first, then wiki pages, then metadata).
- In non-debug mode, drop or slim per-item diagnostic metadata: `selection_reasons`, `matched_query_terms`, `matched_specific_query_terms`, full `source_ids` arrays on wiki pages (cap at ~3), `retrieval_reasons`.
- Cap `citation_snapshots` (count and per-snapshot text length) in non-debug mode, or return snapshot references (ids + offsets) instead of frozen text — the raw text is already in `supporting_chunks`.
**Acceptance:** default-mode `retrieve_context` JSON ≤ ~32KB serialized (≈8k tokens) on the live brain; a regression test asserts serialized size for a seeded workspace; debug mode retains full diagnostics.

### 2. `citations` is a byte-identical duplicate of `citation_snapshots` in every packet
**Evidence:** `service.py:1467` and `service.py:1569` both emit `"citations": citation_snapshots` as a "back-compat alias" (spec v0.1 §10 documents it). Measured: 14,173 chars each in one live packet — ~23% of the payload is pure duplication. **No consumer of the alias exists anywhere** — grep of `src/`, `tests/`, `skills/`, `claude-marketplace/` finds zero readers of `citations` distinct from `citation_snapshots`.
**Action:** remove the alias from both emit sites; update spec v0.1 §10 (delete the alias line) and any doc that mentions it.
**Acceptance:** no `"citations"` key in packets; tests updated; grep confirms no reader breaks.

### 3. Nightly maintenance silently failed for ~12 days — add failure visibility and blast-radius control
**Evidence (live runtime):** `automation_runs` shows success 2026-06-24, then failures until success 2026-07-06 20:34. Causes found: (a) **214 occurrences** of `No such option: --with-llm-wiki-proposals` in `nightly-maintenance.err.log` — a stale flag in a previously installed plist kept the job dead for days; (b) `mem_586f71a0a6564cc2: invalid scope pkm-brain` — a single malformed memory made `memory audit` fail the **entire** nightly run (twice); (c) `codex executable was not found`. Commit `7cfa67e` fixed the scope-validation cause but not the blast radius.
**Action:**
- `brain doctor` reports age of last successful nightly run and last failure reason; warn if > `due_after_hours` + slack.
- `brain launch-agent nightly-status` (and `status`) validates the installed plist's arguments against the current CLI's accepted options and warns on unknown flags (this exact failure mode recurs whenever a flag is renamed).
- Memory-audit schema issues on individual memories should degrade to warnings in the nightly summary (per spec §9.1: "Warnings should be recorded but should not fail the job by default"); reserve run-level failure for stage crashes, not one bad row.
**Acceptance:** a plist with a stale flag is detected by `nightly-status`; a memory with an invalid scope no longer fails the nightly run; `doctor` surfaces "last nightly success: Xh ago".

### 4. Land the in-flight UI v2 working tree (or split it into committable phases)
**Evidence:** uncommitted diff of **+2,528/−1,226 across `ui_server.py` (3,520 changed lines)**, new untracked `src/pkm_brain/ui_static/` (full scaffold + all six view modules), new `docs/brain-ui-v2-spec.md`, updated `tests/test_ui_auth.py`/`test_ui_endpoints.py`. `ui_shell()` is already deleted in the working tree even though the spec's build plan (§6) keeps it until P5. House convention (`docs/archive/cos-determinism-and-doc-conventions.md` §0) explicitly forbids leaving docs describing behavior that exists only in an uncommitted tree.
**Action:** verify each phase's acceptance criteria in `brain-ui-v2-spec.md` §6, run the verification bundle in §9, commit in logical chunks (server/static/queue/wiki-entities/ops), update the spec's Status line to reflect what shipped, and re-stamp per doc conventions. If P5 acceptance (v1 feature-parity checklist) is not actually met, restore the `/legacy` route until it is.
**Acceptance:** clean `git status`; spec status reflects reality; `pytest` + `ruff` green; v1 parity checklist result recorded in the spec.

### 5. Live DB is still ~40% telemetry; automation summaries are huge
**Evidence (live `~/brain/db/brain.sqlite`, 813MB):** `retrieval_events` **328MB** (3,032 events, ~113KB each — dominated by frozen `citation_snapshots` text, 2.6× the whole chunk corpus per `email-ingestion-spec.md` §1); `automation_runs` **41MB** for 662 runs (~62KB/run despite the error-capping work); FTS shadow tables ~192MB for a ~34MB chunk corpus. Phase 0 of the email spec shipped `brain db compact-retrieval-events`, but the live DB has not been compacted/VACUUMed.
**Action:**
- Run the Phase 0 retention/compaction against live + `VACUUM` **[needs Peter]** (backup first via `brain cos backup-runtime`).
- Stop freezing full snapshot text into `retrieval_events` going forward (store references + hashes; keep text only for explicit-feedback events) — align with item 1's snapshot slimming.
- Add `automation_runs` retention (e.g. keep 90 days / last N per job) and cap the `summary` JSON size; add both to the nightly maintenance stage list.
- Consider an FTS `optimize` merge pass in index maintenance (email spec estimates ~6× → ~2×).
**Acceptance:** live DB below ~350MB after compaction; nightly includes telemetry retention; a size-shaped test guards `automation_runs` summary caps.

---

## P1 — Spec drift to reconcile (fix code or fix docs; each item says which)

### 6. MCP `retrieve_context` exposes `budget`/`mode`/`debug` — docs say task/project only, by design
**Evidence:** `mcp_server.py` `retrieve_context(task, project, budget, mode, debug)`. README ("The MCP tool intentionally keeps a simple `task`/`project` surface and uses the default bounded mode") and spec v0.1 §15/§21.4 ("Per-call `budget` and `mode` knobs are CLI-only by design to keep MCP payloads compact") both contradict the code.
**Action (recommended):** fix the **code** — remove `budget`, `mode`, `debug` from the MCP tool signature. The documented rationale is sound and directly supports item 1 (an agent passing `debug=True` or `mode=broad` makes the oversize problem worse). The bundled skill never uses these knobs.
**Acceptance:** MCP tool surface matches spec §15; skill docs unchanged; CLI keeps all knobs.

### 7. Spec v0.1 §21 "Implementation Status" is itself stale
**Evidence:** §21.3 claims "no `brain eval` runner" — false: `brain eval run` exists with five suites (extraction, routing, topology, conflict, retrieval) in `evals.py`, including retrieval golden cases with source-hit/negative-control metrics. §9.1 claims "Nightly does not currently run a wiki synthesis command" — false: `automation.py:256` runs `cos_synthesis` when LLM wiki synthesis is enabled, and the README documents it. §21.2's memory-audit gaps (no duplicate/staleness/conflict detection) are still accurate (`audit.py:34` checks only type/status/scope/source_ids-presence/confidence).
**Action:** re-audit §21 line by line against current code, or (cheaper) collapse §21 to a short pointer: "current status lives in `docs/architecture-code-guide.md`"; fix the §9.1 nightly task list to include the synthesis/timeout/audit stages it already lists elsewhere.
**Acceptance:** no §21 claim contradicts the code; §9.1 stage list matches `automation.py`.

### 8. `golden_queries.yaml` is a dead artifact; retrieval goldens are hardcoded in the package
**Evidence:** `service.py:278` creates `~/brain/evals/golden_queries.yaml` at init; the setup wizard lists it; **nothing ever reads it**. The retrieval eval instead uses `RETRIEVAL_GOLDEN_CASES` hardcoded in `retrieval_fixtures.py` (687 lines of fixture data compiled into the package). Spec §20 promises a user-local, corpus-specific golden set.
**Action:** wire user-local cases in: `brain eval run --suite retrieval` merges cases from `~/brain/evals/golden_queries.yaml` (schema per spec §20) with the built-in fixtures, reporting them separately. Move `RETRIEVAL_GOLDEN_CASES` (and other large fixture blobs) out of `.py` into data files loaded at runtime (`importlib.resources`), trimming `retrieval_fixtures.py` to loader code.
**Acceptance:** adding a case to the YAML changes eval output; `retrieval_fixtures.py` < ~150 lines; spec §20 matches behavior.

### 9. Architecture guide has internal contradictions — re-verify and re-stamp
**Evidence:** header says "Last verified 2026-07-03 against `604e3f1`" but the file has uncommitted edits and HEAD has moved 15+ commits. §11 says `retrieve_context()` "currently returns an empty `open_questions` list" while the Major TODOs say (correctly — `service.py:1504`) that it now returns query-relevant residue. §16/§24 describe the synthesizer as an active nightly producer while the closing "Deterministic vs LLM" summary says "this code scan did not find an active producer for synthesis Markdown." The Major TODO on embeddings ("Remaining work is the Phase 5 side-by-side retrieval eval…") is stale — `embeddings-productization-spec.md` records Phase 5 complete and the live flip done 2026-07-05.
**Action:** one re-verification pass over the guide against current HEAD (after item 4 lands); fix the three stale statements; re-stamp. This satisfies open task **T0.2** in `cos-regeneration-tasklist.md`.
**Acceptance:** guide stamped against a real commit; no internal contradictions; TODO list reflects only genuinely open work.

### 10. Reconcile `cos-regeneration-tasklist.md` checkboxes with post-rebuild reality
**Evidence:** T0.2 (doc stamping), T1.3 (resolver-judgment validation), T4.1 (full scoped regeneration) remain unchecked, yet commit `fb128a7` ("Close out rebuild gates and reclaim unrouted facts") and the live-rebuild session of 2026-07-06 (378 entities, 1,239 active facts live) indicate the rebuild ran. A stale task list is drift in the project's primary Codex coordination doc.
**Action:** update each checkbox with done/not-done + evidence links (eval ids, run dirs); mark the doc's overall status (executed / partially executed).
**Acceptance:** no checkbox contradicts the ledger/eval record.

### 11. Section 14 "Forgetting And Redaction" is entirely unimplemented — decide, don't drift
**Evidence:** no `forget_events` table (confirmed absent from live schema), no `brain forget` commands, no tombstone guard. Yet the system auto-captures agent sessions every 10 minutes and the roadmap adds email. This is the largest gap between stated goals ("The user must be able to remove personal material… removal must propagate predictably") and reality — it is privacy-relevant, not cosmetic.
**Action [needs Peter — scope decision]:** either (a) implement a minimal slice — `brain forget source|session` + content-hash tombstone + propagation to chunks/FTS/vectors/capture_sources + `forget_events` log (spec §14 steps 1–5, 10–11; defer pattern/range/undo), or (b) explicitly re-scope §14 as deferred with a dated note. Recommend (a) before email ingestion Phase 1 lands.
**Acceptance:** either the minimal commands exist with tests, or the spec says why not and when.

### 12. README no longer describes the actual system
**Evidence:** README (857 lines) has **zero occurrences** of "entity"/"entities", `brain cos`, or `brain eval` — the entity layer, action ledger, policy engine, critic gate, and eval harness are the core of the current architecture and the bulk of recent work. Meanwhile ~200 lines are duplicated: Quickstart ≡ "Minimal Local Smoke Test"; the Codex/Claude MCP + skill setup appears twice verbatim (standalone sections and again as "Install On A New Mac" steps 6–7).
**Action:** restructure — short top (what it is, the core rule, quickstart, current architecture sketch incl. facts/actions/entities/evals), one install path (move "Install On A New Mac" to `docs/install.md`), one agent-setup section, pointers to canonical docs. Add a short "Chief-of-Staff autonomous curation" section reflecting `chief-of-staff-spec.md`.
**Acceptance:** README ≤ ~400 lines, no duplicated blocks, mentions the entity/action/eval layers, all commands shown exist.

### 13. Two divergent copies of the `brain-memory` skill
**Evidence:** `skills/brain-memory/SKILL.md` (Codex copy, has `agents/openai.yaml`) vs `claude-marketplace/plugins/pkm-brain-memory/skills/brain-memory/SKILL.md` (Claude copy) already differ in three places (version field, "The/This skill", agent name). The activation policy — the most behavior-shaping doc for agents — has no single source of truth.
**Action:** make one canonical source and generate/copy the other at build time, or add a test asserting the bodies match modulo an allowed substitution list (agent name, version line).
**Acceptance:** CI fails if the copies drift beyond the allowed substitutions.

---

## P2 — Bloat reduction and hygiene

### 14. Split the monolith modules (mechanical, after item 4 lands)
**Evidence:** `service.py` 4,007 lines (workspace init, ingest, mirror rebuild, reindex, retrieval pipeline, memory lifecycle, feedback/lineage, doctor…), `extraction.py` 3,789, `wiki_facts.py` 3,522, `cos_actions.py` 2,398; `tests/test_cos.py` 3,282 and `test_core.py` 1,942 mirror the problem. The arch guide's "How To Read The Code" is fighting the file layout.
**Action:** extract by seam, behavior-identical, tests green after each step: `service.py` → `ingest.py` + `retrieval.py` + `memory_service.py` (+ keep `BrainService` as a facade so callers don't churn); split `test_cos.py` by subsystem (extraction / actions / policy / routing). Do **not** interleave with feature work.
**Acceptance:** no source file > ~2,000 lines; `BrainService` public API unchanged; 352+ tests still pass.

### 15. Remove or repurpose dead schema and dead artifacts
**Evidence:** `relations` table — created in `db.py:103`, **0 rows live, no reader or writer anywhere**; spec §21.3 already notes typed relation edges are deferred (entity layer supersedes). `wiki_change_batches/items/interviews` are documented as archived compatibility data (fine — but only if migrations/audit still read them; verify). `.env.example` still implies API-key-first configuration.
**Action:** drop `relations` from the base schema for new installs (leave existing DBs alone; migrations tolerate presence); add a one-line "archived, read-only" comment where `wiki_change_*` is created; refresh `.env.example` to the current env-var surface (`PKM_BRAIN_LLM_PROVIDER`, auditor provider, embedding provider).
**Acceptance:** fresh init has no dead tables; docs note the removal.

### 16. Move the unrelated `teachme` skill out of this repo
**Evidence:** `skills/teachme/` + `claude-marketplace/plugins/teachme/` (git-tracked) is a general tutoring skill with no relation to PKM Brain. Scope creep in a repo that is otherwise disciplined about what it contains.
**Action [needs Peter — where to move it]:** relocate to a personal skills/dotfiles repo; remove from `claude-marketplace/.claude-plugin/marketplace.json`.
**Acceptance:** repo contains only pkm-brain-related skills.

### 17. Runtime disk footprint: ~20GB of backups/logs/experiment homes
**Evidence:** `~/brain/db/` holds **~3GB of May-era `.bak.gz`** superseded by `~/brain-runtime-backups` (email spec §1 flagged this a day ago); `~/brain/backups` 11GB; `~/brain-runtime-backups` 5GB; `~/brain/logs/curation-promotion-backups` 440MB; `nightly-maintenance.out.log` 34MB unrotated; `~/brain-shadow` 7.6GB + `~/brain-forks` 3.4GB of experiment homes.
**Action:** implement a `brain maintenance prune` command with a retention policy (e.g., keep last N runtime backups + anything < 30 days; rotate LaunchAgent logs at 10MB with 3 keeps) that **reports before deleting and requires `--commit`** **[needs Peter to run]**; document shadow/fork homes as manually pruned experiment artifacts in the arch guide.
**Acceptance:** command exists with dry-run default + tests; one supervised run reclaims the stale `db/*.bak.gz` (~3GB) at minimum.

### 18. Ingest rescans and re-hashes the whole inbox every 10 minutes; inbox contract is drifted
**Evidence:** `service.py:984` `ingest()` `rglob`s the inbox (372 files / 94MB live) and sha256-hashes every file on every 10-minute run. Ingested files are never removed — the inbox permanently holds capture snapshots, while README/spec say inbox is "new files waiting to be ingested."
**Action:** short-circuit hashing with (mtime, size) against the last-seen state; then either document that capture snapshots live in the inbox by design, or relocate capture snapshots to a `capture/` area and keep inbox as a true landing zone (doc + code must agree, whichever way).
**Acceptance:** steady-state 10-minute run does ~zero hashing when nothing changed; docs match behavior.

### 19. Status paths load the full embedding model just to report health
**Evidence:** `brain doctor` (and apparently even skipped nightly runs) loads all 199 sentence-transformer weight tensors and prints a tqdm progress bar into logs/JSON output streams.
**Action:** report embedding availability from cache metadata/config without instantiating the model for status-only paths; disable progress bars in non-interactive contexts (`HF_HUB_DISABLE_PROGRESS_BARS=1` or tqdm disable).
**Acceptance:** `brain doctor --json` emits clean JSON on stdout with no progress noise and runs in < ~2s; skipped nightly ticks don't load weights.

### 20. Docs corpus: separate canonical from historical
**Evidence:** 17 docs / ~350KB. Self-declared historical: `archive/chief-of-staff-autonomy-activation-spec.md`, `archive/chief-of-staff-retrieval-tuning-plan.md`, spec v0.1 (partially), `archive/cos-determinism-and-doc-conventions.md` (§1 actions largely executed). `archive/chief-of-staff-wiki-curation.md` predates the canonical CoS spec.
**Action:** create `docs/archive/` and move fully historical docs there (keep git history); stamp `chief-of-staff-wiki-curation.md` or archive it; add `docs/README.md` — a 20-line index declaring which docs are canonical (`chief-of-staff-spec`, `architecture-code-guide`, `chief-of-staff-retrieval-contract`, `entity-layer-spec`, `extraction-payload-spec`, `email-ingestion-spec`, `embeddings-productization-spec`, `brain-ui-v2-spec`, sync pair) and which are archived.
**Acceptance:** every doc has a status stamp; the index exists; no active doc points at an archived one as authoritative.

---

## P3 — Goal-advancing improvements (roadmap, in order)

### 21. Email ingestion Phases 1–3 (`email-ingestion-spec.md`)
Phase 0 (telemetry retention plumbing) landed in `c736f11`. Phases 1–3 (capture, extraction gating, entity protection) are the next major evidence-source expansion. Sequence **after** items 1–5 land — especially 5 (telemetry retention actually applied) and 11 (forget path), since email multiplies both concerns.

### 22. Review-backlog burn-down support
**Evidence:** live residue: **308 `open_questions` needs_human, 300 `cos_actions` needs_human, 88 proposed actions**. The UI v2 Queue (item 4) is the designed fix; post-rebuild triage is "the only mandatory human work in the system."
**Action:** after UI v2 lands, verify queue throughput on the real backlog (spec acceptance: 20-item mixed queue keyboard-only); add a `brain cos queue-summary` CLI one-liner (counts by kind/age) for scripts and the nightly summary so backlog growth is visible without opening the UI.

### 23. Memory-audit depth (closes the honest gap in §21.2)
Implement the promised checks in `audit.py`: near-duplicate active memories (normalized-content comparison), staleness flag (active memories with `last_seen_at`/`updated_at` older than N days), unresolved `source_ids`, supersession consistency. Keep them warnings (item 3's blast-radius rule).

### 24. Retrieval quality leftovers (spec v0.1 Phase 4 / §21.2)
Still genuinely open and worth keeping visible: query expansion, neighbor-chunk expansion, cross-encoder rerank experiment (now meaningful with real embeddings), and fact vectors as a second stamped LanceDB collection (explicitly anticipated by `embeddings-productization-spec.md`). Gate each with the retrieval eval suite (item 8 makes it corpus-specific).

---

## Suggested execution order for Codex

1. Item 4 (land UI v2 tree) — unblocks doc re-stamping and avoids merge pain everywhere else.
2. Items 2 → 1 → 6 (packet slimming trio: drop alias, budget the serialized packet, tighten MCP surface) — one PR each, agent-facing win immediately.
3. Item 3 (nightly visibility + blast radius) then 5 (telemetry retention, live compaction **[needs Peter]**).
4. Items 7, 9, 10, 20 (doc reconciliation sweep — one PR).
5. Items 8, 12, 13, 15, 16, 18, 19 (hygiene batch, small PRs).
6. Item 17 **[needs Peter]**, then 11 **[needs Peter decision]**.
7. Item 14 (module splits) as a standalone mechanical PR series.
8. Items 21–24 (roadmap).

Every PR: `uv run pytest -q` + `uv run ruff check .` green; re-stamp any doc whose described behavior changed (`docs/archive/cos-determinism-and-doc-conventions.md` §3).
