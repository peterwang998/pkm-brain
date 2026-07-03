# Embeddings Productization — Hash → Sentence-Transformers

**Status:** draft — implementation plan for Codex; not yet implemented (Phase 0 packaging landed in `daf11e3`)
**Last verified:** 2026-07-03 against commit `daf11e3`
**Goal:** make real semantic embeddings a configurable, provenance-stamped retrieval component with a safe rebuild path, replacing the currently unreachable `SentenceTransformerProvider` — while keeping the deterministic offline hash provider available, and never mixing vector spaces.

This is the "embeddings productization" workstream referenced by `docs/architecture-code-guide.md` (Major TODOs) and by the Caveats section of `docs/entity-layer-spec.md`.

---

## 1. Why now

- Hash embeddings (`hash_embedding()`, `embeddings.py`) are a signed bag-of-hashed-tokens. They capture token overlap only — no synonyms, no paraphrase. The vector channel today is effectively a second lexical signal on top of BM25, so RRF fusion adds little.
- Several planned consumers are explicitly blocked on a real encoder: semantic entity candidate generation (`entity-layer-spec.md` Caveats), semantic routing-hint ranking (R1 note: "semantic ranking improves once a real encoder lands"), gardener embedding-similar page-merge candidates, and fact vectors (removed in `5251d08`; can return as a second stamped collection once real embeddings exist).
- The current failure posture is silent in both directions. `get_embedding_provider(prefer_model=True)` swallows every exception and returns hash (`embeddings.py:35`), and since `sentence-transformers` became an optional extra (`daf11e3`), a missing package would also degrade silently. Silent substitution is the worst outcome: it looks like semantic search and isn't.
- The trap is armed: `VECTOR_DIM = 384` equals bge-small-en-v1.5's output dimension, and LanceDB rows carry no provider metadata, so flipping providers without a rebuild would silently blend incompatible vector spaces — no dimension error would ever fire.

## 2. Current state (verified against `daf11e3`)

- `sentence-transformers>=5.4.1` lives in `[project.optional-dependencies] embeddings` (`pyproject.toml`); the base install has no torch. Install with `uv sync --extra embeddings`.
- `SentenceTransformerProvider` exists with a lazy model import (`embeddings.py:19`), but is unreachable: `BrainService.__init__` defaults `prefer_model_embeddings=False`, and every construction site across CLI/MCP/UI/automation/sync/evals passes `False` or omits the flag. Zero call sites pass `True`; no env var or config read can flip it.
- `get_embedding_provider(prefer_model)` eagerly health-checks (`provider.embed(["health check"])`) and falls back to hash on any exception, silently. The eager check also defeats the lazy import for any path that would request the model.
- `config/local/config.yaml` is written at `init_workspace()` with `brain_home` and `embedding_model: BAAI/bge-small-en-v1.5` (`service.py`, `init_workspace`) but is **never read by any code**. It is the config route this spec makes real.
- Ingest and query embedding already share one object: `BrainService.embedding_provider` feeds both chunk upserts (ingest, `rebuild_vector_index`, `reindex_chunks`) and query embedding (`search_vectors`, `indexes.py:61`). Provider consistency is structural once resolution is config-driven.
- Rebuild machinery exists: `rebuild_vector_index()` (timestamped backup + verification), `reset_retrieval_index()`, `index doctor` with `vector_chunk_ids()` (`indexes.py:162`) for SQLite↔LanceDB consistency.
- Chunks are the only vector collection (`TABLE_NAME = "chunks"`); the fact-vector helpers were deleted in `5251d08`.
- Chunking targets `DEFAULT_TARGET_TOKENS = 1200` (`chunking.py:9`), which exceeds bge-small's 512-token max sequence — see D4 and Phase 4.

## 3. Decisions

### D1 — Config-driven provider, one per brain home, no caller booleans
Provider selection moves from the `prefer_model_embeddings` constructor flag (deleted) to config resolved once per process:

```yaml
# config/local/config.yaml
embedding:
  provider: hash            # hash | sentence-transformer
  model: BAAI/bge-small-en-v1.5
  query_instruction: ""     # optional; see D4
```

Env overrides mirror the LLM provider pattern: `PKM_BRAIN_EMBEDDING_PROVIDER`, `PKM_BRAIN_EMBEDDING_MODEL`. Precedence: env → config file → default (`hash`). The legacy flat `embedding_model:` key written by older inits is read as `embedding.model` for compatibility; the misleading `brain_home:` key stops being written. Default stays `hash` until Phase 5 flips the real brain deliberately.

### D2 — One vector space per index, stamped and enforced
A LanceDB index holds vectors from exactly one provider+model, recorded in a sidecar written atomically on table create/rebuild:

```json
// indexes/lancedb/embedding_provider.json
{"provider": "sentence-transformer", "model": "BAAI/bge-small-en-v1.5", "dim": 384, "built_at": "..."}
```

The sidecar lives inside the index directory because it describes that derived artifact and must die with it (rebuilds, `reset_retrieval_index`, mirror refresh all replace it). Every vector write and every vector search verifies stamp == active provider first. Mismatch on write raises; mismatch on read disables the vector channel for that query and reports why. An unstamped non-empty index is grandfathered as `hash` (the only provider that ever wrote historically) and stamped on next verify; `index doctor` notes the grandfathering. Vectors are never migrated in place — provider change means rebuild, per the house rule that indexes are rebuildable derived artifacts.

### D3 — Absence over wrongness (non-silent degradation)
When the configured provider cannot load (extra not installed, model not cached and offline, torch broken):

- **Ingest proceeds without vector writes.** Documents, chunks, and FTS are unaffected; the run summary records `vector_writes: skipped` with the reason. The deterministic spine never blocks on torch or the network.
- **Search runs without the vector channel** (BM25/FTS still work) and marks it: `retrieval_reasons` gains `vector_search unavailable: <reason>`, debug output carries the same.
- **`brain doctor` / UI status / automation summaries** report `embedding: configured=<x> available=<bool> reason=<...>` loudly.
- **Hash is never substituted where sentence-transformer is configured.** The current silent `except Exception: return hash` in `get_embedding_provider` is removed. Missing vectors are backfillable (Phase 3); wrong-space vectors are not.

### D4 — Asymmetric query embedding, done at the provider interface
BGE-family models want a query instruction prefix for retrieval queries ("Represent this sentence for searching relevant passages: ") and plain passages. `EmbeddingProvider` gains `embed_queries(texts)` (default implementation: same as `embed`); the ST provider applies `query_instruction` there. Search paths call `embed_queries`; ingest/rebuild call `embed`. Passage truncation: bge-small embeds at most 512 model tokens while chunks target 1200, so v1 embeds `heading_path + head of chunk` (heading prefix is cheap salience) and accepts tail truncation. Re-chunking is explicitly out of scope; `db reindex-chunks` exists if targets are ever revisited.

### D5 — Eval-gated flip, negative controls are the hard gate
The real brain flips provider only after a side-by-side retrieval eval (hash index vs model index on a copied brain home) shows non-regression. Semantic embeddings are the *most likely component to break negative controls* — nearest neighbors make absent topics look plausibly present — so `negative_control_pass` must stay at 100% and verdict calibration must not degrade. Add a handful of paraphrase golden cases (queries where hash demonstrably misses) so the upside is measured, not assumed. Same posture as policy promotion: no gate, no flip.

### D6 — Model download is explicit, never implicit in scheduled jobs
First model use normally triggers a Hugging Face download (~130 MB). Scheduled paths (LaunchAgent ingest, nightly) must load cache-only and treat an uncached model as unavailable per D3. Interactive commands (`brain embeddings download`, or `index rebuild-vectors` with a TTY) may download with progress output.

---

## 4. Build plan

### Phase 0 — Packaging ✅ (landed `daf11e3`)
`sentence-transformers` is the `embeddings` optional extra; README documents the extra. Nothing imports it eagerly (verify stays true: base test run must not import torch).

### Phase 1 — Provider resolution route
- `embeddings.py`: `EmbeddingConfig` + `load_embedding_config(paths)` (config.yaml `embedding:` block, legacy flat `embedding_model:` fallback, env overrides); `resolve_embedding_provider(config)` returning the hash provider, the ST provider, or an `UnavailableEmbeddingProvider` sentinel carrying the reason (raises on `embed`, reports on inspection). Remove the eager health-check embed; keep the model import lazy so non-vector paths (sync status, UI status, setup wizard) never touch torch even when the model is configured.
- Delete the `prefer_model_embeddings` parameter and update all construction sites (mechanical; they all currently pass `False` or omit).
- Surface `embedding_provider` as `{configured, available, reason}` in `brain doctor`, `index status`, UI status, and automation summaries (fields already carry the provider name; extend them).
- Update `init_workspace()` to write the new `embedding:` block shape.

*Acceptance:* env and config routes both select the provider; `provider: sentence-transformer` without the extra installed yields a loud actionable doctor error (`uv sync --extra embeddings`) and D3 behavior everywhere — never silent hash. Base test suite passes with no torch installed.

### Phase 2 — Index provenance stamp + mismatch enforcement
- Sidecar read/write/verify helpers in `indexes.py`; `upsert_table_vectors` refuses on stamp mismatch; `search_table_vectors` returns unavailable-with-reason on mismatch; stamp written on create/rebuild; grandfather rule for unstamped indexes.
- `index doctor` reports stamp contents, config-vs-stamp match, and missing-vector-row count (SQLite chunk ids minus `vector_chunk_ids()`).

*Acceptance:* flipping config without rebuilding blocks vector writes and disables (with reason) vector reads; no query ever executes against a mismatched space; doctor shows the mismatch and the fix.

### Phase 3 — Rebuild and backfill paths
- `rebuild_vector_index()` embeds with the configured provider, batches, writes the stamp on success, keeps the existing timestamped backup/verification behavior.
- `brain index rebuild-vectors --missing-only`: embed only chunks absent from LanceDB (D3 recovery); refuses if stamp ≠ active config (that case requires a full rebuild).
- Progress/count reporting; print provider, model, and chunk count before starting (cost visibility, mirroring the extraction guardrail pattern).

*Acceptance:* config flip → doctor mismatch → `rebuild-vectors` → stamp matches → vector channel live on the new space; `--missing-only` fills D3 gaps without a full rebuild.

### Phase 4 — Query-side correctness
- `embed_queries()` + `query_instruction` wiring; search paths switch to it; heading-path-prefixed passage text at embed time (ingest, rebuild, reindex use the same helper so rebuilds reproduce ingest vectors).

*Acceptance:* with an instruction configured, query and passage vectors for the same string differ; a paraphrase smoke query returns sane neighbors on a model-stamped index.

### Phase 5 — Eval gate + real-brain flip (go/no-go)
- Copy the brain home (tasklist temp-home pattern), rebuild the copy with `sentence-transformer`, run `brain eval run --suite retrieval` against both homes; record the side-by-side (verdict accuracy, source-hit, calibration/ECE, noise rate, negative-control pass).
- Add 3–5 paraphrase golden cases to `retrieval_fixtures.py` first so semantic gain is measurable.
- On green: flip `~/brain` config, `brain embeddings download`, full rebuild with verified backup, then monitor doctor + nightly index status. Default in code stays `hash`; the flip is a config change on this machine.

*Acceptance:* recorded eval comparison with negative controls at 100%; the real brain runs stamped model vectors; `uv run pytest -q` and `ruff` stay green throughout.

### Unblocked afterwards (separate workstreams, not planned here)
Fact vectors as a second stamped collection (re-designed, not resurrected from `5251d08`); semantic routing-hint ranking (entity spec R1); gardener embedding-similarity candidates; entity-resolution embedding tier.

---

## 5. Caveats
- Model vectors are not bit-deterministic across torch versions/hardware. That is acceptable for a derived, stamped, rebuildable index — but it means hash remains the right default for tests, CI, and fixtures, and eval fixtures must not assert exact vector values.
- bge-small truncates passages at 512 model tokens against a 1200-token chunk target; v1 accepts head-of-chunk embedding (D4). If retrieval quality points at tail loss, revisit chunk targets as its own change via `db reindex-chunks`.
- The repo's `token_count` is approximate, not the model tokenizer; treat 512 as a soft boundary.
- Secondary/sync: indexes are node-local derived artifacts and are not synced; each node resolves its own config and stamps its own index. Mirror rebuild uses the local provider.

## 6. Non-goals
- No cloud embedding APIs (unchanged from README scope).
- No mixed-provider indexes, no per-row provider metadata, no in-place re-embedding — provider change is always an explicit rebuild.
- No automatic provider fallback of any kind (D3).
- No chunking changes in this workstream.

## 7. Code touchpoints
`embeddings.py` (config load, resolution, sentinel, `embed_queries`, instruction) · `paths.py` (stamp path property) · `indexes.py` (stamp read/write/verify in upsert/search; stats surface) · `service.py` (drop `prefer_model_embeddings` everywhere, doctor/index_doctor fields, rebuild + `--missing-only`, degraded-search reasons, `init_workspace` config template) · `cli.py` (construction sites, `embeddings download`, rebuild flag, doctor output) · `ui_server.py` / `automation.py` (status fields) · `retrieval_fixtures.py` (paraphrase golden cases) · tests: extend `tests/test_config_split.py` toward `embedding:` resolution; new stamp-mismatch, degraded-mode, and query-instruction tests.

## 8. Verification bundle
```bash
uv run ruff check .
uv run pytest -q                                  # base env: must pass with no torch installed
uv sync --extra embeddings
uv run pytest -q                                  # extra env: ST provider tests included
uv run brain doctor --home ~/brain                # embedding: configured/available/reason
uv run brain index doctor --home ~/brain          # stamp vs config, missing vector rows
uv run brain eval run --suite retrieval --home <copy>   # side-by-side before any real flip
```
