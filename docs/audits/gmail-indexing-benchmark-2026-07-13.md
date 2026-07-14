# Gmail Indexing Benchmark - 2026-07-13

**Status:** completed isolated experiment
**Window:** 2026-04-15 through 2026-07-13, America/Los_Angeles
**Runner:** `scripts/gmail_brain_benchmark.py`

## Verdict

Indexing 90 days of normalized Gmail is storage-feasible, but unfiltered fact extraction is not cost-feasible.

- The isolated Brain index grew by 319.5 MB allocated (304.7 MiB) for 6,650 thread documents and 21,458 chunks.
- Linear storage growth at the measured rate is about 1.30 GB/year (1.21 GiB/year), before facts and long-term retention policy.
- Deterministic preprocessing admitted only 318 of 6,650 threads (4.8%) to fact extraction.
- The three-day production-policy sample averaged 551,224 total tokens and 172,976 uncached input tokens per selected day.
- Scaling observed per-document cost to the 90-day mean of 3.53 eligible threads/day projects about 448,792 total tokens and 5.7 facts per day.
- Applying the same observed per-document cost to all 74.15 daily threads projects roughly 9.43 million tokens/day. That counterfactual applies the complete fact pipeline to all mail; it is not an estimate for a single-stage operational scan.

The production design should permit retrieval indexing only after explicit approval, classify before extraction, exclude attachment payloads and quoted-history duplication, and pass only likely human/evidence threads into the fact pipeline. Bulk and transactional mail should remain retrieval-only unless a narrower rule explicitly admits them.

## Isolation And Auth

The experiment used the existing Codex-specific Google Workspace credential stored in macOS Keychain under `google-workspace-mcp-codex`. It did not use or widen the Brain Gmail connector's identity-only OAuth grant.

The private experiment root is:

```text
~/Library/Application Support/PKM Brain Experiments/gmail-90d-20260713/
```

The runner:

- never opened or wrote the live Brain database;
- initialized a separate Brain home under the experiment root;
- copied only non-secret embedding, model-role, curation, and labeled-eval configuration;
- stored Gmail API cache, normalized mail, manifests, and reports with private file permissions;
- left the production Gmail connector disabled and `auth_only`;
- selected explicit document IDs for exactly three extraction days.

The resumable API cache contains full `threads.get` responses and can include inline or small MIME payload data returned with a message. No attachment-fetch endpoint was called, and non-text/attachment parts were excluded from normalized documents and the Brain index. The cache is a private, disposable experiment artifact rather than part of the proposed production storage contract.

The full private aggregate report is `artifacts/report.json` under the experiment root. Its original per-cycle usage fields omit the three generated-ID resolver calls; the reconciled totals in this audit supersede those usage fields. Repository documentation contains no sender, recipient, subject, thread ID, or message body.

## Corpus And Preprocessing

The Gmail API query excluded spam and trash and returned 6,720 thread IDs. Seventy had no message whose Gmail internal date fell inside the strict local 90-day boundary, leaving 6,650 normalized thread documents and 7,529 in-window messages.

The pull used `threads.list` plus `threads.get`, cached each response for resumability, and retried time-based errors with exponential backoff. The current runner globally limits Gmail calls to two requests/second because the June 2026 standard quota assigns 40 units to `threads.get` against 6,000 per-user units/minute. Projects with older grandfathered quotas may permit more, but the experiment harness does not assume that.

Each thread became one snapshot-style Markdown document. The normalization pass:

- preferred `text/plain` and used HTML-to-text only as fallback;
- excluded 2,044 recognized attachment MIME parts from normalized documents;
- removed 1,630,387 characters of repeated quoted reply history;
- capped 94 unusually large message bodies;
- classified threads before fact eligibility;
- treated sent-participation as a strong human-thread signal;
- required at least 120 retained body characters for fact eligibility.

Classification result:

| Class | Threads | Share |
| --- | ---: | ---: |
| Bulk | 3,560 | 53.5% |
| Transactional | 2,754 | 41.4% |
| Human | 336 | 5.1% |
| Fact eligible | 318 | 4.8% |

The Gmail `sizeEstimate` total was 744.5 MB, which includes message structures and attachments. Normalized text was 34.5 MB logical / 49.0 MB allocated. The resumable compressed API cache was 134.3 MB logical / 148.0 MB allocated and is not part of the Brain footprint.

## Index Storage

The test used the production sentence-transformer configuration (`BAAI/bge-small-en-v1.5`), not the hash test provider.

| Component | Logical | Allocated |
| --- | ---: | ---: |
| Raw normalized copies | 34.5 MB | 49.0 MB |
| SQLite, chunks, and FTS | 186.4 MB | 201.4 MB |
| LanceDB vectors | 69.5 MB | 69.5 MB |
| Config/other | 0.1 MB | 0.1 MB |
| Brain after index | 290.5 MB | 320.0 MB |
| Empty Brain baseline | 0.5 MB | 0.5 MB |
| Net index growth | 290.0 MB | 319.5 MB |

Ingestion created 6,650 documents, 21,458 chunks, 21,458 embeddings, and approximately 2.91 million chunk tokens in 163.8 seconds. Document, chunk, and vector counts matched exactly and ingestion reported zero errors.

The measured allocation is about 48.0 KB per thread. A simple 365/90 projection yields 1.30 GB/year. This projection assumes similar message mix, thread length, chunking, filesystem allocation, and embedding dimensions.

## Fact Sample

Representative complete weekdays were selected by fact-eligible thread volume at low, median, and high quantiles. Only eligible threads whose latest in-window activity fell on those dates were passed to extraction.

| Date | All threads | Eligible docs | Candidates | Applied facts | Critic rejects | Total tokens | Uncached input |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-05-08 | 78 | 3 | 11 | 3 | 6 | 388,531 | 129,917 |
| 2026-05-26 | 97 | 4 | 8 | 8 | 0 | 475,984 | 140,446 |
| 2026-06-26 | 102 | 6 | 16 | 10 | 6 | 789,156 | 248,566 |
| **Total** | 277 | 13 | 35 | 21 | 12 | 1,653,671 | 518,929 |

Two candidates used fallback routes and correctly remained L3 review work. The other 33 matched the clean-fact L2 policy after the isolated Brain passed the copied 35-case labeled extraction eval. Critic review agreed with 21 and rejected 12.

Role totals:

| Role | Requests | Model/effort | Total tokens | Uncached input |
| --- | ---: | --- | ---: | ---: |
| Extractor | 30 | `gpt-5.6-luna`, low | 422,164 | 154,768 |
| Evaluator/critic | 91 | `gpt-5.6-luna`, medium | 1,193,235 | 353,702 |
| Route resolver | 3 | `gpt-5.6-luna`, medium | 38,272 | 10,459 |
| **Combined** | **124** |  | **1,653,671** | **518,929** |

Evaluator cost dominates because critic evidence review may make more than one request per candidate. Three resolver calls were initially logged under generated cycle IDs rather than the benchmark run IDs; the reconciled totals above include them, and the runner now propagates benchmark usage IDs into route resolution. The sample created 21 facts, 37 entities, and 54 fact-entity links. The Brain grew by another 17.0 MB allocated during the sample; this includes SQLite page allocation, action/evidence records, facts/entities, eval reports, and usage logs, so it should not be projected linearly as fact-only storage.

## Limitations

- Classification is deterministic and intentionally conservative, but it has not been manually precision/recall labeled against this mailbox.
- One document per thread means a future daily connector should process snapshot replacements, not append a second document for every reply.
- The sample represents three weekday volume quantiles, not seasonality or every mail category.
- Thread text was ingested as `markdown_note`; a production adapter still needs a first-class source type and privacy/redaction contract.
- Attachments were counted but not downloaded, indexed, or analyzed.
- The token projection is linear in eligible document count. Long threads, routing context growth, critic repairs, and model caching make real cost nonlinear.
- No gardener or sampled auditor cycle was run. The measurement covers extraction, route resolution, clean-fact critic evaluation, deterministic application/entity linking, and their usage telemetry.
- The first normalized corpus used `source_created_at` frontmatter, which the generic source-date parser did not recognize. The runner now writes `created_at`; the sampled facts from the completed experiment retain their processing-time fallback and should not be used to evaluate temporal accuracy.

## Recommended Next Step

Do not enable the production Gmail connector yet. Revise and approve the email preprocessing spec with these defaults:

1. Use Gmail read-only access; no send, modify, delete, or label mutation.
2. Normalize one replaceable document per thread and preserve Gmail internal dates.
3. Exclude attachment bytes by default; make attachment ingestion a separate opt-in design.
4. Strip quoted reply duplication before storage and extraction.
5. Index normalized bulk, transactional, and human mail only if retrieval value justifies the measured 1.30 GB/year.
6. Admit only likely human/evidence threads to extraction, with a daily document/token budget and deferred overflow.
7. Link email mentions to known entities by default; do not create new entities without stronger evidence.
8. Add a labeled mailbox classification set before relying on the 4.8% eligibility rate.
9. Reduce evaluator requests per candidate before production; evaluator usage was 72% of measured tokens.
10. Keep the auth-only Brain connector boundary until scopes, redaction, retention, and deletion semantics are approved.

## Official API References

- [Manage Gmail threads](https://developers.google.com/workspace/gmail/api/guides/threads)
- [`users.threads.list` reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/list)
- [Gmail API usage limits and per-method units](https://developers.google.com/workspace/gmail/api/reference/quota)
- [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)

The future adapter should account for `gmail.readonly` being a restricted scope and use the narrowest approved access. The benchmark's separate existing credential is not a scope decision for the Brain connector.
