# Gmail Fact-Parity Preflight — 2026-07-23

## Status

The local-only matched-source cohort is structurally frozen. It is **not**
semantic parity evidence and is **not** release-ready. No private model, network,
database, or persistence call was made during the export and packet build.

The temporal natural cohort cannot double as the parity cohort. Its 150 messages
contain only four `important_fact` targets; the 100-message challenge contains
33. Those estimands remain separate and must never be padded, pooled, or used to
infer the parity denominator. The parity evaluator independently requires at
least 50 supported original non-temporal semantic units across at least 30
threads at every candidate, review, and persisted stage.

## Frozen baseline and target

The evaluator now requires the installed original Brain arm to match exactly:

- commit `d5405b9cf7a81775dfc84200892c206687756f3c`;
- prompt `extractor-evidence-units-v6-speaker-context`;
- external Codex model `gpt-5.6-luna`;
- reasoning effort `low`.

The V2 arm remains pinned to prompt
`extractor-evidence-units-v15-gmail-event-time`, external Codex
`gpt-5.6-luna`, and `low` reasoning effort. Three separately receipted V2 runs
are mandatory.

The installed original Brain home has no Gmail knowledge projections: Gmail
projection arrived after the frozen baseline. Consequently, an "original Gmail
admission inventory" cannot be recovered from its runtime database. The fair
comparison is conditional capability on identical packets: both extractors
receive the same preselected Gmail message evidence, and the original arm—not
V2—defines the supported non-temporal denominator after blinded alignment.

## Local-only cohort contract

`scripts/export_gmail_fact_parity_admissions.py` authenticates the temporal v15
manifest and excludes every primary, challenge, and sealed-reserve thread. It
then reads only projection frontmatter, HMAC-ranks the remaining fact-admitted
threads, and freezes identical original/V2 admission inventories. Its manifest
states that the cohort is a fact-rich capability challenge, not a mailbox
population estimate, cannot be pooled with either temporal cohort, and has an
unverified semantic denominator.

The 2026-07-23 freeze produced:

- 325 temporal-holdout threads excluded;
- 311 eligible non-holdout fact-rich threads;
- 150 selected threads and 254 admitted messages;
- byte-identical original and V2 inventories;
- 150 opaque packets over 150 threads and 254 messages;
- owner-only `0700` directories and `0600` files;
- zero external, database, or persistence calls; and
- `semantic_denominator_verified=false` and `release_evidence_ready=false`.

Aggregate artifact identities:

- admission inventory SHA-256:
  `8d35f935d76311dc42fa1a99d51d6dbac16348647946de2167956753852c19ef`;
- cohort SHA-256:
  `ce519e38c2c97ee7fbf11485422fbad3c254b85a7bf38da6c7436a56019fbd19`;
- packet SHA-256:
  `d93a2333da3648ed23954cff35589c12307a1521bb8c2a82cfdddad9f67235da`;
- canonical source-set SHA-256:
  `9e2b969d6bb0788221c7c7b68c5447fbb5fb8e262a8bd72b3e8b3ba946213529`.

## Reproduction commands

The completed local-only preparation was:

```bash
.venv/bin/python scripts/export_gmail_fact_parity_admissions.py \
  /private/tmp/pkm-brain-v7-eval-20260722/inbox/documents/gmail \
  /private/tmp/gmail-temporal-holdout-v4-20260723-retrospective-v15 \
  /private/tmp/gmail-temporal-holdout-v1-20260722-iter1.key \
  /private/tmp/gmail-fact-parity-admissions-v1-20260723 \
  --thread-count 150

.venv/bin/python scripts/build_gmail_fact_parity_cohort.py \
  /private/tmp/pkm-brain-v7-eval-20260722/inbox/documents/gmail \
  /private/tmp/gmail-fact-parity-admissions-v1-20260723/original-admissions.jsonl \
  /private/tmp/gmail-fact-parity-admissions-v1-20260723/v2-admissions.jsonl \
  /private/tmp/gmail-temporal-holdout-v1-20260722-iter1.key \
  /private/tmp/gmail-fact-parity-cohort-v1-20260723
```

## Remaining execution authority

The repository still lacks the production adapter that turns one baseline run
and three V2 runs over `packets.jsonl` into complete
`gmail_fact_parity_run_v1` outputs and hash-bound receipts. That adapter is a
hard blocker: existing runtime facts cannot substitute for fresh identical-
packet runs, and raw count parity cannot substitute for semantic alignment.

Once those four run artifacts exist, the existing preparer can freeze the
private, arm-blind work queue. An external Codex Sol/medium judge must then
align and judge every emitted member, with its receipt binding the cohort,
packets, work queue, and completed units. Only the final evaluator can establish
the release gate. Every V2 run must reach at least 95% semantic-unit retention,
95% thread retention, 95% macro-thread retention, and 95% supported precision at
all three stages, with zero critical errors or duplicates and at least 95%
three-run stability.

Until that execution and judging authority is provided, the correct status is
**structurally prepared, semantic parity unmeasured**.
