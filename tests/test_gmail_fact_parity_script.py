from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
EVALUATOR_PATH = ROOT / "scripts" / "evaluate_gmail_fact_parity.py"
PREPARER_PATH = ROOT / "scripts" / "prepare_gmail_fact_parity_evaluation.py"
COHORT_PATH = ROOT / "scripts" / "build_gmail_fact_parity_cohort.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


parity = _load("test_evaluate_gmail_fact_parity", EVALUATOR_PATH)
prepare = _load("test_prepare_gmail_fact_parity", PREPARER_PATH)
cohort_builder = _load("test_build_gmail_fact_parity", COHORT_PATH)

RUN_IDS = ("original", "v2-a", "v2-b", "v2-c")


def _id(prefix: str, value: int) -> str:
    return f"{prefix}_{value:032x}"


def _private_write(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def _projection(
    *, account: str, thread: str, revision: str, message: str, index: int
) -> str:
    rendered = (
        f"## Message 1 — 2026-07-{(index % 28) + 1:02d}T16:00:00+00:00 — {message}\n\n"
        "From: collaborator@example.test\n"
        "To: owner@example.test\n"
        f"Subject: Private parity source {index}\n\n"
        f"The source-backed durable policy statement for item {index}."
    )
    heading = f"# Email thread: Private parity source {index}"
    body = heading + "\n\n" + rendered
    start = len(heading) + 2
    end = start + len(rendered)
    timestamps = [
        {
            "message_id": message,
            "internal_date": f"2026-07-{(index % 28) + 1:02d}T16:00:00+00:00",
            "start_offset": start,
            "end_offset": end,
        }
    ]
    return (
        "---\n"
        f'title: "Private parity source {index}"\n'
        "source_type: gmail_thread\n"
        f"gmail_account_key: {json.dumps(account)}\n"
        f"gmail_thread_id: {json.dumps(thread)}\n"
        f"gmail_source_revision: {json.dumps(revision)}\n"
        "gmail_projection_version: 3\n"
        "gmail_classifier_version: 7\n"
        f"gmail_message_ids: {json.dumps([message])}\n"
        "gmail_message_timestamps_version: 1\n"
        f"gmail_message_timestamps: {json.dumps(timestamps)}\n"
        "retained_message_count: 1\n"
        "---\n\n"
        f"{body}\n"
    )


def _admission(
    *, account: str, thread: str, revision: str, message: str, digest: str
) -> dict[str, Any]:
    return {
        "version": "gmail_fact_parity_admission_v1",
        "gmail_account_key": account,
        "gmail_thread_id": thread,
        "gmail_source_revision": revision,
        "gmail_projection_version": 3,
        "gmail_classifier_version": 7,
        "source_sha256": digest,
        "admitted_message_ids": [message],
    }


def _cohort(tmp_path: Path, *, unit_count: int) -> dict[str, Path]:
    canonical = tmp_path / "canonical"
    original_rows = []
    v2_rows = []
    account = "private-owner"
    for index in range(unit_count):
        thread = f"private-thread-{index}"
        message = f"private-message-{index}"
        revision = f"{index + 1:064x}"
        path = canonical / f"projection-{index}.md"
        _private_write(
            path,
            _projection(
                account=account,
                thread=thread,
                revision=revision,
                message=message,
                index=index,
            ),
        )
        row = _admission(
            account=account,
            thread=thread,
            revision=revision,
            message=message,
            digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        original_rows.append(row)
        v2_rows.append(row)
    original = tmp_path / "original-admissions.jsonl"
    v2 = tmp_path / "v2-admissions.jsonl"
    _private_write(
        original,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in original_rows),
    )
    _private_write(
        v2, "".join(json.dumps(row, sort_keys=True) + "\n" for row in v2_rows)
    )
    key = tmp_path / "hmac.key"
    _private_write(key, b"k" * 32)
    output = tmp_path / "cohort-output"
    cohort_builder.build_gmail_fact_parity_cohort(canonical, original, v2, key, output)
    return {
        "packets": output / "packets.jsonl",
        "cohort": output / "cohort.jsonl",
        "admissions": output / "admissions.jsonl",
        "cohort_manifest": output / "manifest.json",
        "original_inventory": original,
        "v2_inventory": v2,
    }


def _run_output(
    path: Path,
    *,
    run_id: str,
    arm: str | None = None,
    packets: list[dict[str, Any]],
    cohort_sha256: str,
    packet_sha256: str,
    extra_member: bool = False,
    empty: bool = False,
    empty_packet_indexes: frozenset[int] = frozenset(),
    stage_overrides: dict[int, dict[str, bool]] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, str]:
    resolved_arm = arm or ("original" if run_id == "original" else "v2")
    header = {
        "version": parity.RUN_VERSION,
        "run_id": run_id,
        "arm": resolved_arm,
        "commit": "a" * 40 if resolved_arm == "original" else "b" * 40,
        "prompt_version": (
            "original-fact-v1"
            if resolved_arm == "original"
            else parity.EXPECTED_V2_PROMPT_VERSION
        ),
        "model": model or parity.EXPECTED_V2_MODEL,
        "reasoning_effort": (
            reasoning_effort
            or (
                "medium"
                if resolved_arm == "original"
                else parity.EXPECTED_V2_REASONING_EFFORT
            )
        ),
        "cohort_sha256": cohort_sha256,
        "packet_sha256": packet_sha256,
    }
    members: dict[str, str] = {}
    rows = [header]
    for index, packet in enumerate(packets):
        message_id = packet["messages"][0]["message_id"]
        packet_members = []
        if not empty and index not in empty_packet_indexes:
            member = {
                "statement": f"PRIVATE_FACT_SENTINEL_{run_id}_{index}",
                "evidence_message_ids": [message_id],
                "stages": (stage_overrides or {}).get(
                    index, {stage: True for stage in parity.STAGES}
                ),
            }
            member_id = parity.gmail_fact_parity_member_id(
                run_id,
                packet["packet_id"],
                0,
                member,
            )
            members[packet["packet_id"]] = member_id
            packet_members.append(member)
        if extra_member and index == 0:
            duplicate = {
                "statement": f"PRIVATE_DUPLICATE_SENTINEL_{run_id}",
                "evidence_message_ids": [message_id],
                "stages": {stage: True for stage in parity.STAGES},
            }
            packet_members.append(duplicate)
        rows.append(
            {
                "version": parity.RUN_PACKET_VERSION,
                "run_id": run_id,
                "packet_id": packet["packet_id"],
                "thread_id": packet["thread_id"],
                "members": packet_members,
            }
        )
    _private_write(path, parity.gmail_fact_parity_jsonl_bytes(rows))
    return members


def _receipt(path: Path, *, run_id: str, output: Path, invocation: str) -> None:
    header = json.loads(output.read_text().splitlines()[0])
    _private_write(
        path,
        json.dumps(
            {
                "version": parity.RECEIPT_VERSION,
                "run_id": run_id,
                "invocation_id": invocation,
                "provider": "external-codex",
                "model": header["model"],
                "reasoning_effort": header["reasoning_effort"],
                "started_at": "2026-07-22T10:00:00+00:00",
                "completed_at": "2026-07-22T10:01:00+00:00",
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "attestation": parity.INVOCATION_ATTESTATION,
            },
            sort_keys=True,
        ),
    )


def _judge_receipt(
    path: Path,
    *,
    completed_units: Path,
    evidence: dict[str, Any],
    work_queue: bytes,
) -> None:
    _private_write(
        path,
        json.dumps(
            {
                "version": parity.JUDGE_RECEIPT_VERSION,
                "invocation_id": "invoke-final-sol-judge",
                "provider": parity.EXPECTED_PROVIDER,
                "model": parity.EXPECTED_JUDGE_MODEL,
                "reasoning_effort": parity.EXPECTED_JUDGE_REASONING_EFFORT,
                "started_at": "2026-07-22T11:00:00+00:00",
                "completed_at": "2026-07-22T11:01:00+00:00",
                "cohort_sha256": evidence["cohort_sha256"],
                "packet_sha256": evidence["packet_sha256"],
                "work_queue_sha256": hashlib.sha256(work_queue).hexdigest(),
                "completed_units_sha256": hashlib.sha256(
                    completed_units.read_bytes()
                ).hexdigest(),
                "judge_contract_version": parity.JUDGE_CONTRACT_VERSION,
                "judge_contract_sha256": parity.JUDGE_CONTRACT_SHA256,
                "attestation": parity.INVOCATION_ATTESTATION,
            },
            sort_keys=True,
        ),
    )


def _fixture(
    tmp_path: Path,
    *,
    unit_count: int = 20,
    completed: bool = True,
    extra_member_run: str | None = None,
    empty_runs: frozenset[str] = frozenset(),
    empty_packet_indexes: frozenset[int] = frozenset(),
    run_empty_packet_indexes: dict[str, frozenset[int]] | None = None,
    run_stage_overrides: dict[str, dict[int, dict[str, bool]]] | None = None,
    critical_packet_indexes: dict[str, frozenset[int]] | None = None,
    useful_unit_count: int | None = None,
    run_ids: tuple[str, ...] = RUN_IDS,
    original_run_id: str | None = None,
    original_model: str | None = None,
    original_reasoning_effort: str | None = None,
) -> dict[str, Any]:
    cohort = _cohort(tmp_path, unit_count=unit_count)
    packets = [json.loads(line) for line in cohort["packets"].read_text().splitlines()]
    cohort_manifest = json.loads(cohort["cohort_manifest"].read_text())
    outputs: dict[str, Path] = {}
    receipts: dict[str, Path] = {}
    member_ids: dict[str, dict[str, str]] = {}
    resolved_original_run_id = original_run_id or run_ids[0]
    for run_id in run_ids:
        output = tmp_path / f"{run_id}-output.jsonl"
        member_ids[run_id] = _run_output(
            output,
            run_id=run_id,
            arm="original" if run_id == resolved_original_run_id else "v2",
            packets=packets,
            cohort_sha256=cohort_manifest["cohort_sha256"],
            packet_sha256=cohort_manifest["packet_sha256"],
            extra_member=run_id == extra_member_run,
            empty=run_id in empty_runs,
            empty_packet_indexes=(
                empty_packet_indexes
                | (run_empty_packet_indexes or {}).get(run_id, frozenset())
            ),
            stage_overrides=(run_stage_overrides or {}).get(run_id),
            model=original_model if run_id == resolved_original_run_id else None,
            reasoning_effort=(
                original_reasoning_effort
                if run_id == resolved_original_run_id
                else None
            ),
        )
        receipt = tmp_path / f"{run_id}-receipt.json"
        _receipt(receipt, run_id=run_id, output=output, invocation=f"invoke-{run_id}")
        outputs[run_id] = output
        receipts[run_id] = receipt

    evidence = parity.load_gmail_fact_parity_bound_evidence(
        cohort["packets"],
        cohort["cohort"],
        cohort["admissions"],
        cohort["cohort_manifest"],
        cohort["original_inventory"],
        cohort["v2_inventory"],
    )
    run_evidence = parity.load_gmail_fact_parity_runs(outputs, receipts, evidence)
    aliases = run_evidence["run_aliases"]
    queue_bytes = parity.gmail_fact_parity_jsonl_bytes(
        parity.build_gmail_fact_parity_work_queue(evidence, run_evidence)
    )
    completed_path = tmp_path / "completed-units-input.jsonl"
    completed_rows = []
    for packet_index, packet in enumerate(packets):
        members = {
            aliases[run_id]: [
                {
                    "member_id": member_ids[run_id][packet["packet_id"]],
                    "supported": True,
                    "scope_correct": True,
                    "critical_error": "none",
                }
            ]
            if packet["packet_id"] in member_ids[run_id]
            else []
            for run_id in run_ids
        }
        for run_id, packet_indexes in (critical_packet_indexes or {}).items():
            if packet_index in packet_indexes and members[aliases[run_id]]:
                members[aliases[run_id]][0]["critical_error"] = "wrong_entity"
        all_empty = not any(members.values())
        useful = not all_empty and (
            useful_unit_count is None or packet_index < useful_unit_count
        )
        completed_rows.append(
            {
                "version": parity.COMPLETED_UNIT_VERSION,
                "packet_id": packet["packet_id"],
                "useful": useful,
                "classification": "not_fact" if all_empty else "non_temporal",
                "members": members,
            }
        )
    if extra_member_run is not None:
        output_rows = [
            json.loads(line)
            for line in outputs[extra_member_run].read_text().splitlines()
        ]
        duplicate = output_rows[1]["members"][1]
        extra_id = parity.gmail_fact_parity_member_id(
            extra_member_run,
            packets[0]["packet_id"],
            1,
            duplicate,
        )
        completed_rows[0]["members"][aliases[extra_member_run]].append(
            {
                "member_id": extra_id,
                "supported": False,
                "scope_correct": False,
                "critical_error": "unsupported",
            }
        )
    _private_write(
        completed_path,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in completed_rows),
    )
    judge_receipt = tmp_path / "judge-receipt-input.json"
    _judge_receipt(
        judge_receipt,
        completed_units=completed_path,
        evidence=evidence,
        work_queue=queue_bytes,
    )
    bundle = tmp_path / "evaluation-bundle"
    prepare.prepare_gmail_fact_parity_evaluation(
        cohort["packets"],
        cohort["cohort"],
        cohort["admissions"],
        cohort["cohort_manifest"],
        cohort["original_inventory"],
        cohort["v2_inventory"],
        bundle,
        run_output_paths=outputs,
        run_receipt_paths=receipts,
        completed_units_path=completed_path if completed else None,
        judge_receipt_path=judge_receipt if completed else None,
    )
    return {
        **cohort,
        "packets_value": packets,
        "outputs": outputs,
        "receipts": receipts,
        "member_ids": member_ids,
        "aliases": aliases,
        "evidence": evidence,
        "run_evidence": run_evidence,
        "run_ids": run_ids,
        "queue_bytes": queue_bytes,
        "completed_input": completed_path,
        "completed_rows": completed_rows,
        "judge_receipt_input": judge_receipt,
        "bundle": bundle,
    }


def _evaluate(files: dict[str, Any]) -> dict[str, Any]:
    bundle = files["bundle"]
    return parity.evaluate_gmail_fact_parity(
        bundle / "labels.jsonl",
        bundle / "manifest.json",
        bundle / "alignment.jsonl",
        bundle / "completed-units.jsonl",
        bundle / "judge-receipt.json",
        bundle / "work-queue.jsonl",
        files["cohort"],
        files["packets"],
        files["admissions"],
        files["cohort_manifest"],
        files["original_inventory"],
        files["v2_inventory"],
        files["outputs"],
        files["receipts"],
    )


def test_complete_prepared_bundle_scores_three_runs_without_private_output(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path, unit_count=100)

    result = _evaluate(files)
    v2_alias = files["aliases"]["v2-a"]
    original_alias = files["aliases"]["original"]

    assert result["version"] == parity.VERSION
    assert result["gate_passed"] is True
    assert result["cohort"] == {
        "threads": 100,
        "messages": 100,
        "packets": 100,
        "labeled_units": 100,
        "labeled_packets": 100,
    }
    assert result["coverage_gate"]["passed"] is True
    candidate = result["runs"][v2_alias]["stages"]["candidate"]
    assert candidate["retention"] == 1.0
    assert candidate["precision"] == 1.0
    assert len(result["run_agreement"]) == 9
    assert result["invocation_attestation"] == {
        "claimed_invocation_ids_unique": True,
        "distinct_evidence_files_verified": True,
        "independent_invocations_verified": False,
        "limitation": parity.INVOCATION_ATTESTATION,
    }
    assert result["judge"] == {
        "provider": "external-codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "contract_version": parity.JUDGE_CONTRACT_VERSION,
        "contract_sha256": parity.JUDGE_CONTRACT_SHA256,
        "attestation": parity.INVOCATION_ATTESTATION,
    }
    assert result["runs"][v2_alias]["execution"] == {
        "provider": "external-codex",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "attestation": parity.INVOCATION_ATTESTATION,
    }
    assert result["evidence_execution"][original_alias] == {
        "arm": "original",
        "provider": "external-codex",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "attestation": parity.INVOCATION_ATTESTATION,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "PRIVATE_FACT_SENTINEL" not in serialized
    assert str(tmp_path) not in serialized
    assert stat.S_IMODE(files["bundle"].stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files["bundle"].iterdir()
    )


def test_preparer_generates_complete_private_work_queue_before_labels(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path, unit_count=3, completed=False)
    bundle = files["bundle"]

    assert sorted(path.name for path in bundle.iterdir()) == [
        "preparation.json",
        "work-queue.jsonl",
    ]
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in bundle.iterdir())
    queue = [
        json.loads(line)
        for line in (bundle / "work-queue.jsonl").read_text().splitlines()
    ]
    assert len(queue) == 3
    aliases = set(files["aliases"].values())
    assert all(set(row["members"]) == aliases for row in queue)
    assert all(
        len(row["members"][run_alias]) == 1 for row in queue for run_alias in aliases
    )
    assert all(row["messages"][0]["text"] for row in queue)
    assert not any(run_id in row["members"] for row in queue for run_id in RUN_IDS)
    preparation = json.loads((bundle / "preparation.json").read_text())
    assert preparation["release_evidence_ready"] is False
    assert preparation["independent_invocations_verified"] is False


def test_useful_false_cannot_shrink_the_release_denominator(tmp_path: Path) -> None:
    files = _fixture(
        tmp_path,
        unit_count=100,
        useful_unit_count=1,
        run_empty_packet_indexes={"v2-a": frozenset(range(1, 100))},
    )

    result = _evaluate(files)
    v2_alias = files["aliases"]["v2-a"]
    candidate = result["runs"][v2_alias]["stages"]["candidate"]

    assert candidate["original_non_temporal_units"] == 100
    assert candidate["original_useful_non_temporal_units"] == 1
    assert candidate["retention"] == 0.01
    assert candidate["useful_retention_diagnostic"] == 1.0
    assert result["coverage_gate"]["passed"] is True
    assert result["gate_passed"] is False


def test_tiny_cohort_cannot_pass_even_with_perfect_scores(tmp_path: Path) -> None:
    files = _fixture(tmp_path, unit_count=20)

    result = _evaluate(files)

    assert (
        result["runs"][files["aliases"]["v2-a"]]["stages"]["candidate"]["retention"]
        == 1.0
    )
    assert result["coverage_gate"]["passed"] is False
    assert result["coverage_gate"]["checks"]["minimum_packets"] is False
    assert result["gate_passed"] is False


def test_preparer_refuses_incomplete_semantic_alignment(tmp_path: Path) -> None:
    files = _fixture(tmp_path / "base", unit_count=3, completed=False)
    rows = files["completed_rows"][:-1]
    incomplete = tmp_path / "incomplete.jsonl"
    _private_write(
        incomplete,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
    )
    judge_receipt = tmp_path / "incomplete-judge.json"
    _judge_receipt(
        judge_receipt,
        completed_units=incomplete,
        evidence=files["evidence"],
        work_queue=files["queue_bytes"],
    )

    with pytest.raises(
        prepare.evaluator.GmailFactParityError,
        match="do not align every emitted member",
    ):
        prepare.prepare_gmail_fact_parity_evaluation(
            files["packets"],
            files["cohort"],
            files["admissions"],
            files["cohort_manifest"],
            files["original_inventory"],
            files["v2_inventory"],
            tmp_path / "rejected-bundle",
            run_output_paths=files["outputs"],
            run_receipt_paths=files["receipts"],
            completed_units_path=incomplete,
            judge_receipt_path=judge_receipt,
        )


def test_preparer_refuses_member_reuse_across_semantic_units(tmp_path: Path) -> None:
    files = _fixture(tmp_path / "base", unit_count=3, completed=False)
    rows = list(files["completed_rows"])
    duplicate = json.loads(json.dumps(rows[0]))
    rows.append(duplicate)
    completed = tmp_path / "duplicate-member.jsonl"
    _private_write(
        completed,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
    )
    judge_receipt = tmp_path / "duplicate-member-judge.json"
    _judge_receipt(
        judge_receipt,
        completed_units=completed,
        evidence=files["evidence"],
        work_queue=files["queue_bytes"],
    )

    with pytest.raises(
        prepare.evaluator.GmailFactParityError,
        match="aligned more than once",
    ):
        prepare.prepare_gmail_fact_parity_evaluation(
            files["packets"],
            files["cohort"],
            files["admissions"],
            files["cohort_manifest"],
            files["original_inventory"],
            files["v2_inventory"],
            tmp_path / "rejected-bundle",
            run_output_paths=files["outputs"],
            run_receipt_paths=files["receipts"],
            completed_units_path=completed,
            judge_receipt_path=judge_receipt,
        )


def test_preparer_requires_three_v2_runs(tmp_path: Path) -> None:
    files = _fixture(tmp_path / "base", unit_count=3, completed=False)
    outputs = {
        run_id: path for run_id, path in files["outputs"].items() if run_id != "v2-c"
    }
    receipts = {
        run_id: path for run_id, path in files["receipts"].items() if run_id != "v2-c"
    }

    with pytest.raises(
        prepare.evaluator.GmailFactParityError,
        match="at least 3 V2 runs",
    ):
        prepare.prepare_gmail_fact_parity_evaluation(
            files["packets"],
            files["cohort"],
            files["admissions"],
            files["cohort_manifest"],
            files["original_inventory"],
            files["v2_inventory"],
            tmp_path / "rejected-bundle",
            run_output_paths=outputs,
            run_receipt_paths=receipts,
        )


def test_preparer_rejects_heterogeneous_v2_target_config(tmp_path: Path) -> None:
    files = _fixture(tmp_path / "base", unit_count=3, completed=False)
    rows = [
        json.loads(line) for line in files["outputs"]["v2-b"].read_text().splitlines()
    ]
    rows[0]["commit"] = "c" * 40
    _private_write(files["outputs"]["v2-b"], parity.gmail_fact_parity_jsonl_bytes(rows))
    _receipt(
        files["receipts"]["v2-b"],
        run_id="v2-b",
        output=files["outputs"]["v2-b"],
        invocation="invoke-v2-b",
    )

    with pytest.raises(
        prepare.evaluator.GmailFactParityError,
        match="do not share one exact target config",
    ):
        prepare.prepare_gmail_fact_parity_evaluation(
            files["packets"],
            files["cohort"],
            files["admissions"],
            files["cohort_manifest"],
            files["original_inventory"],
            files["v2_inventory"],
            tmp_path / "rejected-bundle",
            run_output_paths=files["outputs"],
            run_receipt_paths=files["receipts"],
        )


def test_evaluator_refuses_label_subset_even_with_refreshed_manifest(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    bundle = files["bundle"]
    labels = [
        json.loads(line) for line in (bundle / "labels.jsonl").read_text().splitlines()
    ]
    _private_write(
        bundle / "labels.jsonl",
        parity.gmail_fact_parity_jsonl_bytes(labels[:-1]),
    )
    evidence = parity.load_gmail_fact_parity_bound_evidence(
        files["packets"],
        files["cohort"],
        files["admissions"],
        files["cohort_manifest"],
        files["original_inventory"],
        files["v2_inventory"],
    )
    runs = parity.load_gmail_fact_parity_runs(
        files["outputs"], files["receipts"], evidence
    )
    judge = parity.load_gmail_fact_parity_judge_receipt(
        bundle / "judge-receipt.json",
        completed_units_sha256=hashlib.sha256(
            (bundle / "completed-units.jsonl").read_bytes()
        ).hexdigest(),
        work_queue_sha256=hashlib.sha256(
            (bundle / "work-queue.jsonl").read_bytes()
        ).hexdigest(),
        evidence=evidence,
        run_evidence=runs,
    )
    forged = parity.build_gmail_fact_parity_manifest(
        labels_bytes=(bundle / "labels.jsonl").read_bytes(),
        alignment_bytes=(bundle / "alignment.jsonl").read_bytes(),
        completed_units_bytes=(bundle / "completed-units.jsonl").read_bytes(),
        work_queue_bytes=(bundle / "work-queue.jsonl").read_bytes(),
        evidence=evidence,
        run_evidence=runs,
        judge_receipt=judge,
        labels=labels[:-1],
    )
    _private_write(bundle / "manifest.json", json.dumps(forged, sort_keys=True))

    with pytest.raises(parity.GmailFactParityError, match="labels do not cover"):
        _evaluate(files)


def test_evaluator_parses_every_output_and_requires_exact_packet_coverage(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    rows = [
        json.loads(line) for line in files["outputs"]["v2-b"].read_text().splitlines()
    ]
    _private_write(
        files["outputs"]["v2-b"],
        parity.gmail_fact_parity_jsonl_bytes(rows[:-1]),
    )
    _receipt(
        files["receipts"]["v2-b"],
        run_id="v2-b",
        output=files["outputs"]["v2-b"],
        invocation="invoke-v2-b",
    )

    with pytest.raises(parity.GmailFactParityError, match="exactly cover every packet"):
        _evaluate(files)


def test_evaluator_rejects_work_queue_or_cohort_tampering(tmp_path: Path) -> None:
    files = _fixture(tmp_path / "queue")
    queue = files["bundle"] / "work-queue.jsonl"
    rows = [json.loads(line) for line in queue.read_text().splitlines()]
    rows[0]["members"][files["aliases"]["v2-a"]][0]["statement"] = (
        "tampered private statement"
    )
    _private_write(queue, parity.gmail_fact_parity_jsonl_bytes(rows))
    with pytest.raises(parity.GmailFactParityError, match="work queue does not match"):
        _evaluate(files)

    files = _fixture(tmp_path / "cohort")
    _private_write(files["cohort"], files["cohort"].read_bytes() + b"\n")
    with pytest.raises(parity.GmailFactParityError, match="does not match cohort"):
        _evaluate(files)


def test_evaluator_requires_distinct_receipts_and_unique_invocation_claims(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path / "hardlink")
    hardlink = tmp_path / "hardlink" / "receipt-hardlink.json"
    os.link(files["receipts"]["v2-a"], hardlink)
    receipts = dict(files["receipts"])
    receipts["v2-b"] = hardlink
    with pytest.raises(parity.GmailFactParityError, match="distinct files"):
        parity.evaluate_gmail_fact_parity(
            files["bundle"] / "labels.jsonl",
            files["bundle"] / "manifest.json",
            files["bundle"] / "alignment.jsonl",
            files["bundle"] / "completed-units.jsonl",
            files["bundle"] / "judge-receipt.json",
            files["bundle"] / "work-queue.jsonl",
            files["cohort"],
            files["packets"],
            files["admissions"],
            files["cohort_manifest"],
            files["original_inventory"],
            files["v2_inventory"],
            files["outputs"],
            receipts,
        )

    files = _fixture(tmp_path / "claim")
    value = json.loads(files["receipts"]["v2-b"].read_text())
    value["invocation_id"] = "invoke-v2-a"
    _private_write(files["receipts"]["v2-b"], json.dumps(value, sort_keys=True))
    with pytest.raises(parity.GmailFactParityError, match="invocation IDs"):
        _evaluate(files)


def test_external_provider_and_sol_medium_judge_are_enforced(tmp_path: Path) -> None:
    files = _fixture(tmp_path / "provider")
    receipt = json.loads(files["receipts"]["v2-b"].read_text())
    receipt["provider"] = "local-model"
    _private_write(files["receipts"]["v2-b"], json.dumps(receipt, sort_keys=True))
    with pytest.raises(parity.GmailFactParityError, match="does not match"):
        _evaluate(files)

    files = _fixture(tmp_path / "judge")
    judge = files["bundle"] / "judge-receipt.json"
    receipt = json.loads(judge.read_text())
    receipt["model"] = "gpt-5.6-luna"
    _private_write(judge, json.dumps(receipt, sort_keys=True))
    with pytest.raises(parity.GmailFactParityError, match="required external judge"):
        _evaluate(files)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("work_queue_sha256", "0" * 64),
        ("completed_units_sha256", "1" * 64),
        ("cohort_sha256", "2" * 64),
        ("packet_sha256", "3" * 64),
        ("judge_contract_version", "gmail_fact_parity_judge_contract_v999"),
        ("judge_contract_sha256", "4" * 64),
    ],
    ids=(
        "work-queue",
        "completed-units",
        "cohort",
        "packet",
        "contract-version",
        "contract-digest",
    ),
)
def test_judge_receipt_tampering_breaks_its_evidence_binding(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    files = _fixture(tmp_path)
    path = files["bundle"] / "judge-receipt.json"
    receipt = json.loads(path.read_text())
    receipt[field] = replacement
    _private_write(path, json.dumps(receipt, sort_keys=True))

    with pytest.raises(
        parity.GmailFactParityError,
        match="does not match the required external judge",
    ):
        _evaluate(files)


@pytest.mark.parametrize(
    ("artifact", "error"),
    [
        ("admissions", "opaque admission joins"),
        ("original_inventory", "original admission inventory"),
        ("v2_inventory", "V2 admission inventory"),
    ],
    ids=("admission-join", "original-inventory", "v2-inventory"),
)
def test_admission_and_raw_inventory_tampering_breaks_cohort_provenance(
    tmp_path: Path,
    artifact: str,
    error: str,
) -> None:
    files = _fixture(tmp_path)
    path = files[artifact]
    _private_write(path, path.read_bytes() + b"\n")

    with pytest.raises(parity.GmailFactParityError, match=error):
        _evaluate(files)


def test_empty_v2_output_covers_every_packet_and_is_labeled(tmp_path: Path) -> None:
    files = _fixture(tmp_path, empty_runs=frozenset({"v2-c"}))

    output_rows = [
        json.loads(line)
        for line in files["outputs"]["v2-c"].read_text().splitlines()[1:]
    ]
    assert len(output_rows) == len(files["packets_value"])
    assert all(row["members"] == [] for row in output_rows)
    labels = [
        json.loads(line)
        for line in (files["bundle"] / "labels.jsonl").read_text().splitlines()
    ]
    v2_alias = files["aliases"]["v2-c"]
    assert all(row["v2"][v2_alias]["members"] == [] for row in labels)

    result = _evaluate(files)

    stage = result["runs"][v2_alias]["stages"]["candidate"]
    assert stage["v2_members"] == 0
    assert stage["retention"] == 0.0
    assert stage["precision"] is None
    assert result["gate_passed"] is False


def test_all_empty_packet_gets_explicit_not_fact_label(tmp_path: Path) -> None:
    files = _fixture(
        tmp_path,
        unit_count=100,
        empty_packet_indexes=frozenset({0}),
    )

    labels = [
        json.loads(line)
        for line in (files["bundle"] / "labels.jsonl").read_text().splitlines()
    ]
    empty_labels = [
        row
        for row in labels
        if not row["original"]["members"]
        and all(not arm["members"] for arm in row["v2"].values())
    ]
    assert len(labels) == 100
    assert len(empty_labels) == 1
    assert empty_labels[0]["useful"] is False
    assert empty_labels[0]["classification"] == "not_fact"
    assert _evaluate(files)["gate_passed"] is True


def test_member_ids_are_adapter_derived_not_model_authored(tmp_path: Path) -> None:
    files = _fixture(tmp_path, unit_count=3)
    raw_rows = [
        json.loads(line)
        for line in files["outputs"]["v2-a"].read_text().splitlines()[1:]
    ]
    assert all(
        "member_id" not in member for row in raw_rows for member in row["members"]
    )
    queue = [
        json.loads(line)
        for line in (files["bundle"] / "work-queue.jsonl").read_text().splitlines()
    ]
    expected = parity.gmail_fact_parity_member_id(
        "v2-a",
        raw_rows[0]["packet_id"],
        0,
        raw_rows[0]["members"][0],
    )
    assert queue[0]["members"][files["aliases"]["v2-a"]][0]["member_id"] == expected


def test_precision_counts_duplicate_members_and_gate_fails(tmp_path: Path) -> None:
    files = _fixture(tmp_path, extra_member_run="v2-a")

    result = _evaluate(files)
    v2_alias = files["aliases"]["v2-a"]

    candidate = result["runs"][v2_alias]["stages"]["candidate"]
    assert candidate["v2_members"] == 21
    assert candidate["supported_scope_correct_members"] == 20
    assert candidate["precision"] == 20 / 21
    assert candidate["duplicate_members"] == 1
    assert result["runs"][v2_alias]["gates"]["no_duplicate_members"] is False
    assert result["gate_passed"] is False


def test_exact_duplicate_split_across_judge_units_still_fails_structural_gate(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path / "base", unit_count=100, completed=False)
    duplicate_run_id = "v2-a"
    output = files["outputs"][duplicate_run_id]
    output_rows = [json.loads(line) for line in output.read_text().splitlines()]
    output_rows[1]["members"].append(dict(output_rows[1]["members"][0]))
    _private_write(output, parity.gmail_fact_parity_jsonl_bytes(output_rows))
    _receipt(
        files["receipts"][duplicate_run_id],
        run_id=duplicate_run_id,
        output=output,
        invocation=f"invoke-{duplicate_run_id}",
    )

    evidence = parity.load_gmail_fact_parity_bound_evidence(
        files["packets"],
        files["cohort"],
        files["admissions"],
        files["cohort_manifest"],
        files["original_inventory"],
        files["v2_inventory"],
    )
    runs = parity.load_gmail_fact_parity_runs(
        files["outputs"], files["receipts"], evidence
    )
    duplicate_alias = runs["run_aliases"][duplicate_run_id]
    first_packet_id = files["packets_value"][0]["packet_id"]
    completed_rows = []
    for packet in files["packets_value"]:
        packet_id = packet["packet_id"]
        members = {
            runs["run_aliases"][run_id]: [
                {
                    "member_id": member["member_id"],
                    "supported": True,
                    "scope_correct": True,
                    "critical_error": "none",
                }
                for member in runs["runs"][run_id]["packets"][packet_id]["members"]
            ]
            for run_id in runs["all_run_ids"]
        }
        if packet_id == first_packet_id:
            duplicate_judgments = members[duplicate_alias]
            assert len(duplicate_judgments) == 2
            members[duplicate_alias] = [duplicate_judgments[0]]
            completed_rows.append(
                {
                    "version": parity.COMPLETED_UNIT_VERSION,
                    "packet_id": packet_id,
                    "useful": True,
                    "classification": "non_temporal",
                    "members": members,
                }
            )
            duplicate_only = {alias: [] for alias in runs["all_run_aliases"]}
            duplicate_only[duplicate_alias] = [duplicate_judgments[1]]
            completed_rows.append(
                {
                    "version": parity.COMPLETED_UNIT_VERSION,
                    "packet_id": packet_id,
                    "useful": True,
                    "classification": "non_temporal",
                    "members": duplicate_only,
                }
            )
        else:
            completed_rows.append(
                {
                    "version": parity.COMPLETED_UNIT_VERSION,
                    "packet_id": packet_id,
                    "useful": True,
                    "classification": "non_temporal",
                    "members": members,
                }
            )

    completed = tmp_path / "completed-duplicate-units.jsonl"
    _private_write(completed, parity.gmail_fact_parity_jsonl_bytes(completed_rows))
    queue_bytes = parity.gmail_fact_parity_jsonl_bytes(
        parity.build_gmail_fact_parity_work_queue(evidence, runs)
    )
    judge_receipt = tmp_path / "duplicate-judge-receipt.json"
    _judge_receipt(
        judge_receipt,
        completed_units=completed,
        evidence=evidence,
        work_queue=queue_bytes,
    )
    bundle = tmp_path / "duplicate-evaluation-bundle"
    prepare.prepare_gmail_fact_parity_evaluation(
        files["packets"],
        files["cohort"],
        files["admissions"],
        files["cohort_manifest"],
        files["original_inventory"],
        files["v2_inventory"],
        bundle,
        run_output_paths=files["outputs"],
        run_receipt_paths=files["receipts"],
        completed_units_path=completed,
        judge_receipt_path=judge_receipt,
    )

    result = _evaluate({**files, "bundle": bundle})
    run = result["runs"][duplicate_alias]

    assert all(stage["duplicate_members"] == 0 for stage in run["stages"].values())
    assert run["structural_exact_duplicate_members"] == 1
    assert run["gates"]["no_duplicate_members"] is False
    assert result["gate_passed"] is False


def test_all_run_stability_fails_when_each_pair_still_passes(tmp_path: Path) -> None:
    files = _fixture(
        tmp_path,
        unit_count=100,
        run_empty_packet_indexes={
            "v2-a": frozenset({0, 1}),
            "v2-b": frozenset({2, 3}),
            "v2-c": frozenset({4, 5}),
        },
    )

    result = _evaluate(files)

    assert result["coverage_gate"]["passed"] is True
    assert all(
        all(checks for checks in run["gates"].values())
        for run in result["runs"].values()
    )
    assert all(item["agreement"] == 0.96 for item in result["run_agreement"])
    assert all(item["passed"] is True for item in result["run_agreement"])
    assert all(
        item["intersection_over_union"] == 0.94 for item in result["all_v2_stability"]
    )
    assert all(item["passed"] is False for item in result["all_v2_stability"])
    assert result["gate_passed"] is False


def test_critical_member_error_fails_release_gate(tmp_path: Path) -> None:
    files = _fixture(
        tmp_path,
        unit_count=100,
        critical_packet_indexes={"v2-a": frozenset({0})},
    )

    result = _evaluate(files)
    run = result["runs"][files["aliases"]["v2-a"]]

    assert run["stages"]["candidate"]["precision"] == 0.99
    assert run["critical_error_members"] == 1
    assert run["critical_error_units"] == 1
    assert run["gates"]["no_critical_errors"] is False
    assert result["gate_passed"] is False


def test_stage_boundary_loss_fails_only_the_affected_stage_gate(
    tmp_path: Path,
) -> None:
    files = _fixture(
        tmp_path,
        unit_count=100,
        run_stage_overrides={
            "v2-a": {
                index: {"candidate": True, "review": True, "persisted": False}
                for index in range(6)
            }
        },
    )

    result = _evaluate(files)
    run = result["runs"][files["aliases"]["v2-a"]]

    assert run["stages"]["candidate"]["retention"] == 1.0
    assert run["stages"]["review"]["retention"] == 1.0
    assert run["stages"]["persisted"]["retention"] == 0.94
    assert run["gates"]["candidate_retention"] is True
    assert run["gates"]["review_retention"] is True
    assert run["gates"]["persisted_retention"] is False
    assert result["gate_passed"] is False


def test_cli_stdout_contains_aggregates_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = _fixture(tmp_path / "complete", unit_count=100)
    bundle = files["bundle"]
    evaluator_args = [
        str(EVALUATOR_PATH),
        str(bundle / "labels.jsonl"),
        str(bundle / "manifest.json"),
        str(bundle / "alignment.jsonl"),
        str(bundle / "completed-units.jsonl"),
        str(bundle / "judge-receipt.json"),
        str(bundle / "work-queue.jsonl"),
        str(files["cohort"]),
        str(files["packets"]),
        str(files["admissions"]),
        str(files["cohort_manifest"]),
        str(files["original_inventory"]),
        str(files["v2_inventory"]),
    ]
    for run_id in RUN_IDS:
        evaluator_args.extend(["--run-output", f"{run_id}={files['outputs'][run_id]}"])
        evaluator_args.extend(
            ["--run-receipt", f"{run_id}={files['receipts'][run_id]}"]
        )
    monkeypatch.setattr(sys, "argv", evaluator_args)
    parity.main()
    evaluator_stdout = capsys.readouterr().out
    assert json.loads(evaluator_stdout)["gate_passed"] is True
    assert "PRIVATE_FACT_SENTINEL" not in evaluator_stdout
    assert str(tmp_path) not in evaluator_stdout

    queue_only = tmp_path / "queue-only"
    preparer_args = [
        str(PREPARER_PATH),
        str(files["packets"]),
        str(files["cohort"]),
        str(files["admissions"]),
        str(files["cohort_manifest"]),
        str(files["original_inventory"]),
        str(files["v2_inventory"]),
        str(queue_only),
    ]
    for run_id in RUN_IDS:
        preparer_args.extend(["--run-output", f"{run_id}={files['outputs'][run_id]}"])
        preparer_args.extend(["--run-receipt", f"{run_id}={files['receipts'][run_id]}"])
    monkeypatch.setattr(sys, "argv", preparer_args)
    prepare.main()
    preparer_stdout = capsys.readouterr().out
    assert json.loads(preparer_stdout)["release_evidence_ready"] is False
    assert "PRIVATE_FACT_SENTINEL" not in preparer_stdout
    assert str(tmp_path) not in preparer_stdout


def test_cli_exits_nonzero_after_printing_a_failed_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = _fixture(tmp_path, unit_count=100, empty_runs=frozenset({"v2-c"}))
    bundle = files["bundle"]
    evaluator_args = [
        str(EVALUATOR_PATH),
        str(bundle / "labels.jsonl"),
        str(bundle / "manifest.json"),
        str(bundle / "alignment.jsonl"),
        str(bundle / "completed-units.jsonl"),
        str(bundle / "judge-receipt.json"),
        str(bundle / "work-queue.jsonl"),
        str(files["cohort"]),
        str(files["packets"]),
        str(files["admissions"]),
        str(files["cohort_manifest"]),
        str(files["original_inventory"]),
        str(files["v2_inventory"]),
    ]
    for run_id in files["run_ids"]:
        evaluator_args.extend(["--run-output", f"{run_id}={files['outputs'][run_id]}"])
        evaluator_args.extend(
            ["--run-receipt", f"{run_id}={files['receipts'][run_id]}"]
        )
    monkeypatch.setattr(sys, "argv", evaluator_args)

    with pytest.raises(SystemExit) as exc_info:
        parity.main()

    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["gate_passed"] is False


def test_public_result_redacts_arbitrary_original_metadata_and_run_ids(
    tmp_path: Path,
) -> None:
    run_ids = (
        "PRIVATE_ORIGINAL_RUN_ID",
        "PRIVATE_V2_RUN_ALPHA",
        "PRIVATE_V2_RUN_BRAVO",
        "PRIVATE_V2_RUN_CHARLIE",
    )
    original_model = "PRIVATE_ORIGINAL_MODEL_SENTINEL"
    original_effort = "PRIVATE_ORIGINAL_EFFORT_SENTINEL"
    files = _fixture(
        tmp_path,
        unit_count=20,
        run_ids=run_ids,
        original_run_id=run_ids[0],
        original_model=original_model,
        original_reasoning_effort=original_effort,
    )

    result = _evaluate(files)
    serialized = json.dumps(result, sort_keys=True)
    original_alias = files["aliases"][run_ids[0]]

    assert all(run_id not in serialized for run_id in run_ids)
    assert original_model not in serialized
    assert original_effort not in serialized
    assert result["evidence_execution"][original_alias]["model"] == "other"
    assert result["evidence_execution"][original_alias]["reasoning_effort"] == "other"


def test_run_artifact_argument_parser_rejects_ambiguity() -> None:
    assert parity._run_artifact_arguments(["v2-a=/private/a.jsonl"]) == {
        "v2-a": Path("/private/a.jsonl")
    }
    with pytest.raises(parity.GmailFactParityError, match="RUN_ID=PATH"):
        parity._run_artifact_arguments(["v2-a"])
    with pytest.raises(parity.GmailFactParityError, match="invalid"):
        parity._run_artifact_arguments(["v2-a=/a", "v2-a=/b"])
