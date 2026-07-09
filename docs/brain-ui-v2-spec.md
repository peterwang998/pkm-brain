# Brain UI v2 — Design Spec

**Status:** implemented — v2 shell, Today, Queue, Wiki, Entities, Ask, Ops, static asset serving, legacy `ui_shell()` retirement, and native app queue orientation are committed.
**Last verified:** 2026-07-09 against implementation commit `841ba3e`; `swift test --package-path app`, `uv run ruff check .`, and `uv run pytest -q` passed.
**Scaffold:** `src/pkm_brain/ui_static/` (created with this spec — the HTML shell and design tokens are the design; JS view modules are contracts to implement)
**Standing decisions honored:** the browser stays a *thin local control plane over existing primitives* (wiki decision `pkm-brain-reuse-existing-primitives-for-browser-review`); no parallel backend, no parallel approval system. All mutations dispatch to existing service/CoS/question/memory functions.

---

## 1. Evaluation of v1 (what we're replacing and why)

v1 is a single ~1,100-line HTML/CSS/JS string embedded in `ui_server.py` (`ui_shell()`), rendered as eight peer tabs: Status, Setup, Sync, Jobs, Logs, Wiki, Chief of Staff, Memory Review.

What's right and must be kept: stdlib HTTP server, Bearer-token auth, JSON API over existing primitives, zero build step, no external network dependencies.

What's wrong:

1. **The IA is inverted.** Five of eight tabs are operational plumbing (status/setup/sync/jobs/logs) that matters a few times a month; the two activities that matter daily — reviewing residue and reading knowledge — are crammed into two tabs. The UI is organized by *backend module*, not by *user job*.
2. **JSON is the primary display surface.** Most views end in `jsonBlock(data)` — raw payload dumps. The wiki (Markdown projections built for reading) is shown as source text, not rendered.
3. **Review is not a workflow.** The residue queues (now 166 unrouted + 81 conflicts + gardener proposals + memory proposals) live in separate views with separate interaction patterns, no keyboard support, no progress sense, no undo. Post-rebuild, triage is the *only mandatory human work in the system* — it deserves the best surface, and has the weakest.
4. **The entity layer doesn't exist in the UI at all.** 378 entities, the keystone of the new architecture — zero pixels.
5. **No answer to "what happened while I was away?"** The system is autonomous now; the first question every session is *did the nightly behave, what changed, what needs me* — currently answered by reading raw run JSON.
6. **No URL state, no dark mode, no maintainability.** Refresh loses your place; the giant Python string means every UI change is a `ui_server.py` diff.

## 2. First principles

Single user (the operator-owner), local, desktop. Sessions are short and purposeful. Since the rebuild, the system writes itself; the human's remaining jobs, in frequency order:

- **P1 — Trust:** "Is it healthy? What did it do overnight? What needs me?" (daily, 30 seconds)
- **P2 — Judgment:** work the residue queue — the one activity where human time is the bottleneck (few times/week, minutes; must be keyboard-fast, context-rich, undoable)
- **P3 — Knowledge:** read what the brain knows — pages, entities, provenance ("why does it believe this?") (weekly, browsing)
- **P4 — Interrogation:** run a retrieval and see *why* the packet looks the way it does (debugging agents' context) (occasional)
- **P5 — Operations:** sync, indexes, policy, logs, doctor (rare, but must be complete)

Derived rules:

- **Organize by job, not by table.** Six destinations, ordered by frequency: Today · Queue · Wiki · Entities · Ask · Ops.
- **Every list is keyboard-walkable; every decision is one keystroke + undoable.** The action ledger's inverses exist precisely to make review decisions cheap to reverse — surface that as instant undo.
- **Evidence-first cards.** Any item asking for judgment shows: the claim, the verbatim quote (mono, blockquote), the source link, confidences, and — for conflicts — the counterpart, side by side. Never make the human open a second view to decide.
- **Rendered knowledge, linked provenance.** Wiki pages render as documents; every fact bullet exposes quote → chunk → raw document on demand.
- **JSON is a debug affordance, not a display format.** Every view offers a collapsed "raw" disclosure; none leads with it.
- **URL is state.** Hash routing (`#/queue/conflicts/oq_123`); refresh and back-button always work.

Explicit non-goals: multi-user, auth beyond the existing token, mobile layouts (readable at 1024px is enough), realtime push (poll on focus), editing raw sources, charts/dashboards for vanity metrics, any build toolchain, any CDN/runtime network dependency.

## 3. Information architecture & screens

Left nav rail, six items, keyboard `g` chords. Global: `⌘K` command palette (navigate, jump to page/entity, run actions); `?` shows keyboard help; auto light/dark via `prefers-color-scheme`.

### 3.1 Today (`#/today`) — P1

```
┌───────┬──────────────────────────────────────────────────────────────┐
│ nav   │  Today                                    last visit: 2d ago │
│       │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐            │
│ Today │  │ nightly │ │ evals   │ │ index   │ │ agents  │  ← pulse   │
│ Queue │  │ ✓ 03:12 │ │ ✓ all   │ │ ✓ ST    │ │ ✓ 10min │    chips   │
│ Wiki  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘            │
│ Entit │  Since your last visit                                       │
│ Ask   │  • 41 facts added → career/sierra-poc (12), companies/… (8)  │
│ Ops   │  • 2 actions auto-reverted by audit  [inspect]               │
│       │  • 1 policy event · 0 demotions                              │
│       │  Needs you                                                   │
│       │  ▸ 166 unrouted · 81 conflicts · 13 topology · 4 memories    │
│       │    [Start review →  goes to #/queue]                         │
└───────┴──────────────────────────────────────────────────────────────┘
```

- **Pulse chips** (nightly, evals, index/embeddings, LaunchAgents, sync if configured): green/amber/red; click → relevant Ops drill-down. Red is reserved for "a stage crashed"; amber for "found issues awaiting review."
- **Since-you-last-looked digest**: facts added grouped by page, reverts, demotions, eval transitions — computed server-side from `cos_actions`/`automation_runs` newer than the client-sent `since` timestamp (stored in localStorage).
- **Needs-you strip**: live queue counts by kind; the primary CTA of the whole app is `Start review`.

### 3.2 Queue (`#/queue[/kind][/id]`) — P2, the centerpiece

One unified inbox for every human-judgment item: `unrouted_fact`, `fact_conflict_review`, `document_extraction_anomaly`, gardener topology proposals, proposed memories, audit-flagged actions. Split pane:

```
┌───────┬───────────────────────┬──────────────────────────────────────┐
│ nav   │ ALL(264) ▸conflicts 81│  Conflict · oq_4f2c…  (3 of 81)      │
│       │ unrouted 166 topo 13  │  ┌────────────────┬────────────────┐ │
│       │ ──────────────────────│  │ CANDIDATE      │ EXISTING       │ │
│       │ ▸ Peter said politi…  │  │ statement…     │ statement…     │ │
│       │   Sierra POC target…  │  │ ❝quote❞ mono   │ ❝quote❞ mono   │ │
│       │   Hightouch offer d…  │  │ src: meeting…  │ src: meeting…  │ │
│       │   (j/k to move)       │  │ conf 0.86      │ conf 0.91      │ │
│       │                       │  └────────────────┴────────────────┘ │
│       │                       │  [1] keep existing [2] candidate     │
│       │                       │  [3] both true     [4] supports      │
│       │                       │  [5] current       [6] unsure        │
│       │                       │  (nav keys j/k NEVER decide — v2.1  │
│       │                       │   fix: k collided with keep)        │
└───────┴───────────────────────┴──────────────────────────────────────┘
```

Interaction contract:

- `j/k` move, `enter` focus, numeric decision keys act on the focused card **and auto-advance**; every decision fires an optimistic update plus a 6-second undo toast wired to the action inverse (or question re-open). Conflict cards use `1-6` left-to-right. Unrouted cards number route candidates first, then "new page", "reject", and "skip". Topology/action/audit/memory cards use left-to-right numeric buttons (`1` approve/revert, `2` reject/mark ok, etc.). Outside decision focus, `u` may still undo the last decision.
- Progress ("3 of 81") and a session tally ("resolved 14 · skipped 2") — triage should feel like emptying an inbox.
- Card payloads are complete: no decision may require leaving the pane. Unrouted cards show the fact + top-5 route candidates (fuzzy-ranked, numbered `1–5` to pick) + "new page…" + "reject". Topology cards show the resolved entity/page target names first, then ids, affected pages/fact counts, and the gardener's evidence. Memory cards show content, type, scope, provenance.
- Batch mode: `x` selects, visible `Reject selected`/`Route selected…` bar appears. Filters persist in URL.
- Every decision dispatches to the *existing* primitive (answer question / apply-reject action / approve memory) — the queue endpoint is an aggregator + dispatcher, never a second state store.
- `/api/queue` must filter and paginate before building complete cards. A live backlog in the hundreds should keep `limit=1` under 300 ms on local hardware, and complete-card enrichment should run only for the returned page.

### 3.3 Wiki (`#/wiki[/path]`) — P3

Reader-first. Left: namespace tree with type/status filters and search. Right: the rendered page.

- Markdown rendered as a document (measure ~68ch, comfortable line-height). Frontmatter becomes a metadata header row (type · status · updated · fact count · sources count), not YAML text.
- **Every fact bullet is interactive**: hover/`enter` opens a provenance popover — verbatim `evidence_quote` (mono), source doc link, confidences, extraction method — with two actions: `confirm` (sets `confirmed_by_user` via the existing flow) and `flag` (opens a question). This popover is the single most important element in the wiki: it is the "why should I believe this?" answer, one keystroke away.
- Right rail: the page's **contract** (what belongs here / what doesn't), related pages, and snapshot history (click → side-by-side diff using the existing snapshots).
- Managed pages show a subtle "projection — edits happen via facts" banner; hand-authored pages show an Edit affordance (plain textarea + save via existing write path) — the one direct-edit surface, matching the standing decision.

### 3.4 Entities (`#/entities[/id]`) — P3, new surface

- Index: table of active entities (name, type, fact count, alias count, last observed), type filter chips, sort by fact count. Merged/archived hidden behind a filter.
- Detail: header (name, type, aliases as removable chips, status) · facts grouped by page (each with the same provenance popover) · co-mentioned entities · **merge candidates panel** (same-normalized/containment suggestions from gardener signals) with `propose merge` → creates the standard `entity_merge` action through policy — the UI proposes, the ledger decides.

### 3.5 Ask (`#/ask`) — P4

A retrieval console, not a chat. One input, mode selector (`default/compact/broad/inspect`), debug toggle.

- Results render the packet the way agents consume it: **verdict + confidence banner** (`found · 0.94` / `no_strong_match`), then Facts, Pages, Chunks sections — each row showing score, `selection_reasons`, suppression state (suppressed rows collapsed under "suppressed (n)…"), token counts, with the same provenance popovers.
- Debug on: fanout/rerank details, lineage boosts, budget accounting. This view doubles as the retrieval-tuning instrument; it must visibly answer "why did/didn't X come back?"
- History of recent asks (from `retrieval_events`, session-local list) for A/B-ing query phrasings.

### 3.6 Ops (`#/ops[/section]`) — P5

Everything operational, consolidated with an overview grid and drill-in sections: Runs (automation + ingest, stage chips, expandable summaries) · Actions ledger (filterable table; **guarded revert button** with confirm, surfacing `applied_state_hash` drift errors verbatim) · Policy (active version + rules table, version history) · Audit (results, demotion events) · Index & embeddings (doctor output, stamp vs config, optimize buttons) · Sync · Logs (tail view) · Setup. Each section = existing endpoint, presented as tables/chips, raw JSON behind a disclosure.

## 4. Visual design (fixed)

Implemented as CSS custom properties in `ui_static/tokens.css` (authoritative; the file ships with this spec).

- **Type:** system stack for UI; `ui-monospace` for IDs, quotes, evidence, code. Scale: 12 / 13 (base) / 14 / 16 / 20 semibold. Reading surfaces (wiki body) 15/1.6.
- **Color:** near-monochrome slate neutrals; **one accent: indigo** (`--accent`), used only for interactive/selected states. Semantic colors reserved exclusively for status: `--ok` green, `--warn` amber, `--bad` red. Light and dark themes via `prefers-color-scheme` (both defined in tokens; no toggle UI in v2.0).
- **Surfaces:** flat panels, 1px `--line` borders, 8px radius, **no shadows** (single elevation exception: command palette overlay). 8px spacing grid (`--s1..--s6` = 4/8/12/16/24/32).
- **Density:** compact tables (32px rows, 12px type for meta); evidence quotes always `blockquote` + mono + subtle left border. IDs truncate to 10 chars with click-to-copy.
- **Motion:** 120ms ease on focus/selection; nothing else animates. Undo toast slides in bottom-left, auto-dismisses 6s.
- **Empty states are instructions**, one sentence + the command to run (e.g. Queue empty → "Nothing needs you. Nightly runs will add items here."). No illustrations.

## 5. Architecture (fixed)

- **Server:** keep the stdlib `http.server` + Bearer token exactly as-is. Add static-file serving for `src/pkm_brain/ui_static/` via `importlib.resources` (safe path handling; correct MIME; `Cache-Control: no-cache`). `/` serves `index.html`; `ui_shell()` and the legacy embedded HTML route are retired.
- **Client:** vanilla ES modules, **zero build step, zero external dependencies, zero CDN**. Files: `app.js` (hash router + shell + palette + toasts + keyboard), `api.js` (fetch wrapper: token, JSON, error normalization), `md.js` (**small custom Markdown renderer** — headings, lists, bold/italic, links, blockquotes, inline code; the corpus is constrained generated Markdown + simple hand pages; escape-by-default, test fixtures required; do not vendor a library), `views/*.js` one module per destination, `tokens.css` + `app.css`.
- **State:** URL hash is canonical view state; localStorage for token + last-visit timestamp + session tallies. No client framework, no global store — each view owns its fetch/render/keys and registers/unregisters its keymap on mount/unmount.

### 5.1 API additions (the real backend work — all read paths over existing tables, all writes over existing primitives)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/digest?since=<iso>` | GET | Today: pulse (latest automation run stage statuses, eval pass/fail, index doctor summary, launch-agent status) + deltas since `since` (facts by page from applied `fact_upsert` actions, reverts, demotions, queue counts by kind) |
| `/api/queue?kind=&limit=&cursor=` | GET | Unified feed: `open_questions(needs_human)` by kind + gardener-proposed actions + `memories(proposed)` + `audit_status='sampled_bad'` unresolved — each item as a **complete card** (payloads, quotes, counterpart fact, route candidates for unrouted, affected-page summaries for topology) |
| `/api/queue/<item_id>/decision` | POST | `{decision, payload?}` → dispatch to existing primitives (answer/dismiss question, approve/reject/apply action, approve/reject/archive memory, route fact). Returns the applied result + undo handle (action id / question id) |
| `/api/queue/undo` | POST | `{undo_handle}` → guarded revert / re-open via existing inverses |
| `/api/entities?type=&q=` · `/api/entities/<id>` | GET | Index + detail (aliases, facts grouped by page, co-mentions, merge candidates from gardener signal functions) |
| `/api/entities/merge` | POST | Propose `entity_merge` through the normal action/policy path |
| `/api/retrieve` | POST | `{task, mode, debug}` → `service.retrieve_context` verbatim |
| `/api/search?q=` | GET | `service.search` |
| `/api/actions/<id>/revert` | POST | Existing guarded revert, errors surfaced verbatim |

Everything else reuses v1 endpoints unchanged. No schema migrations required; the queue/digest endpoints are aggregating reads.

## 6. Build plan

- **P1 — Shell + Today.** Static serving, `index.html`/tokens/router/api client, Today with `/api/digest`. *Accept:* app boots at `/`, token flow works, pulse reflects a broken nightly within one refresh, digest deltas match `cos_actions` ground truth, light+dark both render.
- **P2 — Queue.** `/api/queue` + decision/undo dispatch + split-pane UI + full keyboard model + batch + progress. *Accept:* a 20-item mixed queue is triageable keyboard-only; every decision verifiably lands in the ledger/questions/memories tables; undo restores within 6s window; conflict cards show both facts side-by-side (counterpart data from the fixed precheck path); nothing requires leaving the pane.
- **P3 — Wiki + Entities.** Renderer with escape/fixture tests, provenance popovers (confirm/flag wired), contract rail, snapshot diffs; entity index/detail/merge-propose. *Accept:* databricks page renders as a readable document; popover shows verbatim quote + working source link; confirm sets `confirmed_by_user`; merge proposal appears as a normal policy-gated action.
- **P4 — Ask.** `/api/retrieve` + packet rendering + debug + history. *Accept:* a negative-control query visibly shows `no_strong_match`; suppressed chunks are inspectable; the "why is this here" question is answerable from `selection_reasons` alone.
- **P5 — Ops + palette + retirement.** Ops consolidation, `⌘K`, `?` help, delete `ui_shell()` and the legacy route. *Accept:* v1 feature-parity checklist passes (sync, jobs, logs, setup, policy, audit, contracts, revert); `ui_server.py` no longer contains HTML; `pytest` covers all new endpoints incl. auth-required and dispatch correctness.

Each phase lands independently green (`ruff` + `pytest` + manual checklist), consistent with house discipline.

## 7. Non-goals (v2)
Multi-user/accounts · mobile layouts · realtime push · charts/analytics · WYSIWYG editing · client framework or build tooling · CDN/runtime network access · theming beyond the two OS-driven themes · rewriting the HTTP server.

## 8. Code touchpoints
`ui_server.py` (static serving, new aggregator/dispatch endpoints, `ui_shell()` removal) · `ui_static/**` (shipped with this spec) · `wiki_facts.py`/`cos_actions.py`/`gardener.py`/`entities.py`/`service.py` (called, not changed — except small read helpers where an aggregation needs one) · `tests/test_ui_endpoints.py` + endpoint/auth coverage for the new surfaces.

## 9. Verification bundle
```bash
uv run ruff check .
uv run pytest -q                       # incl. new endpoint + renderer fixture tests
uv run brain ui --home ~/brain         # manual checklist per phase (§6)
# keyboard-only queue triage session recorded in the phase notes before P2 sign-off
```
