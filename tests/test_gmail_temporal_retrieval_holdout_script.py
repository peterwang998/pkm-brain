from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "evaluate_gmail_temporal_retrieval_holdout.py"


def _load_script() -> ModuleType:
    name = "test_evaluate_gmail_temporal_retrieval_holdout"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


retrieval = _load_script()


def _write_private(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(retrieval._canonical_json(row) + b"\n" for row in rows)


def _fixture_rows(
    *,
    primary_count: int = 40,
    challenge_count: int = 3,
    primary_hit_5: int = 36,
    primary_hit_10: int = 38,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    source_rows: list[dict[str, Any]] = []
    primary_queries: list[dict[str, Any]] = []
    challenge_queries: list[dict[str, Any]] = []
    primary_results: list[dict[str, Any]] = []
    challenge_results: list[dict[str, Any]] = []
    kinds = sorted(retrieval._TEMPORAL_QUERY_KINDS)

    def add_cohort(count: int, *, challenge: bool) -> None:
        prefix = "zchallenge" if challenge else "primary"
        queries = challenge_queries if challenge else primary_queries
        results = challenge_results if challenge else primary_results
        lifecycle_sequence = 0
        for index in range(count):
            thread_id = f"{prefix}-thread-private-{index:03d}"
            query_id = f"{prefix}-query-private-{index:03d}"
            source_ids = [
                f"{thread_id}-source-private-{source_index:02d}"
                for source_index in range(12)
            ]
            for source_id in source_ids:
                source_rows.append(
                    {
                        "available_at": "2026-07-01T00:00:00Z",
                        "source_id": source_id,
                        "thread_scope_id": thread_id,
                        "version": retrieval.SOURCE_VERSION,
                    }
                )
            temporal_kind = kinds[index % len(kinds)]
            lifecycle_class = None
            if temporal_kind == "lifecycle":
                lifecycle_class = sorted(retrieval._LIFECYCLE_QUERY_CLASSES)[
                    lifecycle_sequence % len(retrieval._LIFECYCLE_QUERY_CLASSES)
                ]
                lifecycle_sequence += 1
            queries.append(
                {
                    "as_of": "2026-07-02T00:00:00Z",
                    "context_source_ids": [source_ids[11]],
                    "lifecycle_query_class": lifecycle_class,
                    "query_id": query_id,
                    "query_text": f"private temporal question {prefix} {index}",
                    "relevant_source_ids": [source_ids[0]],
                    "temporal_query_kind": temporal_kind,
                    "thread_scope_id": thread_id,
                    "version": retrieval.QUERY_VERSION,
                }
            )
            if challenge:
                ranked = source_ids[1:11]
            elif index < primary_hit_5:
                ranked = source_ids[:10]
            elif index < primary_hit_10:
                ranked = [*source_ids[1:7], source_ids[0], *source_ids[7:10]]
            else:
                ranked = source_ids[1:11]
            results.append(
                {
                    "query_id": query_id,
                    "retrieved": [
                        {"rank": rank, "source_id": source_id}
                        for rank, source_id in enumerate(ranked, start=1)
                    ],
                    "version": retrieval.RESULT_VERSION,
                }
            )

    add_cohort(primary_count, challenge=False)
    add_cohort(challenge_count, challenge=True)
    source_rows.sort(key=lambda row: (row["thread_scope_id"], row["source_id"]))
    primary_queries.sort(key=lambda row: row["query_id"])
    challenge_queries.sort(key=lambda row: row["query_id"])
    primary_results.sort(key=lambda row: row["query_id"])
    challenge_results.sort(key=lambda row: row["query_id"])
    return (
        source_rows,
        primary_queries,
        challenge_queries,
        primary_results,
        challenge_results,
    )


def _write_fixture(
    root: Path,
    *,
    primary_count: int = 40,
    challenge_count: int = 3,
    primary_hit_5: int = 36,
    primary_hit_10: int = 38,
) -> dict[str, Path]:
    root.mkdir(mode=0o700, parents=True)
    rows = _fixture_rows(
        primary_count=primary_count,
        challenge_count=challenge_count,
        primary_hit_5=primary_hit_5,
        primary_hit_10=primary_hit_10,
    )
    paths = {
        "key": root / "private.key",
        "sources": root / "sources.jsonl",
        "primary_queries": root / "primary-queries.jsonl",
        "challenge_queries": root / "challenge-queries.jsonl",
        "primary_results": root / "primary-results.jsonl",
        "challenge_results": root / "challenge-results.jsonl",
    }
    _write_private(paths["key"], b"retrieval-holdout-private-key!!" * 2)
    for name, payload in zip(
        (
            "sources",
            "primary_queries",
            "challenge_queries",
            "primary_results",
            "challenge_results",
        ),
        rows,
        strict=True,
    ):
        _write_private(paths[name], _jsonl(payload))
    return paths


def _freeze_and_seal(
    tmp_path: Path,
    *,
    challenge_count: int = 3,
    primary_hit_5: int = 36,
    primary_hit_10: int = 38,
) -> tuple[dict[str, Path], Path, Path]:
    paths = _write_fixture(
        tmp_path / "inputs",
        challenge_count=challenge_count,
        primary_hit_5=primary_hit_5,
        primary_hit_10=primary_hit_10,
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        challenge_queries_path=(
            paths["challenge_queries"] if challenge_count else None
        ),
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    retrieval.seal_retrieval_run(
        bundle,
        paths["primary_results"],
        paths["key"],
        provenance,
        run,
        challenge_results_path=(
            paths["challenge_results"] if challenge_count else None
        ),
    )
    return paths, bundle, run


def _write_provenance(root: Path, bundle: Path) -> dict[str, Path]:
    root.mkdir(mode=0o700, parents=True)
    manifest = json.loads((bundle / retrieval.MANIFEST_ARTIFACT).read_bytes())
    paths = {
        "implementation": root / "implementation.bin",
        "configuration": root / "configuration.json",
        "index_receipt": root / "index-receipt.json",
        "query_protocol": root / "query-protocol.txt",
    }
    _write_private(paths["implementation"], b"retriever implementation v1\n")
    _write_private(paths["configuration"], b'{"mode":"temporal"}\n')
    _write_private(paths["query_protocol"], b"temporal query protocol v1\n")
    receipt = {
        "blind_primary_queries_sha256": manifest["artifact_sha256"][
            retrieval.PRIMARY_QUERY_ARTIFACT
        ],
        "index_artifact_sha256": "b" * 64,
        "snapshot_as_of": "2026-07-02T00:00:00Z",
        "source_authority_sha256": manifest["artifact_sha256"][
            retrieval.SOURCE_ARTIFACT
        ],
        "source_count": manifest["source_count"],
        "version": retrieval.INDEX_RECEIPT_VERSION,
    }
    _write_private(paths["index_receipt"], retrieval._canonical_json(receipt) + b"\n")
    return paths


def test_exact_forty_query_metric_gate_passes_and_challenge_is_diagnostic(
    tmp_path: Path,
) -> None:
    paths, bundle, run = _freeze_and_seal(tmp_path)
    score_root = tmp_path / "score"

    result = retrieval.score_retrieval_holdout(bundle, run, paths["key"], score_root)

    assert result["primary_queries"] == 40
    assert result["challenge_queries"] == 3
    assert result["query_hit_rate_at_5"] == 0.9
    assert result["query_hit_rate_at_10"] == 0.95
    assert result["query_hit_rate_at_5_interval_95"]["numerator"] == 36
    assert result["query_hit_rate_at_5_interval_95"]["denominator"] == 40
    assert result["query_hit_rate_at_10_interval_95"]["numerator"] == 38
    assert result["query_hit_rate_at_10_interval_95"]["denominator"] == 40
    assert result["retrieval_metric_prerequisite_passed"] is True
    assert result["release_score_gate_passed"] is False
    assert result["retrospective_preview_only"] is True
    assert result["promotion_pending"] is True
    assert result["release_or_promotion_claimed"] is False
    score = json.loads((score_root / retrieval.SCORE_ARTIFACT).read_text())
    assert score["primary"]["hits_at_5"] == 36
    assert score["primary"]["hits_at_10"] == 38
    assert score["primary"]["included_in_primary_metric_denominator"] is True
    assert score["challenge"]["query_hit_rate_at_10"] == 0.0
    assert score["challenge"]["diagnostic_only"] is True
    assert score["challenge"]["included_in_primary_metric_denominator"] is False
    assert score["retrieval_gate_passed"] is True
    assert score["release_score_gate_passed"] is False
    assert score["pending_final_authenticated_rollup"] is True
    blind_rows = retrieval._canonical_jsonl(
        (bundle / retrieval.PRIMARY_QUERY_ARTIFACT).read_bytes(),
        description="test",
    )
    assert all(
        set(row) == retrieval._PRIMARY_BLIND_QUERY_KEYS
        and "context_source_ids" not in row
        and "temporal_query_kind" not in row
        and "lifecycle_query_class" not in row
        and "relevant_source_ids" not in row
        and "thread_scope_id" not in row
        for row in blind_rows
    )
    challenge_blind_rows = retrieval._canonical_jsonl(
        (bundle / retrieval.CHALLENGE_QUERY_ARTIFACT).read_bytes(),
        description="test",
    )
    assert all(
        set(row) == retrieval._CONTEXTUAL_BLIND_QUERY_KEYS
        and bool(row["context_source_ids"])
        and "temporal_query_kind" not in row
        and "lifecycle_query_class" not in row
        and "relevant_source_ids" not in row
        and "thread_scope_id" not in row
        for row in challenge_blind_rows
    )
    sealed_primary_rows = retrieval._canonical_jsonl(
        (bundle / retrieval.PRIMARY_GOLD_ARTIFACT).read_bytes(),
        description="test",
    )
    assert all(
        set(row) == retrieval._SEALED_QUERY_CONTROL_KEYS
        and bool(row["context_source_ids"])
        and row["temporal_query_kind"] in retrieval._TEMPORAL_QUERY_KINDS
        for row in sealed_primary_rows
    )
    blind_sources = retrieval._canonical_jsonl(
        (bundle / retrieval.SOURCE_ARTIFACT).read_bytes(),
        description="test",
    )
    source_control = retrieval._canonical_jsonl(
        (bundle / retrieval.SOURCE_CONTROL_ARTIFACT).read_bytes(),
        description="test",
    )
    assert all("thread_scope_id" not in row for row in blind_sources)
    assert all("thread_scope_id" in row for row in source_control)
    assert score["primary_temporal_kind_coverage_passed"] is True
    assert set(score["primary_temporal_query_kind_counts"]) == (
        retrieval._TEMPORAL_QUERY_KINDS
    )
    assert set(score["primary_lifecycle_query_class_counts"]) == (
        retrieval._LIFECYCLE_QUERY_CLASSES
    )
    assert score["primary"]["retrieval_mode"] == "global_cold_text_only"
    assert score["challenge"]["retrieval_mode"] == ("contextual_follow_up_diagnostic")
    assert score["challenge"]["metrics_pooled_with_other_cohort"] is False
    assert score["cohort_metrics_must_not_be_pooled"] is True
    bundle_manifest = json.loads((bundle / retrieval.MANIFEST_ARTIFACT).read_bytes())
    assert bundle_manifest["primary_blind_context_source_ids_exposed"] is False
    assert bundle_manifest["blind_query_temporal_taxonomy_exposed"] is False
    assert bundle_manifest["challenge_blind_context_source_ids_exposed"] is True
    assert bundle_manifest["diagnostic_denominator"] == "primary_global_cold_only"
    run_manifest = json.loads((run / retrieval.MANIFEST_ARTIFACT).read_bytes())
    for manifest in (bundle_manifest, run_manifest):
        assert manifest["primary_retrieval_mode"] == "global_cold_text_only"
        assert manifest["primary_blind_query_contract"] == (
            "query_id_query_text_as_of_only"
        )
        assert manifest["primary_blind_context_source_ids_exposed"] is False
        assert manifest["blind_query_temporal_taxonomy_exposed"] is False
        assert manifest["challenge_retrieval_mode"] == (
            "contextual_follow_up_diagnostic"
        )
        assert manifest["challenge_blind_context_source_ids_exposed"] is True
        assert manifest["cohort_metrics_must_not_be_pooled"] is True
    assert score["primary_blind_query_contract"] == ("query_id_query_text_as_of_only")
    assert score["primary_context_hidden_from_blind_input"] is True
    assert score["blind_query_temporal_taxonomy_exposed"] is False
    assert score["challenge_context_input_present"] is True
    score_raw = (score_root / retrieval.SCORE_ARTIFACT).read_text()
    assert "private temporal question" not in score_raw
    assert "private-source" not in score_raw
    assert str(tmp_path) not in score_raw
    assert stat_mode(score_root) == 0o700
    assert stat_mode(score_root / retrieval.SCORE_ARTIFACT) == 0o600

    manifest_raw = (score_root / retrieval.MANIFEST_ARTIFACT).read_bytes()
    manifest = json.loads(manifest_raw)
    authenticator = manifest.pop("manifest_hmac_sha256")
    key = paths["key"].read_bytes()
    expected = hmac.new(
        key,
        retrieval.SCORE_MANIFEST_DOMAIN + retrieval._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(authenticator, expected)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_below_threshold_scores_fail_without_challenge_affecting_denominator(
    tmp_path: Path,
) -> None:
    paths, bundle, run = _freeze_and_seal(tmp_path, primary_hit_5=35, primary_hit_10=37)

    result = retrieval.score_retrieval_holdout(
        bundle, run, paths["key"], tmp_path / "score"
    )

    assert result["query_hit_rate_at_5"] == 0.875
    assert result["query_hit_rate_at_10"] == 0.925
    assert result["retrieval_gate_passed"] is False
    assert result["release_score_gate_passed"] is False


def test_retrospective_preview_cannot_claim_release_eligibility(tmp_path: Path) -> None:
    paths, bundle, run = _freeze_and_seal(tmp_path)

    result = retrieval.score_retrieval_holdout(
        bundle, run, paths["key"], tmp_path / "score"
    )

    assert result["retrieval_gate_passed"] is True
    assert result["release_score_gate_passed"] is False


def test_freeze_requires_exactly_forty_thread_disjoint_primary_queries(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", primary_count=39)
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="exactly forty",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "bundle",
            challenge_queries_path=paths["challenge_queries"],
        )


@pytest.mark.parametrize(
    ("leaked_field", "leaked_value"),
    (
        ("context_source_ids", ["primary-thread-private-000-source-private-11"]),
        ("temporal_query_kind", "occurrence"),
        ("lifecycle_query_class", "cancellation"),
    ),
)
def test_authenticated_primary_blind_query_leaks_fail_closed(
    tmp_path: Path,
    leaked_field: str,
    leaked_value: Any,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
    )
    blind_path = bundle / retrieval.PRIMARY_QUERY_ARTIFACT
    rows = retrieval._canonical_jsonl(blind_path.read_bytes(), description="test")
    rows[0][leaked_field] = leaked_value
    leaked_raw = _jsonl(rows)
    _write_private(blind_path, leaked_raw)

    manifest_path = bundle / retrieval.MANIFEST_ARTIFACT
    manifest = json.loads(manifest_path.read_bytes())
    manifest.pop("manifest_hmac_sha256")
    manifest["artifact_sha256"][retrieval.PRIMARY_QUERY_ARTIFACT] = (
        retrieval._sha256_bytes(leaked_raw)
    )
    authenticator = hmac.new(
        paths["key"].read_bytes(),
        retrieval.BUNDLE_MANIFEST_DOMAIN + retrieval._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        retrieval._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="blind query and sealed gold binding",
    ):
        retrieval._load_bundle(bundle, key=paths["key"].read_bytes())


def test_sealed_query_control_rejoin_preserves_private_coverage_validation(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
    )
    blind_raw = (bundle / retrieval.PRIMARY_QUERY_ARTIFACT).read_bytes()
    sealed_rows = retrieval._canonical_jsonl(
        (bundle / retrieval.PRIMARY_GOLD_ARTIFACT).read_bytes(),
        description="test",
    )
    sealed_rows[0]["context_source_ids"] = ["missing-private-source"]
    joined = retrieval._join_blind_gold_rows(
        blind_raw,
        _jsonl(sealed_rows),
        cohort="primary",
    )
    _source_rows, sources = retrieval._join_blind_source_control(
        (bundle / retrieval.SOURCE_ARTIFACT).read_bytes(),
        (bundle / retrieval.SOURCE_CONTROL_ARTIFACT).read_bytes(),
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="source coverage",
    ):
        retrieval._load_query_rows(
            _jsonl(joined),
            cohort="primary",
            sources=sources,
        )

    paths = _write_fixture(tmp_path / "inputs-overlap")
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    queries[1]["thread_scope_id"] = queries[0]["thread_scope_id"]
    queries[1]["context_source_ids"] = queries[0]["context_source_ids"]
    queries[1]["relevant_source_ids"] = queries[0]["relevant_source_ids"]
    _write_private(paths["primary_queries"], _jsonl(queries))
    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "bundle-overlap",
        )


def test_freeze_requires_balanced_temporal_kind_and_lifecycle_coverage(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "kind-collapse", challenge_count=0)
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    for row in queries:
        row["temporal_query_kind"] = "occurrence"
        row["lifecycle_query_class"] = None
    _write_private(paths["primary_queries"], _jsonl(queries))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="temporal-kind coverage",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "kind-collapse-bundle",
        )

    paths = _write_fixture(tmp_path / "lifecycle-collapse", challenge_count=0)
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    for row in queries:
        if row["temporal_query_kind"] == "lifecycle":
            row["lifecycle_query_class"] = "cancellation"
    _write_private(paths["primary_queries"], _jsonl(queries))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="temporal-kind coverage",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "lifecycle-collapse-bundle",
        )


def test_freeze_rejects_context_relevance_overlap(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "context-overlap", challenge_count=0)
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    queries[0]["context_source_ids"] = list(queries[0]["relevant_source_ids"])
    _write_private(paths["primary_queries"], _jsonl(queries))

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="overlaps sealed relevance gold",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "context-overlap-bundle",
        )


def test_freeze_rejects_missing_gold_source_and_future_query_context(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "missing")
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    queries[0]["relevant_source_ids"] = ["missing-private-source"]
    _write_private(paths["primary_queries"], _jsonl(queries))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="source coverage",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "missing-bundle",
        )

    paths = _write_fixture(tmp_path / "future")
    sources = retrieval._canonical_jsonl(
        paths["sources"].read_bytes(), description="test"
    )
    context_id = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )[0]["context_source_ids"][0]
    next(row for row in sources if row["source_id"] == context_id)["available_at"] = (
        "2026-07-03T00:00:00Z"
    )
    sources.sort(key=lambda row: (row["thread_scope_id"], row["source_id"]))
    _write_private(paths["sources"], _jsonl(sources))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="future evidence",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "future-bundle",
        )


def test_seal_rejects_incomplete_unknown_duplicate_and_future_results(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    original = retrieval._canonical_jsonl(
        paths["primary_results"].read_bytes(), description="test"
    )

    _write_private(paths["primary_results"], _jsonl(original[:-1]))
    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "run-incomplete",
        )

    duplicate = json.loads(json.dumps(original))
    duplicate[0]["retrieved"][1]["source_id"] = duplicate[0]["retrieved"][0][
        "source_id"
    ]
    _write_private(paths["primary_results"], _jsonl(duplicate))
    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "run-duplicate",
        )

    unknown = json.loads(json.dumps(original))
    unknown[0]["retrieved"][0]["source_id"] = "unknown-private-source"
    _write_private(paths["primary_results"], _jsonl(unknown))
    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "run-unknown",
        )

    # A source may exist in the frozen authority but still be unavailable at
    # the simulated query clock.
    paths = _write_fixture(tmp_path / "future-inputs", challenge_count=0)
    sources = retrieval._canonical_jsonl(
        paths["sources"].read_bytes(), description="test"
    )
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    results = retrieval._canonical_jsonl(
        paths["primary_results"].read_bytes(), description="test"
    )
    first_thread = queries[0]["thread_scope_id"]
    future_source = next(
        row
        for row in sources
        if row["thread_scope_id"] == first_thread
        and row["source_id"]
        not in set(queries[0]["context_source_ids"])
        | set(queries[0]["relevant_source_ids"])
    )
    future_source["available_at"] = "2026-07-03T00:00:00Z"
    results[0]["retrieved"][-1]["source_id"] = future_source["source_id"]
    _write_private(paths["sources"], _jsonl(sources))
    _write_private(paths["primary_results"], _jsonl(results))
    future_bundle = tmp_path / "future-bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        future_bundle,
    )
    future_provenance = _write_provenance(tmp_path / "future-provenance", future_bundle)
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="future evidence",
    ):
        retrieval.seal_retrieval_run(
            future_bundle,
            paths["primary_results"],
            paths["key"],
            future_provenance,
            tmp_path / "run-future",
        )


def test_seal_rejects_context_echo_as_retrieval_hit(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "context-echo", challenge_count=0)
    bundle = tmp_path / "context-echo-bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
    )
    provenance = _write_provenance(tmp_path / "context-echo-provenance", bundle)
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    results = retrieval._canonical_jsonl(
        paths["primary_results"].read_bytes(), description="test"
    )
    results[0]["retrieved"][-1]["source_id"] = queries[0]["context_source_ids"][0]
    _write_private(paths["primary_results"], _jsonl(results))

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="context isolation",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "context-echo-run",
        )


def test_score_rejects_mixed_bundle_run_and_tampered_artifacts(tmp_path: Path) -> None:
    paths, bundle, run = _freeze_and_seal(tmp_path / "first", challenge_count=0)
    second_paths = _write_fixture(tmp_path / "second" / "inputs", challenge_count=0)
    second_queries = retrieval._canonical_jsonl(
        second_paths["primary_queries"].read_bytes(), description="test"
    )
    second_queries[0]["query_text"] = "different private frozen question"
    _write_private(second_paths["primary_queries"], _jsonl(second_queries))
    second_bundle = tmp_path / "second" / "bundle"
    retrieval.freeze_retrieval_holdout(
        second_paths["sources"],
        second_paths["primary_queries"],
        second_paths["key"],
        second_bundle,
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="policy",
    ):
        retrieval.score_retrieval_holdout(
            second_bundle, run, paths["key"], tmp_path / "mixed-score"
        )

    bundle_artifact = bundle / retrieval.PRIMARY_QUERY_ARTIFACT
    bundle_artifact.write_bytes(bundle_artifact.read_bytes() + b"{}\n")
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="commitment",
    ):
        retrieval.score_retrieval_holdout(
            bundle, run, paths["key"], tmp_path / "tampered-score"
        )


def test_malformed_noncanonical_and_unsafe_inputs_fail_closed(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    rows = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    raw = b"\n".join(json.dumps(row, indent=2).encode() for row in rows) + b"\n"
    _write_private(paths["primary_queries"], raw)
    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "bundle",
        )

    paths["primary_queries"].chmod(0o644)
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="unsafe",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "bundle-unsafe",
        )


def test_authenticated_manifest_with_unknown_schema_field_is_rejected(
    tmp_path: Path,
) -> None:
    paths, bundle, _run = _freeze_and_seal(tmp_path, challenge_count=0)
    manifest_path = bundle / retrieval.MANIFEST_ARTIFACT
    manifest = json.loads(manifest_path.read_bytes())
    manifest.pop("manifest_hmac_sha256")
    manifest["unexpected_private_field"] = "must-not-be-accepted"
    authenticator = hmac.new(
        paths["key"].read_bytes(),
        retrieval.BUNDLE_MANIFEST_DOMAIN + retrieval._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        retrieval._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="policy",
    ):
        provenance = _write_provenance(tmp_path / "new-provenance", bundle)
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "new-run",
        )


def test_cli_failure_is_generic_and_does_not_print_private_path(tmp_path: Path) -> None:
    secret = tmp_path / "PRIVATE-MAILBOX-IDENTITY"
    secret.mkdir(mode=0o700)
    key = secret / "key"
    sources = secret / "sources"
    queries = secret / "queries"
    _write_private(key, b"x" * 32)
    _write_private(sources, b"not-json\n")
    _write_private(queries, b"not-json\n")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "freeze",
            "--source-authority",
            str(sources),
            "--primary-queries",
            str(queries),
            "--hmac-key",
            str(key),
            "--output-root",
            str(tmp_path / "bundle"),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload == {
        "error": "gmail_temporal_retrieval_holdout_failed",
        "private_content_printed": False,
        "status": "failed",
        "version": retrieval.VERSION,
    }
    assert "PRIVATE-MAILBOX-IDENTITY" not in completed.stdout
    assert completed.stderr == ""
