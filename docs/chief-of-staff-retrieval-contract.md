# Chief-of-Staff Retrieval Contract

Status: companion contract for `docs/chief-of-staff-spec.md` section 5.9. Historical tuning details live in `docs/chief-of-staff-retrieval-tuning-plan.md`.

## Problem

The chief-of-staff fact compiler can now produce managed wiki pages from source-backed facts, but retrieval still behaves like a raw RAG packet builder. In forked-data tests against `/Users/Peter/brain-forks/wiki-review-llm-coach`, known topics such as Hightouch and CloudZero were found, but absent topics still returned normal-looking packets of unrelated chunks and pages.

The agent-facing retrieval path needs to express uncertainty, prefer curated pages, and avoid letting weak lexical coincidences look authoritative.

## Goals

- Return an explicit verdict for every retrieval call: `found`, `partial`, or `no_strong_match`.
- Stop returning confident-looking packets for absent topics.
- Prefer managed chief-of-staff pages over raw reference pages when both match.
- Keep raw chunks as evidence, not as the dominant answer surface when curated pages exist.
- Filter memories by query relevance so unrelated active or proposed memories do not appear in every packet.
- Prevent document-title-only matches from dragging unrelated chunks into the result.

## Non-Goals

- Do not change the fact compiler or conflict-resolution model in this slice.
- Keep `citation_snapshots` as the canonical provenance field; the old duplicate `citations` alias is retired.
- Do not require MCP callers to know about forked data paths. The server still chooses its `BrainPaths` at startup.

## Response Contract

`retrieve_context` and `search` return these additional fields:

- `retrieval_verdict`: one of `found`, `partial`, or `no_strong_match`
- `retrieval_confidence`: normalized confidence from `0.0` to `1.0`
- `retrieval_reasons`: short diagnostic strings explaining the verdict

For `no_strong_match`, callers should treat the packet as "Brain has no reliable evidence for this query." Nearby low-confidence material may be added later as a separate `near_matches` tier, but it should not be mixed with authoritative context.

## Ranking Rules

1. Chunks below the relevance floor are not selected.
2. A chunk must have local query evidence in body or heading, not only a source title match.
3. Agent-session-log titles are ignored for entity anchors unless the query is explicitly about agents, logs, tools, or implementation history.
4. Selected chunks are capped per document to avoid one neighboring meeting or one matching-titled source filling the packet.
5. Wiki pages below the page floor are not selected.
6. Managed wiki pages get a ranking boost; reference pages no longer get a default advantage over synthesized pages.
7. Memories must match query terms before appearing. Proposed memories require a stronger match than active memories.

## Expected Behavior

- Present topics with managed pages should return `found` or `partial`, with managed pages near the top.
- Present topics with only raw evidence may still return `found` if chunk evidence is strong.
- Absent topics should return `no_strong_match` with empty or minimal selected context.
- Matching a document title alone should not cause unrelated chunks from that document to be selected.

## Evaluation Fixtures

The forked-data spot checks use:

- Positive: Hightouch agentic CDP role
- Positive: CloudZero May 29 dashboard sharing interview
- Positive: chief-of-staff wiki review design
- Positive but noisy: AI Chinese children's songs publishing idea
- Negative: ZephyrMart geothermal coffee roasting in Iceland
- Negative: mango orchard irrigation sensors in Fresno
- Negative: Kubernetes eBPF Mars rover telemetry

These should become a durable golden-query smoke set once thresholds stabilize.
