from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[1] / "scripts" / "build_gmail_fact_parity_cohort.py"
)
SPEC = importlib.util.spec_from_file_location(
    "build_gmail_fact_parity_cohort", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
cohort = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cohort
SPEC.loader.exec_module(cohort)


ACCOUNT_KEY = "owner-private@example.test"
THREAD_ID = "provider-thread-secret"
REVISION = "a" * 64
MESSAGE_1 = "provider-message-secret-1"
MESSAGE_2 = "provider-message-secret-2"


def _private_write(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)
    os.chmod(path, 0o600)


def _projection_text() -> str:
    message_texts = [
        (
            f"## Message 1 — 2026-07-01T16:00:00+00:00 — {MESSAGE_1}\n\n"
            "From: collaborator@example.test\n"
            "To: owner@example.test\n"
            "Subject: Project Blue\n\n"
            "We agreed that Project Blue remains limited to the pilot customers."
        ),
        (
            f"## Message 2 — 2026-07-02T17:00:00+00:00 — {MESSAGE_2}\n\n"
            "From: owner@example.test\n"
            "To: collaborator@example.test\n"
            "Subject: Re: Project Blue\n\n"
            "I will prepare the customer note before the pilot begins."
        ),
    ]
    body = "# Email thread: Project Blue\n\n" + "\n\n".join(message_texts)
    ranges = []
    cursor = len("# Email thread: Project Blue")
    for message_id, internal_date, message_text in zip(
        (MESSAGE_1, MESSAGE_2),
        ("2026-07-01T16:00:00+00:00", "2026-07-02T17:00:00+00:00"),
        message_texts,
    ):
        start = cursor + 2
        end = start + len(message_text)
        ranges.append(
            {
                "message_id": message_id,
                "internal_date": internal_date,
                "start_offset": start,
                "end_offset": end,
            }
        )
        cursor = end
    return (
        "---\n"
        'title: "Project Blue"\n'
        "source_type: gmail_thread\n"
        f"gmail_account_key: {json.dumps(ACCOUNT_KEY)}\n"
        f"gmail_thread_id: {json.dumps(THREAD_ID)}\n"
        f"gmail_source_revision: {json.dumps(REVISION)}\n"
        "gmail_projection_version: 3\n"
        "gmail_classifier_version: 7\n"
        f"gmail_message_ids: {json.dumps([MESSAGE_1, MESSAGE_2])}\n"
        "gmail_message_timestamps_version: 1\n"
        f"gmail_message_timestamps: {json.dumps(ranges)}\n"
        "retained_message_count: 2\n"
        "---\n\n"
        f"{body}\n"
    )


def _admission_row(source_sha256: str, admitted_ids: list[str]) -> dict[str, object]:
    return {
        "version": "gmail_fact_parity_admission_v1",
        "gmail_account_key": ACCOUNT_KEY,
        "gmail_thread_id": THREAD_ID,
        "gmail_source_revision": REVISION,
        "gmail_projection_version": 3,
        "gmail_classifier_version": 7,
        "source_sha256": source_sha256,
        "admitted_message_ids": admitted_ids,
    }


def _fixture(tmp_path: Path, *, name: str = "fixture") -> dict[str, Path]:
    base = tmp_path / name
    source_root = base / "canonical"
    source = source_root / "projection.md"
    _private_write(source, _projection_text())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    original = base / "original.jsonl"
    v2 = base / "v2.jsonl"
    _private_write(
        original,
        json.dumps(_admission_row(digest, [MESSAGE_1]), sort_keys=True) + "\n",
    )
    _private_write(
        v2,
        json.dumps(_admission_row(digest, [MESSAGE_1, MESSAGE_2]), sort_keys=True)
        + "\n",
    )
    key = base / "hmac.key"
    _private_write(key, b"k" * 32)
    return {
        "root": source_root,
        "source": source,
        "original": original,
        "v2": v2,
        "key": key,
        "output": base / "output",
    }


def test_cohort_builder_freezes_opaque_identical_packets(tmp_path: Path) -> None:
    files = _fixture(tmp_path)

    result = cohort.build_gmail_fact_parity_cohort(
        files["root"],
        files["original"],
        files["v2"],
        files["key"],
        files["output"],
    )

    assert result == {
        "version": "gmail_fact_parity_cohort_builder_v2",
        "cohort_sha256": result["cohort_sha256"],
        "packet_sha256": result["packet_sha256"],
        "source_binding_sha256": result["source_binding_sha256"],
        "canonical_source_set_sha256": result["canonical_source_set_sha256"],
        "source_revisions": 1,
        "packets": 1,
        "threads": 1,
        "messages": 2,
        "original_admitted_messages": 1,
        "v2_admitted_messages": 2,
        "private_content_printed": False,
        "external_calls": 0,
    }
    assert stat.S_IMODE(files["output"].stat().st_mode) == 0o700
    for name in (
        "packets.jsonl",
        "cohort.jsonl",
        "admissions.jsonl",
        "source-bindings.jsonl",
        "manifest.json",
    ):
        assert stat.S_IMODE((files["output"] / name).stat().st_mode) == 0o600

    packet = json.loads((files["output"] / "packets.jsonl").read_text())
    index = json.loads((files["output"] / "cohort.jsonl").read_text())
    admissions = json.loads((files["output"] / "admissions.jsonl").read_text())
    manifest = json.loads((files["output"] / "manifest.json").read_text())
    bindings = json.loads((files["output"] / "source-bindings.jsonl").read_text())
    assert packet["packet_id"].startswith("gfp_p_")
    assert packet["thread_id"].startswith("gfp_t_")
    assert packet["revision_id"].startswith("gfp_r_")
    assert [item["message_id"] for item in packet["messages"]] == index["message_ids"]
    assert all(item["message_id"].startswith("gfp_m_") for item in packet["messages"])
    assert admissions["original_message_ids"] == [packet["messages"][0]["message_id"]]
    assert admissions["v2_message_ids"] == index["message_ids"]
    assert admissions["union_message_ids"] == index["message_ids"]
    assert admissions["original_renderer"] == {
        "projection_version": 3,
        "classifier_version": 7,
        "source_sha256": hashlib.sha256(files["source"].read_bytes()).hexdigest(),
    }
    assert manifest["cohort_sha256"] == result["cohort_sha256"]
    assert manifest["packet_sha256"] == result["packet_sha256"]
    assert manifest["provider_ids_in_packet_metadata"] is False
    assert manifest["packet_policy"] == "union_admitted_messages_only"
    assert manifest["renderer_versions_are_provenance_not_identity"] is True
    assert bindings["gmail_account_key"] == ACCOUNT_KEY
    assert bindings["gmail_thread_id"] == THREAD_ID
    assert bindings["gmail_source_revision"] == REVISION
    assert [item["gmail_message_id"] for item in bindings["messages"]] == [
        MESSAGE_1,
        MESSAGE_2,
    ]
    unsigned = dict(manifest)
    authenticator = unsigned.pop("manifest_hmac_sha256")
    assert authenticator == cohort._manifest_hmac(b"k" * 32, unsigned)

    artifacts = b"".join(
        (files["output"] / name).read_bytes()
        for name in (
            "packets.jsonl",
            "cohort.jsonl",
            "admissions.jsonl",
            "manifest.json",
        )
    )
    for secret in (ACCOUNT_KEY, THREAD_ID, REVISION, MESSAGE_1, MESSAGE_2):
        assert secret.encode() not in artifacts


def test_cohort_ids_and_digests_are_portable_across_source_paths(
    tmp_path: Path,
) -> None:
    first = _fixture(tmp_path, name="first")
    second = _fixture(tmp_path, name="second")

    first_result = cohort.build_gmail_fact_parity_cohort(
        first["root"],
        first["original"],
        first["v2"],
        first["key"],
        first["output"],
    )
    second_result = cohort.build_gmail_fact_parity_cohort(
        second["root"],
        second["original"],
        second["v2"],
        second["key"],
        second["output"],
    )

    assert first_result == second_result
    for name in (
        "packets.jsonl",
        "cohort.jsonl",
        "admissions.jsonl",
        "source-bindings.jsonl",
    ):
        assert (first["output"] / name).read_bytes() == (
            second["output"] / name
        ).read_bytes()
    first_manifest = json.loads((first["output"] / "manifest.json").read_text())
    second_manifest = json.loads((second["output"] / "manifest.json").read_text())
    assert first_manifest["id_namespace"] == second_manifest["id_namespace"]
    assert (
        first_manifest["canonical_source_set_sha256"]
        == second_manifest["canonical_source_set_sha256"]
    )


def test_cohort_builder_rejects_stale_or_non_private_inputs(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    original_row = json.loads(files["original"].read_text())
    original_row["source_sha256"] = "0" * 64
    _private_write(files["original"], json.dumps(original_row) + "\n")

    with pytest.raises(
        cohort.GmailFactParityCohortError,
        match="stale for the canonical projection",
    ):
        cohort.build_gmail_fact_parity_cohort(
            files["root"],
            files["original"],
            files["v2"],
            files["key"],
            files["output"],
        )
    assert not files["output"].exists()

    original_row["source_sha256"] = hashlib.sha256(
        files["source"].read_bytes()
    ).hexdigest()
    _private_write(files["original"], json.dumps(original_row) + "\n")
    os.chmod(files["v2"], 0o644)
    with pytest.raises(cohort.GmailFactParityCohortError, match="mode 0600"):
        cohort.build_gmail_fact_parity_cohort(
            files["root"],
            files["original"],
            files["v2"],
            files["key"],
            files["output"],
        )


def test_cohort_builder_joins_different_arm_renderer_versions_by_portable_key(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    row = json.loads(files["v2"].read_text())
    row["gmail_classifier_version"] = 8
    row["source_sha256"] = "b" * 64
    _private_write(files["v2"], json.dumps(row) + "\n")

    cohort.build_gmail_fact_parity_cohort(
        files["root"],
        files["original"],
        files["v2"],
        files["key"],
        files["output"],
    )

    manifest = json.loads((files["output"] / "manifest.json").read_text())
    admissions = json.loads((files["output"] / "admissions.jsonl").read_text())
    assert manifest["original_renderer_provenance"] == [
        {"projection_version": 3, "classifier_version": 7, "source_count": 1}
    ]
    assert manifest["v2_renderer_provenance"] == [
        {"projection_version": 3, "classifier_version": 8, "source_count": 1}
    ]
    assert admissions["v2_renderer"]["source_sha256"] == "b" * 64


def test_cohort_builder_requires_complete_matching_portable_source_coverage(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    row = json.loads(files["v2"].read_text())
    row["gmail_thread_id"] = "a-different-provider-thread"
    _private_write(files["v2"], json.dumps(row) + "\n")

    with pytest.raises(
        cohort.GmailFactParityCohortError, match="same canonical source set"
    ):
        cohort.build_gmail_fact_parity_cohort(
            files["root"],
            files["original"],
            files["v2"],
            files["key"],
            files["output"],
        )


def test_cohort_builder_rejects_message_outside_canonical_source(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    row = json.loads(files["v2"].read_text())
    row["admitted_message_ids"].append("not-in-canonical-source")
    _private_write(files["v2"], json.dumps(row) + "\n")

    with pytest.raises(
        cohort.GmailFactParityCohortError,
        match="non-canonical message",
    ):
        cohort.build_gmail_fact_parity_cohort(
            files["root"],
            files["original"],
            files["v2"],
            files["key"],
            files["output"],
        )


def test_message_index_allows_complete_zero_message_sources() -> None:
    assert (
        cohort._source_messages(
            {
                "gmail_message_ids": [],
                "gmail_message_timestamps": [],
                "gmail_message_timestamps_version": 1,
                "retained_message_count": 0,
            },
            "# Email thread: deleted",
        )
        == ()
    )


def test_frozen_cohort_rejects_existing_output_without_rewriting(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    cohort.build_gmail_fact_parity_cohort(
        files["root"],
        files["original"],
        files["v2"],
        files["key"],
        files["output"],
    )
    before = {
        name: (files["output"] / name).read_bytes()
        for name in cohort.OUTPUT_ARTIFACT_NAMES
    }

    with pytest.raises(cohort.GmailFactParityCohortError, match="already exists"):
        cohort.build_gmail_fact_parity_cohort(
            files["root"],
            files["original"],
            files["v2"],
            files["key"],
            files["output"],
        )

    assert before == {
        name: (files["output"] / name).read_bytes()
        for name in cohort.OUTPUT_ARTIFACT_NAMES
    }


def test_failed_publication_leaves_no_partial_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _fixture(tmp_path)
    real_write = cohort._write_private_new
    writes = 0

    def fail_third_write(path: Path, payload: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("synthetic write failure")
        real_write(path, payload)

    monkeypatch.setattr(cohort, "_write_private_new", fail_third_write)
    with pytest.raises(OSError, match="synthetic write failure"):
        cohort.build_gmail_fact_parity_cohort(
            files["root"],
            files["original"],
            files["v2"],
            files["key"],
            files["output"],
        )

    assert not files["output"].exists()
    assert list(files["output"].parent.glob(f".{files['output'].name}.tmp-*")) == []
