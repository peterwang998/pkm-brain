#!/usr/bin/env python3
"""Prepare a complete, private Gmail fact-parity labeling bundle.

Without ``--completed-units`` this command freezes one work item for every
packet and every emitted original/V2 member.  With a completed semantic-unit
file, it refuses partial alignment and emits the exact alignment, labels, and
manifest accepted by the v3 evaluator.  A completed file requires a bound
external Codex Sol/medium judge receipt.  All written artifacts are owner-only;
stdout contains aggregate counts and digests only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
from pathlib import Path
from typing import Any, Mapping


VERSION = "gmail_fact_parity_preparation_v1"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVALUATOR_PATH = _REPO_ROOT / "scripts" / "evaluate_gmail_fact_parity.py"
_SPEC = importlib.util.spec_from_file_location(
    "prepare_gmail_fact_parity_evaluator", _EVALUATOR_PATH
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("fact parity evaluator could not be loaded")
evaluator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(evaluator)


class GmailFactParityPreparationError(ValueError):
    """Raised when a private parity bundle cannot be frozen safely."""


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(PRIVATE_FILE_MODE)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GmailFactParityPreparationError(
            "private preparation artifact write failed"
        ) from exc


def _publish_frozen_artifacts(
    output_root: Path, artifacts: Mapping[str, bytes]
) -> None:
    if output_root.exists() or output_root.is_symlink():
        raise GmailFactParityPreparationError(
            "frozen output path already exists; choose a new output path"
        )
    parent = output_root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailFactParityPreparationError("output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    staging = parent / (f".{output_root.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    try:
        staging.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        for name, payload in artifacts.items():
            _write_private_new(staging / name, payload)
        staging.replace(output_root)
    except Exception:
        if staging.is_dir() and not staging.is_symlink():
            for name in artifacts:
                artifact = staging / name
                if artifact.is_file() and not artifact.is_symlink():
                    artifact.unlink()
            try:
                staging.rmdir()
            except OSError:
                pass
        raise


def _preparation_manifest(
    *,
    release_ready: bool,
    evidence: Mapping[str, Any],
    run_evidence: Mapping[str, Any],
    artifacts: Mapping[str, bytes],
) -> dict[str, Any]:
    return {
        "version": VERSION,
        "release_evidence_ready": release_ready,
        "cohort_sha256": evidence["cohort_sha256"],
        "packet_sha256": evidence["packet_sha256"],
        "cohort_manifest_sha256": evidence["cohort_manifest_sha256"],
        "packet_count": evidence["packet_count"],
        "thread_count": evidence["thread_count"],
        "message_count": evidence["message_count"],
        "run_count": len(run_evidence["all_run_ids"]),
        "v2_run_count": len(run_evidence["v2_run_ids"]),
        "member_count": sum(
            len(run_evidence["runs"][run_id]["members"])
            for run_id in run_evidence["all_run_ids"]
        ),
        "artifact_sha256": {
            name: evaluator._sha256_bytes(payload)  # noqa: SLF001
            for name, payload in sorted(artifacts.items())
        },
        "claimed_invocation_ids_unique": True,
        "independent_invocations_verified": False,
        "invocation_limitation": evaluator.INVOCATION_ATTESTATION,
        "private_content_printed": False,
        "external_calls": 0,
    }


def prepare_gmail_fact_parity_evaluation(
    packets_path: Path,
    cohort_path: Path,
    admissions_path: Path,
    cohort_manifest_path: Path,
    original_inventory_path: Path,
    v2_inventory_path: Path,
    output_root: Path,
    *,
    run_output_paths: Mapping[str, Path],
    run_receipt_paths: Mapping[str, Path],
    completed_units_path: Path | None = None,
    judge_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze a complete work queue or a fully evaluable private bundle."""

    if (completed_units_path is None) != (judge_receipt_path is None):
        raise GmailFactParityPreparationError(
            "completed units and their judge receipt must be supplied together"
        )

    input_artifacts = {
        "packets": packets_path,
        "cohort": cohort_path,
        "admissions": admissions_path,
        "cohort_manifest": cohort_manifest_path,
        "original_inventory": original_inventory_path,
        "v2_inventory": v2_inventory_path,
        **{f"output:{key}": Path(value) for key, value in run_output_paths.items()},
        **{f"receipt:{key}": Path(value) for key, value in run_receipt_paths.items()},
    }
    if completed_units_path is not None:
        assert judge_receipt_path is not None
        input_artifacts["completed_units"] = completed_units_path
        input_artifacts["judge_receipt"] = judge_receipt_path
    evaluator.validate_private_artifact_set(input_artifacts)
    evidence = evaluator.load_gmail_fact_parity_bound_evidence(
        packets_path,
        cohort_path,
        admissions_path,
        cohort_manifest_path,
        original_inventory_path,
        v2_inventory_path,
    )
    run_evidence = evaluator.load_gmail_fact_parity_runs(
        run_output_paths, run_receipt_paths, evidence
    )
    queue_rows = evaluator.build_gmail_fact_parity_work_queue(evidence, run_evidence)
    queue_bytes = evaluator.gmail_fact_parity_jsonl_bytes(queue_rows)
    artifacts: dict[str, bytes] = {"work-queue.jsonl": queue_bytes}
    label_units = 0

    if completed_units_path is not None:
        assert judge_receipt_path is not None
        completed_units = evaluator.load_gmail_fact_parity_completed_units(
            completed_units_path,
            evidence=evidence,
            run_evidence=run_evidence,
        )
        alignment, labels = evaluator.derive_gmail_fact_parity_units(
            completed_units, run_evidence=run_evidence
        )
        alignment_bytes = evaluator.gmail_fact_parity_jsonl_bytes(alignment)
        labels_bytes = evaluator.gmail_fact_parity_jsonl_bytes(labels)
        completed_bytes = completed_units_path.read_bytes()
        judge_receipt = evaluator.load_gmail_fact_parity_judge_receipt(
            judge_receipt_path,
            completed_units_sha256=evaluator._sha256_bytes(completed_bytes),  # noqa: SLF001
            work_queue_sha256=evaluator._sha256_bytes(queue_bytes),  # noqa: SLF001
            evidence=evidence,
            run_evidence=run_evidence,
        )
        judge_receipt_bytes = judge_receipt_path.read_bytes()
        manifest = evaluator.build_gmail_fact_parity_manifest(
            labels_bytes=labels_bytes,
            alignment_bytes=alignment_bytes,
            completed_units_bytes=completed_bytes,
            work_queue_bytes=queue_bytes,
            evidence=evidence,
            run_evidence=run_evidence,
            judge_receipt=judge_receipt,
            labels=labels,
        )
        artifacts.update(
            {
                "completed-units.jsonl": completed_bytes,
                "judge-receipt.json": judge_receipt_bytes,
                "alignment.jsonl": alignment_bytes,
                "labels.jsonl": labels_bytes,
                "manifest.json": evaluator._canonical_json(manifest) + b"\n",  # noqa: SLF001
            }
        )
        label_units = len(labels)

    preparation = _preparation_manifest(
        release_ready=completed_units_path is not None,
        evidence=evidence,
        run_evidence=run_evidence,
        artifacts=artifacts,
    )
    artifacts["preparation.json"] = evaluator._canonical_json(preparation) + b"\n"  # noqa: SLF001
    _publish_frozen_artifacts(output_root, artifacts)
    return {
        "version": VERSION,
        "release_evidence_ready": completed_units_path is not None,
        "cohort_sha256": evidence["cohort_sha256"],
        "packet_sha256": evidence["packet_sha256"],
        "packets": evidence["packet_count"],
        "threads": evidence["thread_count"],
        "messages": evidence["message_count"],
        "runs": len(run_evidence["all_run_ids"]),
        "v2_runs": len(run_evidence["v2_run_ids"]),
        "emitted_members": preparation["member_count"],
        "work_items": len(queue_rows),
        "labeled_units": label_units,
        "independent_invocations_verified": False,
        "private_content_printed": False,
        "external_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packets", type=Path)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("admissions", type=Path)
    parser.add_argument("cohort_manifest", type=Path)
    parser.add_argument("original_inventory", type=Path)
    parser.add_argument("v2_inventory", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--completed-units", type=Path)
    parser.add_argument("--judge-receipt", type=Path)
    parser.add_argument(
        "--run-output",
        action="append",
        default=[],
        metavar="RUN_ID=PATH",
    )
    parser.add_argument(
        "--run-receipt",
        action="append",
        default=[],
        metavar="RUN_ID=PATH",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            prepare_gmail_fact_parity_evaluation(
                args.packets,
                args.cohort,
                args.admissions,
                args.cohort_manifest,
                args.original_inventory,
                args.v2_inventory,
                args.output_root,
                run_output_paths=evaluator._run_artifact_arguments(  # noqa: SLF001
                    args.run_output
                ),
                run_receipt_paths=evaluator._run_artifact_arguments(  # noqa: SLF001
                    args.run_receipt
                ),
                completed_units_path=args.completed_units,
                judge_receipt_path=args.judge_receipt,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
