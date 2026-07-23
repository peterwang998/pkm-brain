# Gmail Fact-Parity Preflight — 2026-07-23

## Status

The local-only matched-source cohort has been rebuilt under the authenticated
v2 authority. It is **not**
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
- 150 owner-only raw-to-opaque source bindings and an HMAC-signed manifest;
- owner-only `0700` directories and `0600` files;
- zero external, database, or persistence calls; and
- `semantic_denominator_verified=false` and `release_evidence_ready=false`.

Aggregate artifact identities:

- admission inventory SHA-256:
  `8d35f935d76311dc42fa1a99d51d6dbac16348647946de2167956753852c19ef`;
- cohort SHA-256:
  `f837c4b835491433d6b69ff923ac47225440f9b3966be525561da15a1c84a0fd`;
- packet SHA-256:
  `46b074966bf25aeb434ce3906504c69616f63a2c79b9fae5b7627ba41a32be00`;
- source-binding SHA-256:
  `96ac3294aa644da3f18c41297970503852a7b621815d787eb4dd7061ed84e34e`;
- manifest HMAC SHA-256:
  `2445e174f59c8a4501ee4d137e9b8d64f365a44924b481779e40425f906ed85d`;
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
  /private/tmp/gmail-fact-parity-cohort-v2-20260723
```

## Remaining execution authority

The repository now defines a canonical, hash-bound post-admission stage contract
and a fail-closed `scripts/run_gmail_fact_parity.py` orchestrator that requires
`gmail_fact_parity_run_v4` evidence and produces a
`gmail_fact_parity_manifest_v5` bundle scored as
`gmail_fact_parity_evaluation_v6`. Candidate membership comes
from an emitted validated candidate; review membership comes from a mapped
policy/critic action or explicit review residue; persistence requires one
applied action and exactly one matching current fact. Ambiguous candidate/action
or action/fact mappings fail closed. Run headers and receipts bind the contract,
adapter, production tree, runtime configuration, prompt, and a complete ordered
per-invocation ledger. Production mode accepts only the canonical adapter path;
test adapters require an explicit test-only Python authority. Candidate, action,
and persisted-fact ownership is one-to-one across a run, and a dirty production
checkout is rejected. `adapter_sha256` is the shared code identity over the
runner and canonical adapter bytes. Each run separately records
`adapter_executable_sha256`, which binds its declared Python launcher, resolved
target, and target bytes; Original and V2 may use different bound launchers,
while all repeated V2 runs must use one exact executable binding. The runner
revalidates both identities immediately before and after each adapter invocation,
so this separation does not require an untracked shared launcher. These per-run
digests record internal agreement; they are not independent launcher authority,
because an arbitrary executable could emit a self-consistent digest and artifact
pair. Release target authority therefore remains fail-closed until trusted
Original and V2 launcher bindings are pinned independently and match the
receipts. Separately, canonical-adapter authority requires the runner and
adapter to be tracked and byte-identical to their current Git `HEAD` blobs. This
is owner-process integrity, not host supply-chain attestation: the personal-use
threat boundary trusts the local operating system, system Git executable, and
repository object database.

The orchestrator now has the canonical production adapter in
`scripts/gmail_fact_parity_production_adapter.py`. It executes sealed packets
through the production extraction API in disposable Brain homes, records the
external Codex invocations, and derives contract records from run-scoped
actions and facts. The globally installed Brain CLI remains stale and is
rejected; the frozen original source tree is the baseline authority.
The remaining blocker is execution and judgment of the private cohort: an
implemented adapter, schema-valid self-reports, existing runtime facts, and raw
count parity cannot substitute for fresh identical-packet runs and semantic
alignment.

Once those four run artifacts exist, the existing preparer can freeze the
private, arm-blind work queue. An external Codex Sol/medium judge must then
align and judge every emitted member, with its receipt binding the cohort,
packets, work queue, and completed units. Only the final evaluator can establish
the metric gate. Every V2 run must reach at least 95% semantic-unit retention,
95% thread retention, 95% macro-thread retention, and 95% supported precision at
all three stages, with zero critical errors or duplicates and at least 95%
three-run stability. The production release gate additionally requires the
canonical adapter and independently authenticated invocations; both remain
false today, so a structurally passing self-report cannot claim release.

Until that execution and judging authority is provided, the correct status is
**structurally prepared, semantic parity unmeasured**.
