"""Build a frozen run manifest for a completed Gmail temporal checkpoint.

The builder is intentionally local-only.  It validates private synthetic evidence,
replays the deterministic candidate frontier, and publishes only a provenance
manifest.  It never sends or prints source content.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any


class CandidateRunManifestError(ValueError):
    """Raised when checkpoint evidence cannot be frozen safely."""


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVALUATOR_PATH = _REPO_ROOT / "scripts" / "evaluate_gmail_temporal_candidate_gold.py"


def _load_evaluator() -> ModuleType:
    module_name = "_gmail_temporal_candidate_gold_manifest_evaluator"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, _EVALUATOR_PATH)
    if spec is None or spec.loader is None:
        raise CandidateRunManifestError("candidate evaluator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


candidate_gold = _load_evaluator()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CandidateRunManifestError(
            "evidence artifact could not be fingerprinted"
        ) from exc


def _load_private_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return candidate_gold._load_jsonl(path)
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateRunManifestError(str(exc)) from exc


def _validate_private_input(path: Path) -> None:
    try:
        candidate_gold._validate_private_artifact(path)
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateRunManifestError(str(exc)) from exc


def _checkpoint_provenance(
    rows: list[dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    first = rows[0]
    if set(first) != candidate_gold._CHECKPOINT_KEYS:
        raise CandidateRunManifestError("checkpoint schema is stale")
    protocol = first.get("protocol_fingerprint")
    source_hashes = first.get("source_module_sha256")
    if (
        first.get("version") != candidate_gold.EXPECTED_CHECKPOINT_VERSION
        or not isinstance(protocol, str)
        or candidate_gold._PROTOCOL_PATTERN.fullmatch(protocol) is None
        or not isinstance(source_hashes, Mapping)
        or set(source_hashes) != candidate_gold._PROVENANCE_MODULE_KEYS
        or any(
            not isinstance(digest, str)
            or candidate_gold._SHA256_PATTERN.fullmatch(digest) is None
            for digest in source_hashes.values()
        )
    ):
        raise CandidateRunManifestError(
            "checkpoint provenance is invalid or unsupported"
        )
    normalized_hashes = {
        str(name): str(digest) for name, digest in sorted(source_hashes.items())
    }
    seen_pages: set[str] = set()
    for row in rows:
        page_fingerprint = row.get("page_fingerprint")
        if set(row) != candidate_gold._CHECKPOINT_KEYS:
            raise CandidateRunManifestError("checkpoint schema is stale")
        if (
            row.get("version") != candidate_gold.EXPECTED_CHECKPOINT_VERSION
            or row.get("protocol_fingerprint") != protocol
            or row.get("source_module_sha256") != normalized_hashes
        ):
            raise CandidateRunManifestError(
                "checkpoint rows have incoherent provenance"
            )
        if (
            not isinstance(page_fingerprint, str)
            or not page_fingerprint
            or page_fingerprint in seen_pages
        ):
            raise CandidateRunManifestError(
                "checkpoint page fingerprints are invalid or duplicated"
            )
        seen_pages.add(page_fingerprint)

    try:
        current_hashes = candidate_gold._current_repo_module_hashes()
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateRunManifestError(str(exc)) from exc
    if any(
        normalized_hashes[name] != digest for name, digest in current_hashes.items()
    ):
        raise CandidateRunManifestError(
            "checkpoint source modules do not match this checkout"
        )
    return protocol, normalized_hashes


def _validate_completed_checkpoint(
    samples: list[dict[str, Any]],
    checkpoint_rows: list[dict[str, Any]],
    *,
    protocol: str,
    source_hashes: Mapping[str, str],
) -> tuple[int, Counter[str]]:
    try:
        runtime_batches, candidates, pages = candidate_gold._runtime_batches(samples)
        units = candidate_gold._compile_gold(samples, candidates)
        if not units:
            raise CandidateRunManifestError("benchmark contains no semantic units")
        provisional_manifest = candidate_gold.RunManifest(
            checkpoint_version=candidate_gold.EXPECTED_CHECKPOINT_VERSION,
            protocol_fingerprint=protocol,
            model=candidate_gold.EXPECTED_MODEL,
            reasoning_effort=candidate_gold.EXPECTED_REASONING_EFFORT,
            source_module_sha256=dict(source_hashes),
            evaluator_sha256="0" * 64,
            semantic_gold_sha256="0" * 64,
            benchmark_builder_sha256="0" * 64,
            sample_sha256="0" * 64,
            sample_record_count=len(samples),
            checkpoint_sha256="0" * 64,
            checkpoint_row_count=len(checkpoint_rows),
        )
        _, effective_verdicts = candidate_gold._checkpoint_verdicts(
            checkpoint_rows,
            runtime_batches,
            pages,
            provisional_manifest,
        )
    except CandidateRunManifestError:
        raise
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateRunManifestError(str(exc)) from exc
    return len(units), Counter(effective_verdicts.values())


def _publish_private_json_no_replace(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise CandidateRunManifestError(
            "output path already exists; choose a new output path"
        )
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise CandidateRunManifestError(
            "output parent must be an existing non-symlink directory"
        )
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary_path = Path(raw_temporary_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CandidateRunManifestError(
                "output path already exists; manifest was not overwritten"
            ) from exc
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise CandidateRunManifestError("published manifest is not mode 0600")
    except OSError as exc:
        raise CandidateRunManifestError("manifest could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def build_run_manifest(
    sample_path: Path,
    checkpoint_path: Path,
    output_path: Path,
) -> dict[str, int | bool | str]:
    resolved_inputs = {sample_path.resolve(), checkpoint_path.resolve()}
    if len(resolved_inputs) != 2 or output_path.resolve() in resolved_inputs:
        raise CandidateRunManifestError(
            "sample, checkpoint, and output paths must be distinct"
        )
    if output_path.exists() or output_path.is_symlink():
        raise CandidateRunManifestError(
            "output path already exists; choose a new output path"
        )

    _validate_private_input(sample_path)
    _validate_private_input(checkpoint_path)
    initial_sample_sha256 = _sha256_file(sample_path)
    initial_checkpoint_sha256 = _sha256_file(checkpoint_path)
    initial_artifact_hashes = {
        "evaluator_sha256": _sha256_file(candidate_gold._EVALUATOR_PATH),
        "semantic_gold_sha256": _sha256_file(candidate_gold._SEMANTIC_GOLD_PATH),
        "benchmark_builder_sha256": _sha256_file(
            candidate_gold._BENCHMARK_BUILDER_PATH
        ),
    }
    samples = _load_private_jsonl(sample_path)
    checkpoint_rows = _load_private_jsonl(checkpoint_path)
    protocol, source_hashes = _checkpoint_provenance(checkpoint_rows)
    semantic_unit_count, verdict_counts = _validate_completed_checkpoint(
        samples,
        checkpoint_rows,
        protocol=protocol,
        source_hashes=source_hashes,
    )

    final_sample_sha256 = _sha256_file(sample_path)
    final_checkpoint_sha256 = _sha256_file(checkpoint_path)
    final_source_hashes = candidate_gold._current_repo_module_hashes()
    final_artifact_hashes = {
        "evaluator_sha256": _sha256_file(candidate_gold._EVALUATOR_PATH),
        "semantic_gold_sha256": _sha256_file(candidate_gold._SEMANTIC_GOLD_PATH),
        "benchmark_builder_sha256": _sha256_file(
            candidate_gold._BENCHMARK_BUILDER_PATH
        ),
    }
    if (
        final_sample_sha256 != initial_sample_sha256
        or final_checkpoint_sha256 != initial_checkpoint_sha256
        or final_artifact_hashes != initial_artifact_hashes
        or any(
            source_hashes[name] != digest
            for name, digest in final_source_hashes.items()
        )
    ):
        raise CandidateRunManifestError(
            "evidence or source modules changed during validation"
        )
    _validate_private_input(sample_path)
    _validate_private_input(checkpoint_path)

    manifest = {
        "version": candidate_gold.RUN_MANIFEST_VERSION,
        "checkpoint_version": candidate_gold.EXPECTED_CHECKPOINT_VERSION,
        "protocol_fingerprint": protocol,
        "model": candidate_gold.EXPECTED_MODEL,
        "reasoning_effort": candidate_gold.EXPECTED_REASONING_EFFORT,
        "source_module_sha256": dict(sorted(source_hashes.items())),
        **initial_artifact_hashes,
        "sample_sha256": final_sample_sha256,
        "sample_record_count": len(samples),
        "checkpoint_sha256": final_checkpoint_sha256,
        "checkpoint_row_count": len(checkpoint_rows),
    }
    if set(manifest) != candidate_gold._RUN_MANIFEST_KEYS:
        raise CandidateRunManifestError("internal run manifest schema is invalid")
    _publish_private_json_no_replace(output_path, manifest)
    return {
        "version": candidate_gold.RUN_MANIFEST_VERSION,
        "sample_records": len(samples),
        "checkpoint_pages": len(checkpoint_rows),
        "semantic_units": semantic_unit_count,
        "candidate_verdicts": sum(verdict_counts.values()),
        "supported_verdicts": verdict_counts["supported"],
        "uncertain_verdicts": verdict_counts["uncertain"],
        "unsupported_verdicts": verdict_counts["unsupported"],
        "private_content_printed": False,
        "external_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            build_run_manifest(args.sample, args.checkpoint, args.output),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
