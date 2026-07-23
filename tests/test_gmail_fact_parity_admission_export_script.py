from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


exporter = _load(
    "test_export_gmail_fact_parity_admissions",
    ROOT / "scripts" / "export_gmail_fact_parity_admissions.py",
)
cohort = _load(
    "test_exported_gmail_fact_parity_cohort",
    ROOT / "scripts" / "build_gmail_fact_parity_cohort.py",
)


def _private_write(path: Path, payload: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)


def _projection_text(index: int) -> str:
    account = "private-owner@example.test"
    thread = f"private-thread-{index}"
    revision = hashlib.sha256(f"revision-{index}".encode()).hexdigest()
    message = f"private-message-{index}"
    internal_at = "2026-07-01T16:00:00+00:00"
    heading = f"## Message 1 — {internal_at} — {message}"
    message_text = (
        f"{heading}\n\nFrom: private@example.test\nSubject: Private {index}\n\n"
        f"A durable non-temporal fact for private source {index}."
    )
    title = f"# Email thread: Private {index}"
    body = f"{title}\n\n{message_text}"
    start = len(title) + 2
    timestamp = {
        "message_id": message,
        "internal_date": internal_at,
        "start_offset": start,
        "end_offset": start + len(message_text),
    }
    return (
        "---\n"
        f"title: {json.dumps(f'Private {index}')}\n"
        "source_type: gmail_thread\n"
        f"gmail_account_key: {json.dumps(account)}\n"
        f"gmail_thread_id: {json.dumps(thread)}\n"
        f"gmail_source_revision: {json.dumps(revision)}\n"
        "gmail_projection_version: 7\n"
        "gmail_classifier_version: 5\n"
        f"gmail_message_ids: {json.dumps([message])}\n"
        f"gmail_fact_admitted_message_ids: {json.dumps([message])}\n"
        "gmail_message_timestamps_version: 1\n"
        f"gmail_message_timestamps: {json.dumps([timestamp])}\n"
        "retained_message_count: 1\n"
        "deleted: false\n"
        "---\n\n"
        f"{body}\n"
    )


def _binding(index: int) -> dict[str, str]:
    return {
        "version": exporter.TEMPORAL_BINDING_VERSION,
        "gmail_account_key": "private-owner@example.test",
        "gmail_thread_id": f"private-thread-{index}",
        "gmail_source_revision": hashlib.sha256(
            f"revision-{index}".encode()
        ).hexdigest(),
        "gmail_message_id": f"private-message-{index}",
    }


def _fixture(tmp_path: Path) -> dict[str, Path]:
    key = tmp_path / "key"
    _private_write(key, b"k" * 32)
    canonical = tmp_path / "canonical"
    canonical.mkdir(mode=0o700)
    for index in range(103):
        _private_write(canonical / f"source-{index}.md", _projection_text(index))

    holdout = tmp_path / "holdout"
    holdout.mkdir(mode=0o700)
    artifact_sha256: dict[str, str] = {}
    for index, relative_name in enumerate(exporter.TEMPORAL_BINDING_NAMES):
        payload = exporter._canonical_json(_binding(index)) + b"\n"  # noqa: SLF001
        path = holdout / relative_name
        _private_write(path, payload)
        artifact_sha256[relative_name] = hashlib.sha256(payload).hexdigest()
    manifest = {
        "version": exporter.TEMPORAL_MANIFEST_VERSION,
        "builder_sha256": "b" * 64,
        "label_status": "unlabeled",
        "routable": False,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "artifact_sha256": artifact_sha256,
    }
    authenticator = hmac.new(
        key.read_bytes(),
        exporter.TEMPORAL_MANIFEST_DOMAIN + exporter._canonical_json(manifest),  # noqa: SLF001
        hashlib.sha256,
    ).hexdigest()
    _private_write(
        holdout / "manifest.json",
        exporter._canonical_json(  # noqa: SLF001
            {**manifest, "manifest_hmac_sha256": authenticator}
        )
        + b"\n",
    )
    return {
        "key": key,
        "canonical": canonical,
        "holdout": holdout,
        "export": tmp_path / "export",
        "cohort": tmp_path / "cohort",
    }


def test_export_freezes_identical_non_holdout_admissions_accepted_by_builder(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)

    result = exporter.export_gmail_fact_parity_admissions(
        files["canonical"],
        files["holdout"],
        files["key"],
        files["export"],
        thread_count=100,
    )

    assert result["threads"] == 100
    assert result["messages"] == 100
    assert result["eligible_threads"] == 100
    assert result["temporal_holdout_threads_excluded"] == 3
    assert result["inventories_identical"] is True
    assert result["semantic_denominator_verified"] is False
    assert result["release_evidence_ready"] is False
    original = files["export"] / "original-admissions.jsonl"
    v2 = files["export"] / "v2-admissions.jsonl"
    assert original.read_bytes() == v2.read_bytes()
    selected_thread_ids = {
        json.loads(line)["gmail_thread_id"]
        for line in original.read_text().splitlines()
    }
    assert all(
        _binding(index)["gmail_thread_id"] not in selected_thread_ids
        for index in range(3)
    )
    manifest = json.loads((files["export"] / "manifest.json").read_text())
    assert manifest["selection_scope"] == (
        "fact_rich_capability_challenge_not_population_estimate"
    )
    assert manifest["may_be_pooled_with_temporal_cohorts"] is False
    assert manifest["original_native_gmail_admission_available"] is False
    assert manifest["original_baseline"] == {
        "commit": exporter.evaluator.EXPECTED_ORIGINAL_COMMIT,
        "prompt_version": exporter.evaluator.EXPECTED_ORIGINAL_PROMPT_VERSION,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
    }
    assert stat.S_IMODE(files["export"].stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in files["export"].iterdir()
    )

    cohort_result = cohort.build_gmail_fact_parity_cohort(
        files["canonical"], original, v2, files["key"], files["cohort"]
    )
    assert cohort_result["packets"] == 100
    assert cohort_result["threads"] == 100
    assert cohort_result["messages"] == 100
    assert cohort_result["original_admitted_messages"] == 100
    assert cohort_result["v2_admitted_messages"] == 100


def test_export_rejects_a_structurally_underpowered_cohort(tmp_path: Path) -> None:
    files = _fixture(tmp_path)

    with pytest.raises(
        exporter.GmailFactParityAdmissionExportError,
        match="thread count must be at least 100",
    ):
        exporter.export_gmail_fact_parity_admissions(
            files["canonical"],
            files["holdout"],
            files["key"],
            files["export"],
            thread_count=99,
        )


def test_export_rejects_tampered_temporal_exclusions(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    binding = files["holdout"] / exporter.TEMPORAL_BINDING_NAMES[0]
    _private_write(binding, binding.read_bytes() + b"\n")

    with pytest.raises(
        exporter.GmailFactParityAdmissionExportError,
        match="binding digest is stale",
    ):
        exporter.export_gmail_fact_parity_admissions(
            files["canonical"],
            files["holdout"],
            files["key"],
            files["export"],
            thread_count=100,
        )
