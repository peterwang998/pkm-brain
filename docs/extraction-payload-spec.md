# Fact Extraction Payload And Harness

**Status:** implemented contract
**Last verified:** 2026-06-27 against the dirty working tree

This document describes what PKM Brain sends to the extractor LLM and how deterministic code gates the response before facts can enter the CoS action path.

## Summary

Extraction is document-scoped and windowed. `extract_recent_documents()` selects changed documents that pass source-type policy, partitions every selected document into chunk windows, and calls the extractor once per window. The LLM proposes facts in bulk for that window, but validation is per fact.

The extractor does **not** receive the full fact table. It receives:

- a static instruction/schema block;
- one source document's metadata;
- one window of raw chunk text from that document;
- lightweight routing hints from page contracts or wiki page rows.

The LLM returns `chunk_id` plus an exact `evidence_quote`, not character offsets. Deterministic validation finds the quote inside the cited chunk and computes `source_spans`.

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
        "text": "full chunk text, not clipped"
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
      "evidence_quote": "Exact quote copied from the chunk text.",
      "page_hint": "concepts/example.md",
      "section_hint": "Summary",
      "claim_class": "factual_update",
      "entity_key": "concepts:example:summary",
      "effective_at": null,
      "extraction_confidence": 0.98,
      "routing_confidence": 0.9,
      "truth_confidence": 0.95
    }
  ]
}
```

Compatibility note: validation can also read quote-bearing `source_spans` entries, but offset-only spans are rejected. LLM character offsets are not trusted.

## Coverage

`recent_source_cards()` no longer uses `LIMIT 6` or `text[:2000]`. It loads all chunks for each selected document and builds bounded windows. Current defaults:

- `max_chunks: 6`
- `overlap_chunks: 1`

Every chunk in the document appears in at least one extraction window. Cross-window duplicate facts are deduped within the current extraction run by statement, page hint, and derived spans; deeper semantic merging remains the resolver's job.

## Source-Type Policy

Coverage is controlled by the `extraction:` block in `config/local/cos_llm.yaml`.

Default behavior:

- extract full coverage for ordinary notes, meetings, transcripts, web clips, and unknown non-noisy source types;
- skip `agent_session_log` by default.

Current local config uses:

```yaml
extraction:
  window:
    max_chunks: 6
    overlap_chunks: 1
  routing_hints_limit: 80
  source_types:
    default:
      extract: true
      full_coverage: true
    agent_session_log:
      extract: false
      full_coverage: false
```

`full_coverage` is recorded as policy intent. The current implementation always windows all chunks for extractable source types.

## Harness

`extract_facts_with_validation_retry()` is the mini harness:

1. Send one source window to the extractor.
2. Parse JSON through `complete_json()`.
3. Validate every proposed fact independently.
4. Accept valid facts immediately.
5. Drop non-durable claim classes deterministically; dropped classes are not retried.
6. If any durable facts are rejected, send only the rejected fact diagnostics plus the same source window back to the LLM once.
7. Deduplicate accepted facts from both attempts.
8. Return compact validation metadata with coherent attempted/accepted/rejected/dropped counts.

Validation rejects facts when:

- `facts` is not an array;
- a fact is not an object;
- `statement` is missing;
- `claim_class` is missing or not one of the closed enum values;
- `chunk_id` or `evidence_quote` is missing;
- `chunk_id` is unknown;
- `evidence_quote` cannot be found in the cited chunk text, including after whitespace normalization.

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

Low-information or boilerplate documents (blank meeting templates, "No transcript captured", signatures, log preambles) get re-extracted every run and/or produce provenanced-but-worthless facts. This is a **content property, not a source property** — do not special-case per source (e.g. a Hyprnote adapter). Three source-agnostic gates:

1. **Terminal "nothing durable" watermark.** `extracted_empty` is distinct from `invalid`, and changed-only selection skips it like `ok`. The watermark is keyed on a **normalized content hash** that strips whitespace/template/timestamps, not the raw `content_hash`.
   *Why:* the repeat loop is a watermark bug — only `ok` suppresses re-extraction, so `invalid` and cosmetic content-hash churn (a re-exported note with a new timestamp) re-extract forever. Distinguish *transient failure* (retry) from *successfully-found-nothing* (terminal).

2. **Claim-class gate.** The extractor labels each candidate with a closed enum: `decision | commitment | preference | role_or_responsibility | project_state | factual_update | open_question | event_metadata | transcript_mechanic | pleasantry | boilerplate | non_claim`. Deterministic policy drops non-claim classes.
   *Why:* provenance ≠ value — a boilerplate quote genuinely exists in the chunk and passes the substring gate, so the provenance check can't filter it. The class gate is the source-agnostic value filter (it classifies the *claim*, not the source). Drop only non-claims; do not a-priori drop genuine claims — the value of real claims is judged later by usage/lineage, not at extraction.

3. **Novelty terminal condition.** A document whose extraction yields zero accepted facts (after class drops) is watermarked `extracted_empty`, not `invalid`.
   *Why:* "produces nothing new" is the general property the blank template shares with every empty/boilerplate source; it stops the loop without naming any source.

**Source adapters are optional precision only.** A per-source parser (e.g. split structured metadata from body) may be added when a source has known exploitable structure, but it is never the value/repeat mechanism — the three gates above are. *Optional/future:* empirically strip recurring boilerplate by cross-document line/shingle frequency (text recurring near-verbatim across many docs is template boilerplate), learning boilerplate for any source without per-source code.

## Deterministic Boundary

LLM-assisted:

- deciding which atomic claims are worth proposing from a source window;
- writing the statement;
- choosing a preliminary page/section/entity route;
- copying the supporting quote.

Deterministic:

- source-type policy;
- document selection and normalized-content watermark checks;
- chunk windowing;
- claim-class value gating;
- quote lookup and span computation;
- deterministic `entity_key` derivation from the canonical page/section route;
- candidate provenance fields;
- retry gating and compact diagnostics;
- action proposal mechanics.

## Drift Checks

These should stay true:

- no `LIMIT 6` in `src/pkm_brain/extraction.py`;
- no `text[:2000]` clipping in extraction payloads;
- no prompt instruction that allows whole-chunk fallback spans;
- no fact-table rows in extractor prompts;
- facts from the extractor carry quote-derived `source_spans` and `extractor_model`;
- `agent_session_log` is skipped by default unless config overrides it;
- low-information/boilerplate docs are watermarked terminally (`extracted_empty`, keyed on normalized content) and not re-extracted; non-claim classes are dropped (no per-source boilerplate adapter required).

Useful verification:

```bash
uv run pytest tests/test_cos.py::test_extraction_skips_agent_session_logs_by_default \
  tests/test_cos.py::test_extraction_windows_all_chunks_without_truncation \
  tests/test_cos.py::test_extraction_prompt_includes_routing_hints_without_fact_rows \
  tests/test_cos.py::test_extraction_derives_spans_from_evidence_quote \
  tests/test_cos.py::test_extraction_drops_non_claim_classes_and_marks_extracted_empty \
  tests/test_cos.py::test_extraction_empty_normalized_content_skips_cosmetic_reexports -q
uv run ruff check src/pkm_brain/extraction.py tests/test_cos.py
```

Before reloading the nightly LaunchAgent, run a real-model shadow extraction against a copied Brain DB and inspect accepted/rejected counts plus quote-derived spans.
