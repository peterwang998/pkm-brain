from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_script(module_name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


manifest_builder = _load_script(
    "gmail_temporal_candidate_run_manifest_builder",
    "build_gmail_temporal_candidate_run_manifest.py",
)
candidate_gold = manifest_builder.candidate_gold
benchmark_builder = _load_script(
    "gmail_temporal_candidate_run_manifest_benchmark_builder",
    "build_gmail_temporal_synthetic_benchmark.py",
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _provenance() -> dict[str, Any]:
    source_hashes = candidate_gold._current_repo_module_hashes()
    source_hashes.update({"runner": "a" * 64, "base_runner": "b" * 64})
    return {
        "checkpoint_version": candidate_gold.EXPECTED_CHECKPOINT_VERSION,
        "protocol_fingerprint": "gtfproto_" + "c" * 64,
        "source_module_sha256": dict(sorted(source_hashes.items())),
    }


def _checkpoint_rows(
    pages: dict[str, tuple[Any, Any]],
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    verdict_cycle = ("supported", "uncertain", "unsupported")
    verdict_sequence = 0
    for page_fingerprint, (runtime_batch, page) in pages.items():
        verdicts: list[dict[str, str]] = []
        for cluster in page.clusters:
            for candidate_id in cluster.candidate_ids:
                verdicts.append(
                    {
                        "candidate_id": candidate_id,
                        "verdict": verdict_cycle[verdict_sequence % len(verdict_cycle)],
                    }
                )
                verdict_sequence += 1
        rows.append(
            {
                "version": provenance["checkpoint_version"],
                "sample_id": runtime_batch.sample_id,
                "source_sha256": runtime_batch.analysis.source_sha256,
                "protocol_fingerprint": provenance["protocol_fingerprint"],
                "source_module_sha256": provenance["source_module_sha256"],
                "plan_fingerprint": runtime_batch.plan_fingerprint,
                "page_case_id": candidate_gold._page_case_id(runtime_batch, page),
                "batch_fingerprint": runtime_batch.batch.manifest.batch_fingerprint,
                "analysis_fingerprint": runtime_batch.analysis.snapshot_fingerprint,
                "frontier_fingerprint": page.frontier_fingerprint,
                "page_fingerprint": page_fingerprint,
                "candidate_page_plan_fingerprint": (
                    runtime_batch.candidate_page_plan_fingerprint
                ),
                "candidate_page_payload_bytes": dict(
                    runtime_batch.candidate_page_payload_bytes
                )[page_fingerprint],
                "batch_sequence": runtime_batch.batch.sequence,
                "page_sequence": page.sequence,
                "page_count": len(runtime_batch.pages),
                "verdicts": verdicts,
            }
        )
    return rows


def _fixture(tmp_path: Path) -> dict[str, Any]:
    sample = tmp_path / "sample.jsonl"
    checkpoint = tmp_path / "checkpoint.jsonl"
    output = tmp_path / "run-manifest.json"
    benchmark_builder.build(sample)
    samples = candidate_gold._load_jsonl(sample)
    _, _, pages = candidate_gold._runtime_batches(samples)
    rows = _checkpoint_rows(pages, _provenance())
    _write_jsonl(checkpoint, rows)
    return {
        "sample": sample,
        "checkpoint": checkpoint,
        "output": output,
        "samples": samples,
        "pages": pages,
        "rows": rows,
    }


def test_builder_freezes_valid_completed_checkpoint_for_candidate_evaluator(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)

    result = manifest_builder.build_run_manifest(
        files["sample"],
        files["checkpoint"],
        files["output"],
    )

    assert result["sample_records"] == len(files["samples"])
    assert result["checkpoint_pages"] == len(files["pages"])
    assert result["candidate_verdicts"] == sum(
        len(row["verdicts"]) for row in files["rows"]
    )
    assert result["private_content_printed"] is False
    assert result["external_calls"] == 0
    assert stat.S_IMODE(files["output"].stat().st_mode) == 0o600

    manifest = json.loads(files["output"].read_text(encoding="utf-8"))
    assert set(manifest) == candidate_gold._RUN_MANIFEST_KEYS
    assert manifest["sample_record_count"] == len(files["samples"])
    assert manifest["checkpoint_row_count"] == len(files["pages"])
    assert manifest["sample_sha256"] == candidate_gold._sha256_file(files["sample"])
    assert manifest["checkpoint_sha256"] == candidate_gold._sha256_file(
        files["checkpoint"]
    )
    assert manifest["evaluator_sha256"] == candidate_gold._sha256_file(
        candidate_gold._EVALUATOR_PATH
    )
    assert manifest["semantic_gold_sha256"] == candidate_gold._sha256_file(
        candidate_gold._SEMANTIC_GOLD_PATH
    )
    assert manifest["benchmark_builder_sha256"] == candidate_gold._sha256_file(
        candidate_gold._BENCHMARK_BUILDER_PATH
    )
    evaluated = candidate_gold.evaluate(
        files["sample"],
        files["checkpoint"],
        files["output"],
    )
    assert evaluated["records"] == len(files["samples"])
    assert result["supported_verdicts"] == evaluated["supported_candidates"]
    assert result["uncertain_verdicts"] == evaluated["uncertain_candidates"]
    assert result["unsupported_verdicts"] == (
        result["candidate_verdicts"]
        - evaluated["supported_candidates"]
        - evaluated["uncertain_candidates"]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows[0].update({"extra": "not-bound"}), "schema is stale"),
        (
            lambda rows: rows[1].update(
                {"protocol_fingerprint": "gtfproto_" + "d" * 64}
            ),
            "incoherent provenance",
        ),
        (
            lambda rows: rows[1].update(
                {
                    "source_module_sha256": {
                        **rows[1]["source_module_sha256"],
                        "runner": "e" * 64,
                    }
                }
            ),
            "incoherent provenance",
        ),
        (
            lambda rows: rows[1].update(
                {"page_fingerprint": rows[0]["page_fingerprint"]}
            ),
            "duplicated",
        ),
        (lambda rows: rows.pop(), "does not cover the page cohort exactly"),
    ],
)
def test_builder_rejects_stale_incoherent_duplicate_or_incomplete_checkpoint(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    files = _fixture(tmp_path)
    mutation(files["rows"])
    _write_jsonl(files["checkpoint"], files["rows"])

    with pytest.raises(manifest_builder.CandidateRunManifestError, match=message):
        manifest_builder.build_run_manifest(
            files["sample"],
            files["checkpoint"],
            files["output"],
        )

    assert not files["output"].exists()


def test_builder_rejects_checkpoint_from_different_current_source(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    source_hashes = dict(files["rows"][0]["source_module_sha256"])
    source_hashes["pkm_brain.gmail_temporal_frontier"] = "f" * 64
    for row in files["rows"]:
        row["source_module_sha256"] = source_hashes
    _write_jsonl(files["checkpoint"], files["rows"])

    with pytest.raises(
        manifest_builder.CandidateRunManifestError,
        match="do not match this checkout",
    ):
        manifest_builder.build_run_manifest(
            files["sample"],
            files["checkpoint"],
            files["output"],
        )


@pytest.mark.parametrize("private_input", ["sample", "checkpoint"])
def test_builder_requires_mode_0600_inputs(
    tmp_path: Path,
    private_input: str,
) -> None:
    files = _fixture(tmp_path)
    os.chmod(files[private_input], 0o644)

    with pytest.raises(
        manifest_builder.CandidateRunManifestError,
        match="mode 0600",
    ):
        manifest_builder.build_run_manifest(
            files["sample"],
            files["checkpoint"],
            files["output"],
        )

    assert not files["output"].exists()


def test_builder_never_overwrites_existing_output(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    files["output"].write_text("preserve-me\n", encoding="utf-8")
    files["output"].chmod(0o600)

    with pytest.raises(
        manifest_builder.CandidateRunManifestError,
        match="already exists",
    ):
        manifest_builder.build_run_manifest(
            files["sample"],
            files["checkpoint"],
            files["output"],
        )

    assert files["output"].read_text(encoding="utf-8") == "preserve-me\n"


def test_cli_prints_aggregates_without_source_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = _fixture(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_gmail_temporal_candidate_run_manifest.py",
            str(files["sample"]),
            str(files["checkpoint"]),
            str(files["output"]),
        ],
    )

    manifest_builder.main()

    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {
        "version",
        "sample_records",
        "checkpoint_pages",
        "semantic_units",
        "candidate_verdicts",
        "supported_verdicts",
        "uncertain_verdicts",
        "unsupported_verdicts",
        "private_content_printed",
        "external_calls",
    }
    assert printed["private_content_printed"] is False
    assert printed["external_calls"] == 0
