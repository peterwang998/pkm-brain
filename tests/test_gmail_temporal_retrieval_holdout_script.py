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

_FIXTURE_KEY = b"retrieval-holdout-private-key!!" * 2
_FIXTURE_ACCOUNT = "private-account"


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_implementation_fixture(
    root: Path,
    *,
    untracked_role: str | None = None,
) -> dict[str, tuple[str, str]]:
    root.mkdir()
    _run_git(root, "init", "-q")
    sources: dict[str, tuple[str, str]] = {}
    committed_paths: list[str] = []
    for role, relative_path in sorted(retrieval._GIT_IMPLEMENTATION_PATHS.items()):
        raw = f"# authoritative {role}\n".encode()
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        sources[role] = (raw.decode(), hashlib.sha256(raw).hexdigest())
        if role != untracked_role:
            committed_paths.append(relative_path)
    _run_git(root, "add", "--", *committed_paths)
    _run_git(
        root,
        "-c",
        "user.name=Temporal Test",
        "-c",
        "user.email=temporal-test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return sources


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
    list[dict[str, Any]],
]:
    source_rows: list[dict[str, Any]] = []
    source_bindings: list[dict[str, Any]] = []
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
            provider_thread_id = f"{prefix}-thread-private-{index:03d}"
            thread_id = retrieval._expected_bound_thread_scope_id(
                _FIXTURE_KEY, _FIXTURE_ACCOUNT, provider_thread_id
            )
            query_id = f"{prefix}-query-private-{index:03d}"
            document_id = f"{prefix}-document-private-{index:03d}"
            document_hash = hashlib.sha256(document_id.encode()).hexdigest()
            source_ids: list[str] = []
            for source_index in range(12):
                message_id = f"{prefix}-message-private-{index:03d}-{source_index:02d}"
                source_id = retrieval._expected_bound_source_id(
                    _FIXTURE_KEY, _FIXTURE_ACCOUNT, message_id
                )
                source_ids.append(source_id)
                source_rows.append(
                    {
                        "available_at": "2026-07-01T00:00:00Z",
                        "source_id": source_id,
                        "thread_scope_id": thread_id,
                        "version": retrieval.SOURCE_VERSION,
                    }
                )
                chunks = [
                    {
                        "chunk_id": f"chunk-{message_id}",
                        "end_offset": len(message_id),
                        "start_offset": 0,
                        "text_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                    }
                ]
                source_bindings.append(
                    {
                        "available_at": "2026-07-01T00:00:00Z",
                        "chunk_inventory_hmac_sha256": (
                            retrieval._expected_chunk_inventory_authenticator(  # noqa: SLF001
                                _FIXTURE_KEY,
                                source_id=source_id,
                                document_id=document_id,
                                gmail_account_key=_FIXTURE_ACCOUNT,
                                gmail_message_id=message_id,
                                gmail_thread_id=provider_thread_id,
                                chunks=chunks,
                            )
                        ),
                        "chunks": chunks,
                        "document_content_sha256": document_hash,
                        "document_id": document_id,
                        "gmail_account_key": _FIXTURE_ACCOUNT,
                        "gmail_message_id": message_id,
                        "gmail_thread_id": provider_thread_id,
                        "message_sha256": hashlib.sha256(
                            message_id.encode()
                        ).hexdigest(),
                        "source_id": source_id,
                        "version": retrieval.BINDING_VERSION,
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
    source_bindings.sort(key=lambda row: row["source_id"])
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
        source_bindings,
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
        "source_bindings": root / "source-bindings.jsonl",
        "source_authority_manifest": root / "source-authority-manifest.json",
        "primary_queries": root / "primary-queries.jsonl",
        "challenge_queries": root / "challenge-queries.jsonl",
        "primary_results": root / "primary-results.jsonl",
        "challenge_results": root / "challenge-results.jsonl",
    }
    _write_private(paths["key"], _FIXTURE_KEY)
    for name, payload in zip(
        (
            "sources",
            "primary_queries",
            "challenge_queries",
            "primary_results",
            "challenge_results",
            "source_bindings",
        ),
        rows,
        strict=True,
    ):
        _write_private(paths[name], _jsonl(payload))
    document_count = len({row["document_id"] for row in rows[5]})
    unsigned_manifest = {
        "version": retrieval.SOURCE_AUTHORITY_MANIFEST_VERSION,
        "builder_version": retrieval.SOURCE_AUTHORITY_BUILDER_VERSION,
        "source_version": retrieval.SOURCE_VERSION,
        "binding_version": retrieval.BINDING_VERSION,
        "artifact_sha256": {
            retrieval.SOURCE_AUTHORITY_SOURCE_ARTIFACT: hashlib.sha256(
                paths["sources"].read_bytes()
            ).hexdigest(),
            retrieval.SOURCE_AUTHORITY_BINDING_ARTIFACT: hashlib.sha256(
                paths["source_bindings"].read_bytes()
            ).hexdigest(),
        },
        "document_count": document_count,
        "message_count": len(rows[0]),
        "chunk_count": len(rows[0]),
        "source_scope": "every_message_in_every_active_trusted_gmail_projection",
        "source_identity": "hmac_account_and_provider_message_id",
        "thread_identity": "hmac_account_and_provider_thread_id",
        "header_chunk_clock": "latest_retained_message_provider_internal_date",
        "chunk_binding": (
            "authenticated_exact_chunk_id_range_text_sha256_and_source_assignment"
        ),
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }
    source_authority_manifest = {
        **unsigned_manifest,
        "manifest_hmac_sha256": hmac.new(
            _FIXTURE_KEY,
            retrieval.SOURCE_AUTHORITY_MANIFEST_DOMAIN
            + retrieval._canonical_json(unsigned_manifest),
            hashlib.sha256,
        ).hexdigest(),
    }
    _write_private(
        paths["source_authority_manifest"],
        retrieval._canonical_json(source_authority_manifest) + b"\n",
    )
    return paths


def _resign_source_authority_manifest(paths: dict[str, Path]) -> None:
    manifest = json.loads(paths["source_authority_manifest"].read_bytes())
    manifest.pop("manifest_hmac_sha256", None)
    manifest["artifact_sha256"] = {
        retrieval.SOURCE_AUTHORITY_SOURCE_ARTIFACT: hashlib.sha256(
            paths["sources"].read_bytes()
        ).hexdigest(),
        retrieval.SOURCE_AUTHORITY_BINDING_ARTIFACT: hashlib.sha256(
            paths["source_bindings"].read_bytes()
        ).hexdigest(),
    }
    authenticator = hmac.new(
        paths["key"].read_bytes(),
        retrieval.SOURCE_AUTHORITY_MANIFEST_DOMAIN
        + retrieval._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        paths["source_authority_manifest"],
        retrieval._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )


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
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
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


def _write_provenance(
    root: Path,
    bundle: Path,
    *,
    binding_sha256: str | None = None,
) -> dict[str, Path]:
    root.mkdir(mode=0o700, parents=True)
    manifest = json.loads((bundle / retrieval.MANIFEST_ARTIFACT).read_bytes())
    paths = {
        "implementation": root / "implementation.bin",
        "configuration": root / "configuration.json",
        "index_receipt": root / "index-receipt.json",
        "query_protocol": root / "query-protocol.txt",
    }
    implementation_sources = {
        role: source
        for role, (source, _digest) in (
            retrieval._authoritative_implementation_sources().items()
        )
    }
    implementation = {
        "version": retrieval.RETRIEVER_IMPLEMENTATION_VERSION,
        "production_api": "BrainService.retrieve_retrospective_evidence",
        "retrospective_retrieval_sha256": hashlib.sha256(
            implementation_sources["retrospective_retrieval"].encode()
        ).hexdigest(),
        "retrospective_retrieval_source": implementation_sources[
            "retrospective_retrieval"
        ],
        "runner_sha256": hashlib.sha256(
            implementation_sources["runner"].encode()
        ).hexdigest(),
        "runner_source": implementation_sources["runner"],
        "service_sha256": hashlib.sha256(
            implementation_sources["service"].encode()
        ).hexdigest(),
        "service_source": implementation_sources["service"],
    }
    implementation_raw = retrieval._canonical_json(implementation) + b"\n"
    _write_private(paths["implementation"], implementation_raw)
    snapshot_components = {
        "config_sha256": "1" * 64,
        "embedding_stamp_sha256": None,
        "lancedb_tree_sha256": "2" * 64,
        "sqlite_sha256": "3" * 64,
        "sqlite_wal_sha256": None,
    }
    live_components = {
        "config_sha256": "1" * 64,
        "embedding_stamp_sha256": None,
        "lancedb_tree_sha256": "2" * 64,
        "sqlite_sha256": "4" * 64,
        "sqlite_wal_sha256": None,
    }
    configuration = {
        "binding_sha256": binding_sha256 or manifest["source_binding_sha256"],
        "brain_read_only": True,
        "context_source_filter": "excluded_from_ranked_results",
        "fact_source_fusion": "ranked_facts_then_deterministic_source_chunks",
        "future_source_filter": "available_at_lte_query_as_of",
        "implementation_provenance_sha256": hashlib.sha256(
            implementation_raw
        ).hexdigest(),
        "index_components": snapshot_components,
        "live_source_index_artifact_sha256": hashlib.sha256(
            retrieval._canonical_json(live_components)
        ).hexdigest(),
        "live_source_index_components": live_components,
        "mode": "temporal",
        "production_retrieval_api": "BrainService.retrieve_retrospective_evidence",
        "production_retrieval_api_version": (retrieval.RETROSPECTIVE_RETRIEVAL_VERSION),
        "result_depth_policy": "zero_to_ten_no_padding",
        "retrieval_execution": "disposable_transactional_index_snapshot",
        "retrospective_retrieval_sha256": implementation[
            "retrospective_retrieval_sha256"
        ],
        "runner_sha256": implementation["runner_sha256"],
        "runner_version": retrieval.RETRIEVER_RUNNER_VERSION,
        "scratch_writes_discarded": True,
        "service_sha256": implementation["service_sha256"],
        "snapshot_index_artifact_sha256": hashlib.sha256(
            retrieval._canonical_json(snapshot_components)
        ).hexdigest(),
        "snapshot_index_components": snapshot_components,
        "source_recency_and_lineage": "disabled_for_as_of_replay_determinism",
        "telemetry_recorded": False,
        "temporal_fact_clock": (
            "source_availability_replay_via_complete_fact_citation_cutoff"
        ),
        "version": retrieval.RETRIEVER_CONFIG_VERSION,
    }
    _write_private(
        paths["configuration"], retrieval._canonical_json(configuration) + b"\n"
    )
    _write_private(paths["query_protocol"], retrieval._QUERY_PROTOCOL_BYTES)
    receipt = {
        "blind_primary_queries_sha256": manifest["artifact_sha256"][
            retrieval.PRIMARY_QUERY_ARTIFACT
        ],
        "index_artifact_sha256": "b" * 64,
        "index_components": snapshot_components,
        "snapshot_as_of": "2026-07-02T00:00:00Z",
        "source_authority_sha256": manifest["artifact_sha256"][
            retrieval.SOURCE_ARTIFACT
        ],
        "source_count": manifest["source_count"],
        "version": retrieval.INDEX_RECEIPT_VERSION,
    }
    receipt["index_artifact_sha256"] = configuration["snapshot_index_artifact_sha256"]
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
    assert score["primary"]["mean_reciprocal_rank_at_10"] == pytest.approx(
        (36 + 2 / 7) / 40
    )
    assert score["primary"]["macro_relevant_source_recall_at_5"] == 0.9
    assert score["primary"]["macro_relevant_source_recall_at_10"] == 0.95
    assert score["primary"]["complete_queries_at_10"] == 38
    assert score["primary"]["complete_query_recall_at_10"] == 0.95
    assert (
        score["primary"]["complete_query_recall_at_10_interval_95"]["numerator"] == 38
    )
    assert score["primary"]["minimum_result_depth"] == 10
    assert score["primary"]["maximum_result_depth"] == 10
    assert score["primary"]["mean_result_depth"] == 10.0
    assert score["primary"]["queries_with_no_results"] == 0
    kind_metrics = score["primary"]["temporal_query_kind_metrics"]
    assert set(kind_metrics) == retrieval._TEMPORAL_QUERY_KINDS
    assert kind_metrics["occurrence"]["queries"] == 7
    assert kind_metrics["occurrence"]["hits_at_10"] == 6
    lifecycle_metrics = score["primary"]["lifecycle_query_class_metrics"]
    assert set(lifecycle_metrics) == retrieval._LIFECYCLE_QUERY_CLASSES
    assert sum(item["queries"] for item in lifecycle_metrics.values()) == 7
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
    assert (
        bundle_manifest["source_binding_sha256"]
        == hashlib.sha256(paths["source_bindings"].read_bytes()).hexdigest()
    )
    assert (
        bundle_manifest["source_authority_manifest_sha256"]
        == hashlib.sha256(paths["source_authority_manifest"].read_bytes()).hexdigest()
    )
    assert bundle_manifest["primary_blind_context_source_ids_exposed"] is False
    assert bundle_manifest["blind_query_temporal_taxonomy_exposed"] is False
    assert bundle_manifest["challenge_blind_context_source_ids_exposed"] is True
    assert bundle_manifest["diagnostic_denominator"] == "primary_global_cold_only"
    run_manifest = json.loads((run / retrieval.MANIFEST_ARTIFACT).read_bytes())
    assert (
        run_manifest["source_binding_sha256"]
        == bundle_manifest["source_binding_sha256"]
    )
    assert (
        run_manifest["source_authority_manifest_sha256"]
        == bundle_manifest["source_authority_manifest_sha256"]
    )
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
        assert manifest["minimum_result_depth"] == 0
        assert manifest["maximum_result_depth"] == 10
        assert manifest["missing_result_ranks_scored_as_misses"] is True
    assert run_manifest["minimum_observed_result_depth"] == 10
    assert run_manifest["maximum_observed_result_depth"] == 10
    assert run_manifest["retrieved_result_count"] == 430
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


def test_freeze_rejects_swapped_source_authority_artifacts(tmp_path: Path) -> None:
    first = _write_fixture(tmp_path / "first", challenge_count=0)
    second = _write_fixture(tmp_path / "second", challenge_count=1)

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="source authority manifest policy",
    ):
        retrieval.freeze_retrieval_holdout(
            first["sources"],
            first["primary_queries"],
            first["key"],
            tmp_path / "swapped-bundle",
            source_bindings_path=second["source_bindings"],
            source_authority_manifest_path=first["source_authority_manifest"],
        )


def test_freeze_rejects_resigned_permuted_source_bindings(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bindings = retrieval._canonical_jsonl(
        paths["source_bindings"].read_bytes(), description="test"
    )
    bindings[0]["gmail_message_id"], bindings[1]["gmail_message_id"] = (
        bindings[1]["gmail_message_id"],
        bindings[0]["gmail_message_id"],
    )
    _write_private(paths["source_bindings"], _jsonl(bindings))
    _resign_source_authority_manifest(paths)

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="source binding chunk authority",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "permuted-bundle",
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
        )


def test_freeze_rejects_resigned_thread_scope_permutation(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    sources = retrieval._canonical_jsonl(
        paths["sources"].read_bytes(), description="test"
    )
    first = sources[0]
    second = next(
        row for row in sources if row["thread_scope_id"] != first["thread_scope_id"]
    )
    first["thread_scope_id"], second["thread_scope_id"] = (
        second["thread_scope_id"],
        first["thread_scope_id"],
    )
    sources.sort(key=lambda row: (row["thread_scope_id"], row["source_id"]))
    _write_private(paths["sources"], _jsonl(sources))
    _resign_source_authority_manifest(paths)

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="source binding authority",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "thread-permuted-bundle",
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
        )


def test_freeze_rejects_resigned_chunk_content_or_assignment_tampering(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bindings = retrieval._canonical_jsonl(
        paths["source_bindings"].read_bytes(), description="test"
    )
    bindings[0]["chunks"][0]["text_sha256"] = "f" * 64
    bindings[0]["chunks"], bindings[1]["chunks"] = (
        bindings[1]["chunks"],
        bindings[0]["chunks"],
    )
    _write_private(paths["source_bindings"], _jsonl(bindings))
    _resign_source_authority_manifest(paths)

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="source binding chunk authority",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "chunk-tampered-bundle",
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
        )


@pytest.mark.parametrize(
    ("field", "replacement", "error"),
    (
        ("source_scope", "selected_messages_only", "manifest policy"),
        ("message_count", 999_999, "manifest counts"),
    ),
)
def test_freeze_rejects_resigned_source_authority_scope_and_count_changes(
    tmp_path: Path,
    field: str,
    replacement: Any,
    error: str,
) -> None:
    paths = _write_fixture(tmp_path / field, challenge_count=0)
    manifest = json.loads(paths["source_authority_manifest"].read_bytes())
    manifest[field] = replacement
    _write_private(
        paths["source_authority_manifest"],
        retrieval._canonical_json(manifest) + b"\n",
    )
    _resign_source_authority_manifest(paths)

    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError, match=error):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / f"{field}-bundle",
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
        )


def test_seal_rejects_swapped_source_binding_and_noncanonical_configuration(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    manifest = json.loads((bundle / retrieval.MANIFEST_ARTIFACT).read_bytes())

    swapped_raw = b'{"source_id":"swapped-private-binding"}\n'
    _write_private(paths["source_bindings"], swapped_raw)
    swapped_provenance = _write_provenance(
        tmp_path / "swapped-provenance",
        bundle,
        binding_sha256=hashlib.sha256(swapped_raw).hexdigest(),
    )
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="retriever configuration contract",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            swapped_provenance,
            tmp_path / "swapped-run",
        )

    canonical_provenance = _write_provenance(
        tmp_path / "noncanonical-provenance", bundle
    )
    configuration = {
        "binding_sha256": manifest["source_binding_sha256"],
        "mode": "temporal",
    }
    _write_private(
        canonical_provenance["configuration"],
        json.dumps(configuration, indent=2, sort_keys=True).encode() + b"\n",
    )
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="retriever configuration contract",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            canonical_provenance,
            tmp_path / "noncanonical-run",
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("brain_read_only", False),
        ("telemetry_recorded", True),
        ("runner_version", "unapproved-runner"),
        ("mode", "hybrid"),
        ("snapshot_index_artifact_sha256", "f" * 64),
    ),
)
def test_seal_rejects_non_runner_configuration_contracts(
    tmp_path: Path, field: str, replacement: Any
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    configuration = json.loads(provenance["configuration"].read_bytes())
    configuration[field] = replacement
    _write_private(
        provenance["configuration"], retrieval._canonical_json(configuration) + b"\n"
    )

    with pytest.raises(retrieval.GmailTemporalRetrievalHoldoutError):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "invalid-configuration-run",
        )


def test_seal_rejects_extra_configuration_fields(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    configuration = json.loads(provenance["configuration"].read_bytes())
    configuration["untrusted_override"] = True
    _write_private(
        provenance["configuration"], retrieval._canonical_json(configuration) + b"\n"
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="configuration contract",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "extra-configuration-run",
        )


def test_seal_rejects_fabricated_implementation_and_query_protocol(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    implementation_provenance = _write_provenance(
        tmp_path / "implementation-provenance", bundle
    )
    implementation = json.loads(
        implementation_provenance["implementation"].read_bytes()
    )
    implementation["runner_source"] += "# uncommitted behavior\n"
    _write_private(
        implementation_provenance["implementation"],
        retrieval._canonical_json(implementation) + b"\n",
    )
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="implementation provenance",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            implementation_provenance,
            tmp_path / "fabricated-implementation-run",
        )

    protocol_provenance = _write_provenance(tmp_path / "protocol-provenance", bundle)
    _write_private(
        protocol_provenance["query_protocol"],
        retrieval._QUERY_PROTOCOL_BYTES + b"override=true\n",
    )
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="query protocol",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            protocol_provenance,
            tmp_path / "fabricated-protocol-run",
        )


@pytest.mark.parametrize(
    "component",
    ("runner", "service", "retrospective_retrieval"),
)
def test_seal_rejects_self_consistent_forged_production_source(
    tmp_path: Path,
    component: str,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "forged-provenance", bundle)
    implementation = json.loads(provenance["implementation"].read_bytes())
    implementation[f"{component}_source"] += "# forged production behavior\n"
    implementation[f"{component}_sha256"] = hashlib.sha256(
        implementation[f"{component}_source"].encode()
    ).hexdigest()
    implementation_raw = retrieval._canonical_json(implementation) + b"\n"
    _write_private(provenance["implementation"], implementation_raw)

    configuration = json.loads(provenance["configuration"].read_bytes())
    configuration[f"{component}_sha256"] = implementation[f"{component}_sha256"]
    configuration["implementation_provenance_sha256"] = hashlib.sha256(
        implementation_raw
    ).hexdigest()
    _write_private(
        provenance["configuration"], retrieval._canonical_json(configuration) + b"\n"
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="authoritative production implementation",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / f"forged-{component}-run",
        )


def test_git_implementation_authority_accepts_exact_clean_head_blobs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clean-repository"
    expected = _git_implementation_fixture(root)

    assert (
        retrieval._verified_git_implementation_sources(
            root,
            retrieval._GIT_IMPLEMENTATION_PATHS,
        )
        == expected
    )


@pytest.mark.parametrize(
    "component",
    ("runner", "service", "retrospective_retrieval"),
)
def test_git_implementation_authority_rejects_hidden_dirty_source(
    tmp_path: Path,
    component: str,
) -> None:
    root = tmp_path / f"dirty-{component}"
    _git_implementation_fixture(root)
    relative_path = retrieval._GIT_IMPLEMENTATION_PATHS[component]
    _run_git(root, "update-index", "--assume-unchanged", "--", relative_path)
    path = root / relative_path
    path.write_bytes(path.read_bytes() + b"# self-consistent dirty behavior\n")

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="does not match authoritative Git HEAD",
    ):
        retrieval._verified_git_implementation_sources(
            root,
            retrieval._GIT_IMPLEMENTATION_PATHS,
        )


def test_git_implementation_authority_rejects_untracked_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "untracked-repository"
    _git_implementation_fixture(root, untracked_role="runner")

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="working tree is dirty",
    ):
        retrieval._verified_git_implementation_sources(
            root,
            retrieval._GIT_IMPLEMENTATION_PATHS,
        )


@pytest.mark.parametrize("tamper", ("artifact", "components"))
def test_seal_cross_checks_receipt_against_snapshot_commitments(
    tmp_path: Path, tamper: str
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    receipt = json.loads(provenance["index_receipt"].read_bytes())
    if tamper == "artifact":
        receipt["index_artifact_sha256"] = "f" * 64
    else:
        receipt["index_components"]["sqlite_sha256"] = "f" * 64
    _write_private(
        provenance["index_receipt"], retrieval._canonical_json(receipt) + b"\n"
    )

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="index receipt",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / f"invalid-receipt-{tamper}-run",
        )


def test_source_binding_commitment_is_authenticated(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    manifest_path = bundle / retrieval.MANIFEST_ARTIFACT
    manifest = json.loads(manifest_path.read_bytes())
    manifest["source_binding_sha256"] = "f" * 64
    _write_private(manifest_path, retrieval._canonical_json(manifest) + b"\n")

    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="authentication failed",
    ):
        retrieval._load_bundle(bundle, key=paths["key"].read_bytes())


def test_variable_result_depth_is_scored_and_rank_contract_stays_strict(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    original = retrieval._canonical_jsonl(
        paths["primary_results"].read_bytes(), description="test"
    )

    shortened = json.loads(json.dumps(original))
    shortened[0]["retrieved"] = []
    shortened[1]["retrieved"] = shortened[1]["retrieved"][:3]
    _write_private(paths["primary_results"], _jsonl(shortened))
    run = tmp_path / "run"
    sealed = retrieval.seal_retrieval_run(
        bundle,
        paths["primary_results"],
        paths["key"],
        provenance,
        run,
    )
    score_root = tmp_path / "score"
    result = retrieval.score_retrieval_holdout(bundle, run, paths["key"], score_root)
    score = json.loads((score_root / retrieval.SCORE_ARTIFACT).read_text())

    assert sealed["minimum_observed_result_depth"] == 0
    assert sealed["maximum_observed_result_depth"] == 10
    assert sealed["retrieved_result_count"] == 383
    assert result["query_hit_rate_at_5"] == 0.875
    assert result["query_hit_rate_at_10"] == 0.925
    assert score["primary"]["queries_with_no_results"] == 1
    assert score["primary"]["minimum_result_depth"] == 0
    assert score["primary"]["maximum_result_depth"] == 10
    assert score["primary"]["mean_result_depth"] == 383 / 40
    assert score["primary"]["macro_relevant_source_recall_at_10"] == 0.925
    assert score["primary"]["complete_query_recall_at_10"] == 0.925
    assert score["primary"]["mean_reciprocal_rank_at_10"] == pytest.approx(
        (35 + 2 / 7) / 40
    )

    over_depth = json.loads(json.dumps(original))
    thread_id = over_depth[0]["query_id"].replace("query", "thread")
    over_depth[0]["retrieved"].append(
        {
            "rank": 11,
            "source_id": f"{thread_id}-source-private-10",
        }
    )
    _write_private(paths["primary_results"], _jsonl(over_depth))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="result coverage",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "run-over-depth",
        )

    rank_gap = json.loads(json.dumps(original))
    rank_gap[0]["retrieved"] = rank_gap[0]["retrieved"][:3]
    rank_gap[0]["retrieved"][2]["rank"] = 4
    _write_private(paths["primary_results"], _jsonl(rank_gap))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="ranking",
    ):
        retrieval.seal_retrieval_run(
            bundle,
            paths["primary_results"],
            paths["key"],
            provenance,
            tmp_path / "run-rank-gap",
        )

    manifest_path = run / retrieval.MANIFEST_ARTIFACT
    manifest = json.loads(manifest_path.read_bytes())
    manifest.pop("manifest_hmac_sha256")
    manifest["retrieved_result_count"] += 1
    authenticator = hmac.new(
        paths["key"].read_bytes(),
        retrieval.RUN_MANIFEST_DOMAIN + retrieval._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        retrieval._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="result-depth policy",
    ):
        retrieval.score_retrieval_holdout(
            bundle, run, paths["key"], tmp_path / "tampered-depth-score"
        )


def test_multi_source_gold_reports_micro_macro_and_complete_recall(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    bindings = retrieval._canonical_jsonl(
        paths["source_bindings"].read_bytes(), description="test"
    )
    second_relevant_source = next(
        row["source_id"]
        for row in bindings
        if row["gmail_message_id"] == "primary-message-private-000-10"
    )
    queries[0]["relevant_source_ids"].append(second_relevant_source)
    queries[0]["relevant_source_ids"].sort()
    _write_private(paths["primary_queries"], _jsonl(queries))
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    run = tmp_path / "run"
    retrieval.seal_retrieval_run(
        bundle,
        paths["primary_results"],
        paths["key"],
        provenance,
        run,
    )
    score_root = tmp_path / "score"
    retrieval.score_retrieval_holdout(bundle, run, paths["key"], score_root)
    metrics = json.loads((score_root / retrieval.SCORE_ARTIFACT).read_text())["primary"]

    assert metrics["relevant_sources"] == 41
    assert metrics["relevant_source_recall_at_10"] == 38 / 41
    assert metrics["macro_relevant_source_recall_at_10"] == 37.5 / 40
    assert metrics["complete_queries_at_10"] == 37
    assert metrics["complete_query_recall_at_10"] == 37 / 40
    assert metrics["mean_reciprocal_rank_at_10"] == pytest.approx((36 + 2 / 7) / 40)


def test_query_hits_cannot_hide_low_multi_source_recall(tmp_path: Path) -> None:
    paths = _write_fixture(tmp_path / "inputs", challenge_count=0)
    queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    bindings = retrieval._canonical_jsonl(
        paths["source_bindings"].read_bytes(), description="test"
    )
    source_by_message = {row["gmail_message_id"]: row["source_id"] for row in bindings}
    for index, query in enumerate(queries[:5]):
        query["relevant_source_ids"].append(
            source_by_message[f"primary-message-private-{index:03d}-10"]
        )
        query["relevant_source_ids"].sort()
    _write_private(paths["primary_queries"], _jsonl(queries))
    bundle = tmp_path / "bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
    )
    provenance = _write_provenance(tmp_path / "provenance", bundle)
    run = tmp_path / "run"
    retrieval.seal_retrieval_run(
        bundle,
        paths["primary_results"],
        paths["key"],
        provenance,
        run,
    )
    score_root = tmp_path / "score"
    result = retrieval.score_retrieval_holdout(bundle, run, paths["key"], score_root)
    score = json.loads((score_root / retrieval.SCORE_ARTIFACT).read_text())

    assert result["query_hit_rate_at_5"] == 0.9
    assert result["query_hit_rate_at_10"] == 0.95
    assert result["macro_relevant_source_recall_at_10"] == 0.8875
    assert result["complete_query_recall_at_10"] == 0.825
    assert result["retrieval_gate_passed"] is False
    assert score["top_5_gate_passed"] is True
    assert score["top_10_gate_passed"] is True
    assert score["macro_relevant_source_recall_at_10_gate_passed"] is False
    assert score["complete_query_recall_at_10_gate_passed"] is False


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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
            challenge_queries_path=paths["challenge_queries"],
        )


def test_primary_cold_context_may_be_empty_but_challenge_context_may_not(
    tmp_path: Path,
) -> None:
    paths = _write_fixture(tmp_path / "empty-primary-context")
    primary_queries = retrieval._canonical_jsonl(
        paths["primary_queries"].read_bytes(), description="test"
    )
    for query in primary_queries:
        query["context_source_ids"] = []
    _write_private(paths["primary_queries"], _jsonl(primary_queries))
    bundle = tmp_path / "empty-primary-context-bundle"

    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
        challenge_queries_path=paths["challenge_queries"],
    )

    sealed_primary = retrieval._canonical_jsonl(
        (bundle / retrieval.PRIMARY_GOLD_ARTIFACT).read_bytes(),
        description="test",
    )
    assert all(row["context_source_ids"] == [] for row in sealed_primary)

    paths = _write_fixture(tmp_path / "empty-challenge-context")
    challenge_queries = retrieval._canonical_jsonl(
        paths["challenge_queries"].read_bytes(), description="test"
    )
    challenge_queries[0]["context_source_ids"] = []
    _write_private(paths["challenge_queries"], _jsonl(challenge_queries))
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="query context authority",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "empty-challenge-context-bundle",
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
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
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
    bindings = retrieval._canonical_jsonl(
        paths["source_bindings"].read_bytes(), description="test"
    )
    next(row for row in bindings if row["source_id"] == context_id)["available_at"] = (
        "2026-07-03T00:00:00Z"
    )
    _write_private(paths["source_bindings"], _jsonl(bindings))
    _resign_source_authority_manifest(paths)
    with pytest.raises(
        retrieval.GmailTemporalRetrievalHoldoutError,
        match="future evidence",
    ):
        retrieval.freeze_retrieval_holdout(
            paths["sources"],
            paths["primary_queries"],
            paths["key"],
            tmp_path / "future-bundle",
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
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
    bindings = retrieval._canonical_jsonl(
        paths["source_bindings"].read_bytes(), description="test"
    )
    next(row for row in bindings if row["source_id"] == future_source["source_id"])[
        "available_at"
    ] = "2026-07-03T00:00:00Z"
    _write_private(paths["source_bindings"], _jsonl(bindings))
    _resign_source_authority_manifest(paths)
    _write_private(paths["primary_results"], _jsonl(results))
    future_bundle = tmp_path / "future-bundle"
    retrieval.freeze_retrieval_holdout(
        paths["sources"],
        paths["primary_queries"],
        paths["key"],
        future_bundle,
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
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
        source_bindings_path=paths["source_bindings"],
        source_authority_manifest_path=paths["source_authority_manifest"],
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
        source_bindings_path=second_paths["source_bindings"],
        source_authority_manifest_path=second_paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
            source_bindings_path=paths["source_bindings"],
            source_authority_manifest_path=paths["source_authority_manifest"],
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
    source_bindings = secret / "source-bindings"
    source_authority_manifest = secret / "source-authority-manifest"
    queries = secret / "queries"
    _write_private(key, b"x" * 32)
    _write_private(sources, b"not-json\n")
    _write_private(source_bindings, b"not-json\n")
    _write_private(source_authority_manifest, b"not-json\n")
    _write_private(queries, b"not-json\n")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "freeze",
            "--source-authority",
            str(sources),
            "--source-bindings",
            str(source_bindings),
            "--source-authority-manifest",
            str(source_authority_manifest),
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
