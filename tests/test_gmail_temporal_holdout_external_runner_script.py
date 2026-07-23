from __future__ import annotations

import copy
import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from test_gmail_temporal_holdout_candidate_gold_adapter_script import (
    _fixture as candidate_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_gmail_temporal_holdout_external.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_gmail_temporal_holdout_external_runner", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load()


def _label_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": row["sample_id"],
        **{
            field: copy.deepcopy(row[field]) for field in runner.finalizer._LABEL_FIELDS
        },
    }


def _label_invoker(completed: dict[str, dict[str, Any]], seen: list[str]):
    def invoke(
        request: dict[str, Any],
        _schema: dict[str, Any],
        model: str,
        effort: str,
        _timeout: int,
    ) -> dict[str, Any]:
        assert model == runner.LABEL_MODEL
        assert effort == "medium"
        serialized = json.dumps(request, sort_keys=True)
        seen.append(serialized)
        return {
            "version": runner.LABEL_RESPONSE_VERSION,
            "labels": [
                _label_fields(completed[str(row["sample_id"])])
                for row in request["records"]
            ],
        }

    return invoke


def _verifier_invoker(calls: list[dict[str, Any]]):
    def invoke(
        request: dict[str, Any],
        _schema: dict[str, Any],
        model: str,
        effort: str,
        _timeout: int,
    ) -> dict[str, Any]:
        assert model == runner.VERIFIER_MODEL
        assert effort == runner.VERIFIER_REASONING_EFFORT
        calls.append(copy.deepcopy(request))
        pages = []
        for payload in request["requests"]:
            candidate_ids = [
                candidate_id
                for cluster in payload["page"]["clusters"]
                for candidate_id in cluster["candidate_ids"]
            ]
            pages.append(
                {
                    "request_fingerprint": payload["request_fingerprint"],
                    "verdicts": [
                        {"candidate_id": candidate_id, "verdict": "unsupported"}
                        for candidate_id in candidate_ids
                    ],
                }
            )
        return {"version": runner.VERIFIER_RESPONSE_VERSION, "pages": pages}

    return invoke


def _assert_owner_only_tree(root: Path) -> None:
    for path in root.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path


def test_labels_happy_path_is_source_only_resumable_and_finalizer_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_base = tmp_path / "fixture"
    fixture = candidate_fixture(
        fixture_base,
        monkeypatch,
        include_challenge=True,
    )
    completed_rows = []
    for name in ("completed.jsonl", "completed-challenge.jsonl"):
        completed_rows.extend(
            json.loads(line)
            for line in (fixture_base / name).read_text(encoding="utf-8").splitlines()
        )
    completed = {str(row["sample_id"]): row for row in completed_rows}

    # The source-only phase must not even hash/read evaluation-authority content.
    internal_path = fixture["root"] / "evaluation-authority/primary-samples.jsonl"
    internal_original = internal_path.read_bytes()
    private_internal_marker = "PIPELINE_ONLY_PRIVATE_MARKER"
    internal_path.write_text(private_internal_marker, encoding="utf-8")
    internal_path.chmod(0o600)

    seen: list[str] = []
    output = tmp_path / "label-run"
    result = runner.run_labels(
        fixture["root"],
        fixture["key"],
        output,
        batch_size=8,
        concurrency=2,
        invoke=_label_invoker(completed, seen),
    )

    assert result["status"] == "complete"
    assert result["primary_records"] == 1
    assert result["challenge_records"] == 1
    assert len(seen) == 2  # cohorts are never mixed in one labeling call
    assert all(private_internal_marker not in request for request in seen)
    authority, _ = runner.finalizer._load_label_authority_manifest(
        output / "label-authority.json",
        key=fixture["key_value"],
        source_holdout_manifest_sha256=runner._sha256_bytes(
            (fixture["root"] / "manifest.json").read_bytes()
        ),
        source_primary_label_queue_sha256=runner._sha256_bytes(
            (fixture["root"] / "label-queue/primary.jsonl").read_bytes()
        ),
        source_challenge_label_queue_sha256=runner._sha256_bytes(
            (fixture["root"] / "label-queue/challenge.jsonl").read_bytes()
        ),
        completed_labels_sha256=runner._sha256_bytes(
            (output / "completed-primary.jsonl").read_bytes()
        ),
        completed_challenge_labels_sha256=runner._sha256_bytes(
            (output / "completed-challenge.jsonl").read_bytes()
        ),
        source_primary_label_queue_raw=(
            fixture["root"] / "label-queue/primary.jsonl"
        ).read_bytes(),
        source_challenge_label_queue_raw=(
            fixture["root"] / "label-queue/challenge.jsonl"
        ).read_bytes(),
        completed_labels_raw=(output / "completed-primary.jsonl").read_bytes(),
        completed_challenge_labels_raw=(
            output / "completed-challenge.jsonl"
        ).read_bytes(),
    )
    assert authority["source_only_labeling"] is True
    assert authority["internal_evaluation_artifacts_inspected"] is False
    assert authority["receipt_set_sha256"] == runner.recompute_call_set_hashes(
        output,
        fixture["key"],
    )["receipt_set_sha256"]

    # Restore the unrelated frozen artifact, then prove the ordinary finalizer
    # consumes the runner outputs without any translation.
    internal_path.write_bytes(internal_original)
    internal_path.chmod(0o600)
    runner.finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        output / "completed-primary.jsonl",
        fixture["key"],
        tmp_path / "finalized-gold",
        completed_challenge_labels_path=output / "completed-challenge.jsonl",
        label_authority_manifest_path=output / "label-authority.json",
    )

    # Copying only the aggregate artifacts cannot detach them from their signed
    # plan/start/receipt ledger and manufacture a new chronology.
    moved = tmp_path / "moved-label-aggregates"
    moved.mkdir(mode=0o700)
    for name in (
        "completed-primary.jsonl",
        "completed-challenge.jsonl",
        "label-authority.json",
    ):
        target = moved / name
        target.write_bytes((output / name).read_bytes())
        target.chmod(0o600)
    with pytest.raises(
        runner.finalizer.GmailTemporalLabelFinalizerError,
        match="label evidence plan",
    ):
        runner.finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            moved / "completed-primary.jsonl",
            fixture["key"],
            tmp_path / "moved-must-not-finalize",
            completed_challenge_labels_path=moved / "completed-challenge.jsonl",
            label_authority_manifest_path=moved / "label-authority.json",
        )

    receipt_path = next(output.glob("calls/*/attempt-*/receipt.json"))
    receipt = json.loads(receipt_path.read_bytes())
    receipt["completed_at"] = "2099-01-01T00:00:00+00:00"
    receipt_path.write_bytes(runner._canonical_json(receipt) + b"\n")
    receipt_path.chmod(0o600)
    with pytest.raises(
        runner.finalizer.GmailTemporalLabelFinalizerError,
        match="label evidence receipt is invalid",
    ):
        runner.finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            output / "completed-primary.jsonl",
            fixture["key"],
            tmp_path / "tampered-receipt-must-not-finalize",
            completed_challenge_labels_path=output / "completed-challenge.jsonl",
            label_authority_manifest_path=output / "label-authority.json",
        )
    _assert_owner_only_tree(output)


def _prepared_primary_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    fixture_base = tmp_path / "fixture"
    fixture = candidate_fixture(fixture_base, monkeypatch)
    for name in (
        "PRIMARY_MIN_LABELED_HARD_NEGATIVES",
        "CHALLENGE_MIN_EXPECTED_MATERIAL_RECORDS",
        "CHALLENGE_MIN_SEMANTIC_MEMBERS",
        "CHALLENGE_MIN_SUPPORTED_MEMBERS",
        "CHALLENGE_MIN_LABELED_HARD_NEGATIVES",
    ):
        monkeypatch.setattr(
            runner.finalizer,
            name,
            getattr(runner.adapter.finalizer, name),
        )
    completed_rows = [
        json.loads(line)
        for line in (fixture_base / "completed.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    completed = {str(row["sample_id"]): row for row in completed_rows}
    label_run = tmp_path / "label-run"
    runner.run_labels(
        fixture["root"],
        fixture["key"],
        label_run,
        invoke=_label_invoker(completed, []),
    )
    trusted_gold = tmp_path / "trusted-gold"
    runner.finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        label_run / "completed-primary.jsonl",
        fixture["key"],
        trusted_gold,
        completed_challenge_labels_path=label_run / "completed-challenge.jsonl",
        label_authority_manifest_path=label_run / "label-authority.json",
    )
    fixture["gold"] = trusted_gold
    runner.adapter.prepare_gmail_temporal_holdout_candidate_gold(
        fixture["root"],
        fixture["gold"],
        fixture["key"],
        fixture["output"],
        cohort="primary",
    )
    return fixture


def test_verifier_happy_path_exact_checkpoint_receipts_and_v2_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_primary_fixture(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []
    output = tmp_path / "verifier-run"

    result = runner.run_verifier(
        fixture["root"],
        fixture["output"],
        fixture["key"],
        output,
        cohort="primary",
        run_ordinal=1,
        batch_size=4,
        concurrency=2,
        invoke=_verifier_invoker(calls),
    )

    assert result["status"] == "complete"
    assert result["frozen_requests"] == result["checkpoint_rows"]
    assert result["external_calls"] == len(calls)
    attestation, _ = runner.load_verifier_attestation_v2(
        output / "attestation.json",
        key=fixture["key_value"],
        adapter_manifest_sha256=runner._sha256_bytes(
            (fixture["output"] / "manifest.json").read_bytes()
        ),
        checkpoint_sha256=runner._sha256_bytes(
            (output / "checkpoint.jsonl").read_bytes()
        ),
        cohort="primary",
        run_ordinal=1,
        frozen_request_artifact_sha256=runner._sha256_bytes(
            (
                fixture["root"] / "evaluation-authority/primary-requests.jsonl"
            ).read_bytes()
        ),
        checkpoint_row_count=result["checkpoint_rows"],
        retained_run_root=output,
    )
    assert attestation["external_calls"] == len(calls)
    assert attestation["exact_request_coverage"] is True
    partition = json.loads((output / "plan.json").read_bytes())["units"]
    assert [item for unit in partition for item in unit["item_ids"]] == [
        json.loads(line)["request_fingerprint"]
        for line in (fixture["root"] / "evaluation-authority/primary-requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    _assert_owner_only_tree(output)


def test_verifier_refuses_gold_without_sealed_source_only_label_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = candidate_fixture(tmp_path / "fixture", monkeypatch)
    runner.adapter.prepare_gmail_temporal_holdout_candidate_gold(
        fixture["root"],
        fixture["gold"],
        fixture["key"],
        fixture["output"],
        cohort="primary",
    )
    calls: list[dict[str, Any]] = []
    with pytest.raises(
        runner.GmailTemporalExternalRunnerError,
        match="adapter binding is invalid",
    ):
        runner.run_verifier(
            fixture["root"],
            fixture["output"],
            fixture["key"],
            tmp_path / "must-not-run",
            cohort="primary",
            run_ordinal=1,
            invoke=_verifier_invoker(calls),
        )
    assert calls == []


def test_interrupted_response_is_recovered_without_a_second_external_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_primary_fixture(tmp_path, monkeypatch)
    output = tmp_path / "interrupted-run"
    calls: list[dict[str, Any]] = []
    original_write = runner._write_private_new
    failed = False

    def interrupt_receipt(path: Path, payload: bytes) -> None:
        nonlocal failed
        if path.name == "receipt.json" and not failed:
            failed = True
            raise OSError("simulated crash after durable response")
        original_write(path, payload)

    monkeypatch.setattr(runner, "_write_private_new", interrupt_receipt)
    with pytest.raises(OSError, match="simulated crash"):
        runner.run_verifier(
            fixture["root"],
            fixture["output"],
            fixture["key"],
            output,
            cohort="primary",
            run_ordinal=1,
            invoke=_verifier_invoker(calls),
        )
    assert len(calls) == 1
    monkeypatch.setattr(runner, "_write_private_new", original_write)

    def must_not_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("valid durable response should be recovered")

    result = runner.run_verifier(
        fixture["root"],
        fixture["output"],
        fixture["key"],
        output,
        cohort="primary",
        run_ordinal=1,
        invoke=must_not_call,
    )
    assert result["external_calls"] == 1
    assert result["retry_calls"] == 0
    receipt = next(output.glob("calls/*/attempt-*/receipt.json"))
    assert json.loads(receipt.read_bytes())["status"] == "success"


def test_v2_attestation_and_retained_receipt_tampering_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_primary_fixture(tmp_path, monkeypatch)
    output = tmp_path / "tamper-run"
    runner.run_verifier(
        fixture["root"],
        fixture["output"],
        fixture["key"],
        output,
        cohort="primary",
        run_ordinal=1,
        invoke=_verifier_invoker([]),
    )
    attestation_path = output / "attestation.json"
    attestation = json.loads(attestation_path.read_bytes())
    tampered = {**attestation, "receipt_set_sha256": "0" * 64}
    with pytest.raises(
        runner.GmailTemporalExternalRunnerError,
        match="attestation is invalid",
    ):
        runner.validate_verifier_attestation_v2(
            tampered,
            key=fixture["key_value"],
            adapter_manifest_sha256=attestation["adapter_manifest_sha256"],
            checkpoint_sha256=attestation["checkpoint_sha256"],
            cohort="primary",
            run_ordinal=1,
        )

    # Even a re-signed false aggregate cannot disagree with retained receipts.
    resigned = runner._signed_value(
        tampered,
        key=fixture["key_value"],
        domain=runner.VERIFIER_ATTESTATION_V2_DOMAIN,
        signature_field="attestation_hmac_sha256",
    )
    runner.validate_verifier_attestation_v2(
        resigned,
        key=fixture["key_value"],
        adapter_manifest_sha256=attestation["adapter_manifest_sha256"],
        checkpoint_sha256=attestation["checkpoint_sha256"],
        cohort="primary",
        run_ordinal=1,
    )
    with pytest.raises(
        runner.GmailTemporalExternalRunnerError,
        match="retained call receipts",
    ):
        runner.validate_retained_call_set(
            resigned,
            run_root=output,
            key=fixture["key_value"],
        )


def test_serialized_request_ceiling_splits_and_rejects_oversize_singletons() -> None:
    rows = [{"id": index, "text": "x" * 80} for index in range(3)]

    def request(values: list[dict[str, Any]]) -> dict[str, Any]:
        return {"records": values}

    batches = runner._bounded_batches(
        rows,
        max_items=3,
        max_request_bytes=230,
        request_factory=request,
    )
    assert [len(batch) for batch in batches] == [2, 1]
    with pytest.raises(
        runner.GmailTemporalExternalRunnerError,
        match="serialized-byte ceiling",
    ):
        runner._bounded_batches(
            [{"text": "x" * 500}],
            max_items=1,
            max_request_bytes=100,
            request_factory=request,
        )


def test_verifier_plan_and_start_must_strictly_postdate_label_completion() -> None:
    label_completed = "2026-07-23T12:00:00+00:00"
    equal_plan = {
        "phase": "verify",
        "created_at": label_completed,
        "inputs": {
            "label_chronology_verified": True,
            "label_completed_at": label_completed,
        },
    }
    with pytest.raises(
        runner.GmailTemporalExternalRunnerError,
        match="does not postdate",
    ):
        runner._validate_verifier_plan_chronology(equal_plan)

    plan = {
        **equal_plan,
        "logical_run_id": "gthxr_r_" + "a" * 64,
        "created_at": "2026-07-23T12:00:01+00:00",
        "model": runner.VERIFIER_MODEL,
        "reasoning_effort": runner.VERIFIER_REASONING_EFFORT,
    }
    runner._validate_verifier_plan_chronology(plan)
    unit = runner.PlanUnit(
        unit_id="gthxu_" + "b" * 64,
        cohort="primary",
        ordinal=1,
        item_ids=("request",),
        item_sha256=("c" * 64,),
        request={"phase": "verify"},
        expected={},
    )
    request_sha256 = runner._sha256_bytes(runner._canonical_json(unit.request) + b"\n")
    invalid_start = runner._signed_value(
        runner._start_value(
            plan=plan,
            unit=unit,
            attempt_ordinal=1,
            invocation_id="gthvr_i_" + "d" * 64,
            request_sha256=request_sha256,
            started_at=label_completed,
        ),
        key=b"k" * 32,
        domain=runner.CALL_START_DOMAIN,
        signature_field="start_hmac_sha256",
    )
    with pytest.raises(
        runner.GmailTemporalExternalRunnerError,
        match="start marker is invalid",
    ):
        runner._validate_start(
            invalid_start,
            key=b"k" * 32,
            plan=plan,
            unit=unit,
            request_sha256=request_sha256,
        )


def test_cli_failure_never_prints_private_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "PRIVATE-MAIL-SHOULD-NOT-PRINT"

    def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise runner.GmailTemporalExternalRunnerError(private_marker)

    monkeypatch.setattr(runner, "run_labels", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_temporal_holdout_external.py",
            "labels",
            "--holdout-root",
            "/missing/holdout",
            "--hmac-key",
            "/missing/key",
            "--output-root",
            "/missing/output",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    output = capsys.readouterr().out
    assert private_marker not in output
    assert json.loads(output) == runner._safe_failure("labels")
