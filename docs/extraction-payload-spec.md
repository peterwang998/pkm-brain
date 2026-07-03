# Fact Extraction Payload And Harness

**Status:** implemented contract
**Last verified:** 2026-07-02 against the dirty working tree; focused extraction/CoS tests passing (`uv run pytest tests/test_cos.py -q`).
**Note:** H1 evidence-by-unit citation is now the live extraction contract. Quote-copy payloads are no longer a compatibility path; the database can be rebuilt.

This document describes what PKM Brain sends to the extractor LLM and how deterministic code gates the response before facts can enter the CoS action path.

## Summary

Extraction is document-scoped and windowed. `extract_recent_documents()` selects changed documents that pass source-type policy, partitions every selected document into chunk windows, and calls the extractor once per window. The LLM proposes facts in bulk for that window, but validation is per fact.

The extractor does **not** receive the full fact table. It receives:

- a static instruction/schema block;
- one source document's metadata;
- one window of raw chunk text from that document;
- lightweight routing hints from page contracts or wiki page rows, relevance-ranked for this specific source window.

The LLM returns `chunk_id` plus `evidence_unit_ids`, not quotes or character offsets. Deterministic validation reconstructs the quote cache and `source_spans` from the cited units, then runs lightweight faithfulness checks over genuine quantities. Entity-surface faithfulness is advisory rather than a hard quote-local rejection in the current implementation.

## Payload Shape

One prompt contains one source window:

```json
{
  "document": {
    "document_id": "doc_...",
    "title": "Document title",
    "source_type": "markdown_note",
    "source_id": "document:doc_...",
    "content_hash": "normalized:...",
    "raw_content_hash": "...",
    "normalized_content_empty": false
  },
  "window": {
    "window_id": "doc_...:window:0",
    "window_index": 0,
    "chunk_start_index": 0,
    "chunk_end_index": 5,
    "chunks": [
      {
        "chunk_id": "chunk_...",
        "chunk_index": 0,
        "heading_path": "",
        "token_count": 220,
        "units": [
          {
            "unit_id": "u0",
            "text": "First deterministic evidence unit."
          },
          {
            "unit_id": "u1",
            "text": "Second deterministic evidence unit."
          }
        ]
      }
    ]
  },
  "routing_hints": [
    {
      "page_hint": "concepts/example.md",
      "canonical_entity": "Example",
      "page_scope": "What belongs on the page",
      "retrieval_purpose": "Why this page exists"
    }
  ]
}
```

The expected LLM response is:

```json
{
  "facts": [
    {
      "statement": "Atomic source-backed claim.",
      "chunk_id": "chunk_...",
      "evidence_unit_ids": ["u0"],
      "page_hint": "concepts/example.md",
      "section_hint": "Summary",
      "claim_class": "factual_update",
      "entity_key": "Example",
      "entities": [
        {
          "surface": "Example",
          "type": "concept",
          "mention_kind": "named",
          "is_primary": true
        }
      ],
      "effective_at": null,
      "extraction_confidence": 0.98,
      "routing_confidence": 0.9,
      "truth_confidence": 0.95
    }
  ]
}
```

The extractor must not return `evidence_quote`, `source_spans`, or offsets. Validation does not accept quote-only facts. `evidence_quote` is still stored on accepted facts as a deterministic cache built from source text.

## Coverage

`recent_source_cards()` no longer uses `LIMIT 6` or `text[:2000]`. It loads all chunks for each selected document and builds bounded windows. Current defaults:

- `max_chunks: 6`
- `overlap_chunks: 1`
- `max_workers: 1`

Every chunk in the document appears in at least one extraction window. Cross-window duplicate facts are deduped within the current extraction run by statement, page hint, and derived spans; deeper semantic merging remains the resolver's job.

Low-information windows are filtered before the extractor is called when their normalized window text is empty after stripping template/timestamp/no-summary/no-transcript lines. The run report records these as `skipped_windows`; they count toward `source_window_count` but not `window_count`.

## Source-Type Policy

Coverage is controlled by the `extraction:` block in `config/local/cos_llm.yaml`.

Default behavior:

- extract full coverage for ordinary notes, meetings, transcripts, web clips, and unknown non-noisy source types;
- skip `agent_session_log` by default.

Current local config uses:

```yaml
roles:
  extractor:
    provider: codex
    model: gpt-5.4-mini
    reasoning_effort: low
extraction:
  window:
    max_chunks: 6
    overlap_chunks: 1
  parallelism:
    max_workers: 4
  routing_hints_limit: 40
  source_types:
    default:
      extract: true
      full_coverage: true
    agent_session_log:
      extract: false
      full_coverage: false
```

`full_coverage` is recorded as policy intent. The current implementation always windows all chunks for extractable source types.

`parallelism.max_workers` (or the equivalent top-level `extraction.max_workers` / `extraction.window_workers`) controls bounded window extraction concurrency. Workers perform prompt construction, LLM completion, and deterministic validation only. Watermark writes, action proposal/application, and final ordering remain single-threaded and deterministic.

## Harness

`extract_facts_with_validation_retry()` is the mini harness:

1. Send one source window to the extractor.
2. Parse JSON through `complete_json()`.
3. Validate every proposed fact independently.
4. Accept valid facts immediately.
5. Drop non-durable claim classes deterministically; dropped classes are not retried.
6. If any durable facts are rejected for repairable payload problems, send only the rejected fact diagnostics plus the same source window back to the LLM once. Statement faithfulness failures are terminal and are not retried.
7. Deduplicate accepted facts from both attempts.
8. Return compact validation metadata with coherent attempted/accepted/rejected/dropped counts and timing counters.

Instrumentation:

- run-level `timing` records total, selection, routing-hint, extraction, apply, and worker-count values;
- run/document/window validation reports include `window_count`, `source_window_count`, `skipped_window_count`, `attempt_count`, `llm_duration_ms`, `validation_duration_ms`, `prompt_char_count`, and `source_window_char_count`;
- each validation attempt records its own prompt size, LLM/JSON-completion duration, validation duration, and total duration.

Validation rejects facts when:

- `facts` is not an array;
- a fact is not an object;
- `statement` is missing;
- `claim_class` is missing or not one of the closed enum values;
- `chunk_id` or `evidence_unit_ids` is missing;
- `chunk_id` is unknown;
- any cited `evidence_unit_id` is unknown for the cited chunk;
- zero evidence units are cited (citing *more* than the cap is **truncated** to the first K and accepted, not rejected — see H1b);
- the statement contains a genuine **quantity** not supported by the cited units after numeric normalization (spoken forms included); non-quantity identifiers/idioms (`B2B`, `zero-to-one`, `v2`, `24/7`) are excluded from being treated as numbers (see H1b);
- named entity support is currently advisory. If this becomes a hard gate again, it must check the source **window**, not merely the cited units (see H1b).

Validation drops, rather than accepts or retries, facts whose `claim_class` is non-durable:

- `event_metadata`
- `transcript_mechanic`
- `pleasantry`
- `boilerplate`
- `non_claim`

Watermark behavior:

- document watermarks are per document, not per multi-document batch;
- watermarks use a normalized extraction-content hash, not the raw document `content_hash`;
- `ok` is recorded when the document produced accepted candidates;
- `extracted_empty` is recorded when extraction successfully found no durable accepted candidates and had no validation failures;
- `invalid` is recorded when all durable attempted facts failed validation;
- changed-only selection skips `ok` and `extracted_empty`, but not `invalid`.

## Low-Information & Boilerplate Gating

Low-information or boilerplate documents (blank meeting templates, "No transcript captured", signatures, log preambles) get re-extracted every run and/or produce provenanced-but-worthless facts. This is a **content property, not a source property** — do not special-case per source (e.g. a Hyprnote adapter). Four source-agnostic gates:

1. **Terminal "nothing durable" watermark.** `extracted_empty` is distinct from `invalid`, and changed-only selection skips it like `ok`. The watermark is keyed on a **normalized content hash** that strips whitespace/template/timestamps, not the raw `content_hash`.
   *Why:* the repeat loop is a watermark bug — only `ok` suppresses re-extraction, so `invalid` and cosmetic content-hash churn (a re-exported note with a new timestamp) re-extract forever. Distinguish *transient failure* (retry) from *successfully-found-nothing* (terminal).

2. **Claim-class gate.** The extractor labels each candidate with a closed enum: `decision | commitment | preference | role_or_responsibility | project_state | factual_update | open_question | event_metadata | transcript_mechanic | pleasantry | boilerplate | non_claim`. Deterministic policy drops non-claim classes.
   *Why:* provenance ≠ value — a boilerplate quote genuinely exists in the chunk and passes the substring gate, so the provenance check can't filter it. The class gate is the source-agnostic value filter (it classifies the *claim*, not the source). Drop only non-claims; do not a-priori drop genuine claims — the value of real claims is judged later by usage/lineage, not at extraction.

3. **Window prefilter.** A source window whose normalized content is empty after low-information stripping is recorded as skipped and never sent to the extractor.
   *Why:* a mixed document can contain one durable section and one blank/template section. Skipping only the whole document is too coarse; window-level gating saves LLM calls without blocking durable windows in the same document.

4. **Novelty terminal condition.** A document whose extraction yields zero accepted facts (after class drops) is watermarked `extracted_empty`, not `invalid`.
   *Why:* "produces nothing new" is the general property the blank template shares with every empty/boilerplate source; it stops the loop without naming any source.

**Source adapters are optional precision only.** A per-source parser (e.g. split structured metadata from body) may be added when a source has known exploitable structure, but it is never the value/repeat mechanism — the gates above are. *Optional/future:* empirically strip recurring boilerplate by cross-document line/shingle frequency (text recurring near-verbatim across many docs is template boilerplate), learning boilerplate for any source without per-source code.

## Deterministic Boundary

LLM-assisted:

- deciding which atomic claims are worth proposing from a source window;
- writing the statement;
- choosing a preliminary page/section/entity route;
- selecting evidence unit IDs that support each statement.

Deterministic:

- source-type policy;
- document selection and normalized-content watermark checks;
- chunk windowing;
- claim-class value gating;
- evidence-unit segmentation;
- quote cache and span reconstruction from cited units;
- numeric and named-entity faithfulness checks;
- canonical route validation: strip `wiki/` prefixes, block `references/*` / `agent_session_log/*`, fuzzy-snap near-duplicate canonical paths, and mark invalid destinations as unrouted residue;
- deterministic `entity_key` derivation from the validated canonical page/section route;
- candidate provenance fields;
- retry gating and compact diagnostics;
- action proposal mechanics.

## Extraction Hardening (implemented H1; throughput levers remain operational tuning)

**Added 2026-07-01**, grounded in shadow run `/private/tmp/pkm-extraction-shadow-tHWqg4` (20 hyprnote sources, 29 windows, 2 workers): 216 accepted / 304 raw; **37 final rejects — 35 `evidence_quote not found`, 2 `unknown chunk_id`**; wall 33 min, aggregate LLM 66 min → **~88 s per LLM call**; validation 2.35 s (negligible); 16/29 windows retried once (≈ all for quote failures). These are two independent problems: (H1) provenance-copy failure rate, (H2) throughput.

### H1 — Evidence by unit-ID citation, not quote-copy

**Problem.** The extractor does two jobs: write a clean atomic statement (LLM-strong) and copy an exact substring for provenance (LLM-weak on noisy ASR). It cleans / paraphrases / stitches / mis-points the quote, so the substring gate rejects it — **95% of final rejects are this**, and retry just repeats the same cleaned quote (wasted LLM time).

**Redesign — the LLM points, code copies.**
- **Payload:** pre-segment each chunk deterministically into numbered **evidence units** (sentence-split; for punctuation-poor ASR fall back to bounded ~N-token spans). Emit with stable ids: `chunk.units: [{ "unit_id": "u3", "text": "…" }]`.
- **Response:** the extractor returns `statement`, `chunk_id`, and **`evidence_unit_ids`** (1..K, K small, e.g. ≤3) instead of `evidence_quote`.
- **Deterministic reconstruction:** code assembles `evidence_quote` + `source_spans` from the cited units' *true* source text/offsets. Adjacent units → one span; non-adjacent → multiple spans (honest multi-span provenance).
- **Validation:** reject if a cited `unit_id` is absent from the cited chunk, or if **0** units are cited; if **more than K** units are cited, **truncate** to the first K (or a token-length bound) and accept — do not reject/retry (H1b). **No substring matching** — provenance is exact *by construction*.
- There is no legacy quote-copy compatibility path. This repo can rebuild the database, so new extraction payloads must cite `evidence_unit_ids`.

*Why it works:* it converts "transcribe bytes" (LLM-weak) into "select units" (LLM-strong), so provenance cannot drift. It eliminates all three current failure modes at once — paraphrase, stitch, and wrong-chunk (you cannot cite a unit outside the payload) — and removes ~most retries (a throughput win too).

**H1a — Faithfulness gate (don't lose the accuracy check).** Unit-citation guarantees the evidence *exists*, not that the *statement is faithful to it*. The old copy-gate caught this incidentally: in the shadow run a statement said "sixty hours-ish" while the source said "sixty **seven** hours" → quote miss → correctly rejected. Under unit-citation that fact would now be accepted with a wrong number. Add a **deterministic faithfulness gate** over the reconstructed evidence:
- **Numbers:** normalize numeric expressions in *both* the statement and the cited units to canonical values — including **spoken forms** ("two hundred million" → 2e8, "$300K" → 3e5) — then require every statement number to be supported by a cited-unit number (tolerance for "about/-ish"). This is the non-trivial part: a naïve token check would falsely reject spoken-number transcript facts (source "two hundred million ARR" vs statement "$200M ARR"), so a **spoken-number normalizer is required**. It correctly accepts the Sierra $200M fact and rejects the 60-vs-67 fact.
- **Entities:** each named entity in the statement should have a supporting surface form in the cited units (cheap string/alias check).
- On failure: reject with a `statement_not_supported_by_evidence` diagnostic (retry rarely helps — prefer drop or human).

*Why it works:* it restores the statement↔evidence agreement the copy-gate provided for free, so the recall gained by H1 doesn't come at the cost of silent inaccuracy. The spoken-number normalization is what makes the gate safe on transcripts.

**Acceptance:** cited unit ids reconstruct exact spans; a non-existent unit id rejects; multi-unit citation yields multi-span; `"$200M"` is accepted against "two hundred million"; a statement number absent from the cited units (60 vs 67) is rejected.

### H1b — Refinements from the 2026-07-01 real-model run

Grounded in `/private/tmp/pkm-real-model-gardener-20260701-225036` (gpt-5.4-mini, low effort, 4 workers, 20 sources): **quote-copy failures are gone** (H1 worked), but three behaviors are over-strict and are the new dominant loss/retry sources.

1. **Unit-count cap: truncate, don't reject.** 114 attempt-level "too many evidence_unit_ids" was the dominant rejection/retry driver (~21/29 windows retried). Short ASR units legitimately need >3 to cover one claim, so a hard count cap fights the segmentation. **Change:** if more than K units are cited, keep the first K adjacent units **or** cap by reconstructed-evidence token length, and **accept** — never reject/retry on count. The cap's real purpose is "don't cite the whole chunk," which a token-length bound enforces directly.
   *Why:* removes the top retry driver (recall + throughput) without weakening provenance — the kept units are still exact source text.

2. **Number faithfulness: only genuine quantities.** False positives seen: `B2B` parsed as "2B", `zero-to-one` parsed as numbers. **Change:** treat a token as a number only when it is a genuine quantity (digit sequence with optional unit/scale, or a spoken cardinal in quantity position); exclude alphanumeric identifiers (`B2B`, `P0`, `v2`, `Series B`, `24/7`, `GPT-4`) and idiomatic number-words (`zero to one`, `one-on-one`, `day one`). Bias toward *not* flagging.
   *Why:* the gate must be high-precision — a false reject discards a good fact — while still catching the real quantity-mismatch class (60 vs 67).

3. **Entity faithfulness: window scope, not unit scope (or advisory).** False positive seen: `Databricks` rejected when the cited unit supported `Unity Catalog` but didn't name Databricks, though the document context did. **Change:** require a supporting surface form anywhere in the source **window** (not just the 1..K cited units); *or* demote entity-faithfulness to an advisory signal and keep only **number**-faithfulness as a hard reject. Number errors are the dangerous, copy-gate-caught class; entity fabrication is rare and already constrained by the mention/resolution layer.
   *Why:* an entity can be legitimately established by document context outside the specific cited unit.

**Acceptance (H1b):** citing >K units yields an accepted fact with the first-K/length-bounded span and no retry; `B2B` / `zero to one` no longer trip the number gate; a statement entity supported elsewhere in the window is accepted; a genuine quantity mismatch (60 vs 67) still rejects.

### H2 — Throughput

Validation is 2.35 s; **all cost is LLM completion** (~88 s/call). On a reasoning model (gpt-5.4, medium effort, ~46K-char/~12K-token prompt) that time is **likely inference-dominated, not subprocess overhead** — so tune model work first.

**Hard constraint — the default extractor provider stays `codex` (subprocess exec).** This is a product requirement, not an incidental default: extraction must consume the user's regular **Codex quota**, *not* a separately-metered cloud API key. Levers 1–4 are the supported path to throughput and **all keep the extractor on Codex**; they are expected to be sufficient. Lever 5 (HTTP/persistent provider) is an explicit, user-chosen opt-in escape hatch only — never the shipped default, and hardening work must not flip the default off `codex`.

Levers, in order:

1. **Reasoning effort (Codex-compatible, biggest easy win).** Set `roles.extractor.reasoning_effort: low` (or `minimal`) in `config/local/cos_llm.yaml` — already supported (`llm.py`), passed to `codex` as `-c model_reasoning_effort=…`. Extraction is structured extraction, not deep reasoning; verify accepted-count/quality hold at lower effort.
2. **Fewer retries (free, from H1).** Unit-id citation removes ~most quote-failure retries (16/29 windows today). Do not retry faithfulness failures.
3. **Parallelism.** `extraction.parallelism.max_workers` 2 → 3–4 (aggregate LLM ≈ 2× wall confirms overlap works); bounded by provider rate limits / local process pressure.
4. **Prompt size.** ~46K chars/call. Trim `routing_hints_limit` (currently 40) to the top-N **relevance-ranked** hints and keep the static block lean; smaller prompt = faster inference + lower cost. ⚠️ Trimming the hint *count* is only safe once hints are ranked by window relevance (see routing fix **R1** in `docs/entity-layer-spec.md`); with today's recency ordering, fewer hints means *worse* routing.
5. **Persistent provider — opt-in only, off by default (leaves Codex).** *Not the default*, per the hard constraint above. The `codex` provider is `subprocess.run(codex … exec)` **per call** (fresh temp dir, no persistent/server mode — `llm.py` `CodexProvider._complete_once`), so it can't reuse connections or cache prompts. A user who *explicitly opts in* can point the `extractor` role at `anthropic` / `openai` / `ollama` (HTTP) to remove per-call subprocess spawn and unlock connection reuse, concurrency, and **prompt caching** of the large static prefix. The cost of opting in is exactly what the default avoids: API-key auth + separately-metered billing, and it no longer consumes Codex quota. If a persistent provider is ever needed without a cloud bill, `ollama` (local persistent server) is the local-first option. Treat this strictly as a last resort after 1–4, chosen deliberately by the user — not something to enable automatically.

*Diagnostic to resolve overhead-vs-inference:* time one `codex exec` on a trivial prompt at low effort. Tens of seconds → subprocess/init overhead dominates (the provider swap pays off big). A few seconds → inference dominates (reasoning effort + prompt size are the levers).

## Drift Checks

These should stay true:

- no `LIMIT 6` in `src/pkm_brain/extraction.py`;
- no `text[:2000]` clipping in extraction payloads;
- no prompt instruction that allows whole-chunk fallback spans;
- no prompt instruction that accepts quote-only provenance from the LLM;
- no fact-table rows in extractor prompts;
- facts from the extractor carry unit-derived `source_spans`, deterministic `evidence_quote`, and `extractor_model`;
- `agent_session_log` is skipped by default unless config overrides it;
- low-information/boilerplate docs are watermarked terminally (`extracted_empty`, keyed on normalized content) and not re-extracted; non-claim classes are dropped (no per-source boilerplate adapter required);
- the **default `extractor` provider is `codex`** (subprocess exec, consuming the user's Codex quota); the HTTP providers (`anthropic`/`openai`/`ollama`) are opt-in only and never the shipped default (`llm.py` `DEFAULT_LLM_PROVIDER = "codex"`). Throughput work must not change this default.

Useful verification:

```bash
uv run pytest tests/test_cos.py::test_extraction_skips_agent_session_logs_by_default \
  tests/test_cos.py::test_extraction_windows_all_chunks_without_truncation \
  tests/test_cos.py::test_extraction_prompt_includes_routing_hints_without_fact_rows \
  tests/test_cos.py::test_extraction_derives_spans_from_evidence_unit_ids \
  tests/test_cos.py::test_extraction_accepts_supported_numeric_paraphrase \
  tests/test_cos.py::test_extraction_rejects_unsupported_statement_number \
  tests/test_cos.py::test_extraction_drops_non_claim_classes_and_marks_extracted_empty \
  tests/test_cos.py::test_extraction_empty_normalized_content_skips_cosmetic_reexports -q
uv run ruff check src/pkm_brain/extraction.py tests/test_cos.py
```

Before reloading the nightly LaunchAgent, run a real-model shadow extraction against a copied Brain DB and inspect accepted/rejected counts plus unit-derived spans and deterministic quote caches.
