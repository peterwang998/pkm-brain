from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "audit_gmail_temporal_public_gold.py"
SPEC = importlib.util.spec_from_file_location(
    "test_gmail_temporal_public_gold_audit",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def _write_private(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _inputs(
    tmp_path: Path,
    *,
    variant: int,
    fixture: dict[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    fixture_path = tmp_path / "fixture.json"
    if fixture is None:
        audit.scale_builder.write_fixture(fixture_path, variant=variant)
        fixture = audit.scale_builder.build_fixture(variant)
    else:
        _write_private(
            fixture_path,
            audit._canonical_json(fixture) + b"\n",  # noqa: SLF001
        )
    key_path = _write_private(
        tmp_path / "key",
        b"public-gold-audit-test-key-at-least-32-bytes",
    )
    return fixture_path, key_path, fixture


def _valid_disposition() -> dict[str, Any]:
    return {
        "disposition": "valid",
        "issue_codes": ["none"],
        "rationale": "The proposed label is source-supported.",
    }


def _response(
    request: dict[str, Any],
    *,
    corrected_case: str | None = None,
) -> dict[str, Any]:
    cases = []
    for row in request["cases"]:
        case = _valid_disposition()
        members = [
            {"member_ordinal": ordinal, **_valid_disposition()}
            for ordinal, _ in enumerate(row["proposed_gold"]["members"])
        ]
        if row["case_id"] == corrected_case:
            case = {
                "disposition": "correction_needed",
                "issue_codes": ["wrong_value"],
                "rationale": "One normalized value needs correction.",
            }
            members[0] = {
                "member_ordinal": 0,
                "disposition": "correction_needed",
                "issue_codes": ["wrong_value"],
                "rationale": "The normalized value differs from the source.",
            }
        cases.append(
            {
                "case_id": row["case_id"],
                **case,
                "members": members,
                "forbidden_bindings": [
                    {"forbidden_ordinal": ordinal, **_valid_disposition()}
                    for ordinal, _ in enumerate(row["proposed_gold"]["forbidden"])
                ],
                "group_flag": _valid_disposition(),
            }
        )
    return {"version": audit.RESPONSE_VERSION, "cases": cases}


def test_canonical_fixture_and_generator_hash_allowlists_are_exact() -> None:
    generator_raw = audit._approved_scale_builder_source()  # noqa: SLF001
    assert hashlib.sha256(generator_raw).hexdigest() == (
        audit._APPROVED_SCALE_BUILDER_SHA256  # noqa: SLF001
    )
    for variant, expected_sha256 in audit._ALLOWED_SCALE_FIXTURE_SHA256.items():  # noqa: SLF001
        fixture = audit.scale_builder.build_fixture(variant)
        raw = audit._canonical_json(fixture) + b"\n"  # noqa: SLF001
        assert hashlib.sha256(raw).hexdigest() == expected_sha256
        assert len(fixture["cases"]) == 100


def test_response_schema_uses_provider_supported_array_constraints() -> None:
    schema = audit._response_schema()  # noqa: SLF001

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert "uniqueItems" not in value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    with pytest.raises(audit.PublicGoldAuditError, match="response is invalid"):
        audit._validate_disposition(  # noqa: SLF001
            {
                "disposition": "correction_needed",
                "issue_codes": ["wrong_value", "wrong_value"],
                "rationale": "Duplicate codes remain invalid at runtime.",
            },
            label="member",
        )


def test_external_call_pacing_enforces_minimum_start_interval() -> None:
    interval = audit.MIN_EXTERNAL_CALL_START_INTERVAL_SECONDS
    assert audit._external_call_delay(None, 100.0) == 0.0  # noqa: SLF001
    assert audit._external_call_delay(100.0, 100.0 + interval) == 0.0  # noqa: SLF001
    assert audit._external_call_delay(100.0, 105.0) == pytest.approx(  # noqa: SLF001
        interval - 5.0
    )


def test_real_100_case_audit_is_blind_bounded_pinned_and_hmac_sealed(
    tmp_path: Path,
) -> None:
    variant = 2
    fixture_path, key_path, fixture = _inputs(tmp_path, variant=variant)
    expected_by_id = {row["case_id"]: row for row in fixture["cases"]}
    corrected_case = "v002-positive-schedule-01"
    calls: list[dict[str, Any]] = []

    def invoke(
        request: dict[str, Any],
        schema: dict[str, Any],
        model: str,
        reasoning_effort: str,
        timeout: int,
    ) -> dict[str, Any]:
        calls.append(request)
        assert schema["properties"]["version"]["const"] == audit.RESPONSE_VERSION
        assert model == "gpt-5.6-sol"
        assert reasoning_effort == "medium"
        assert timeout == audit.DEFAULT_TIMEOUT_SECONDS
        assert request["pipeline_predictions_present"] is False
        assert request["public_synthetic"] is True
        assert request["contains_private_gmail"] is False
        assert "predictions" not in request
        assert "prediction_artifacts" not in request
        for case in request["cases"]:
            expected = expected_by_id[case["case_id"]]
            assert case["source"] == {
                "sender": expected["sender"],
                "subject": expected["subject"],
                "body": expected["body"],
                "label_ids": expected["label_ids"],
            }
            assert case["proposed_gold"] == {
                "members": expected["members"],
                "forbidden": expected["forbidden"],
                "complete_group_required": expected["complete_group_required"],
            }
        return _response(request, corrected_case=corrected_case)

    output_root = tmp_path / "audit"
    result = audit.audit_public_gold(
        fixture_path,
        key_path,
        output_root,
        fixture_variant=variant,
        invoke=invoke,
    )

    assert len(calls) == 25
    assert [len(call["cases"]) for call in calls] == [4] * 25
    assert [case["case_id"] for call in calls for case in call["cases"]] == [
        row["case_id"] for row in fixture["cases"]
    ]
    assert result["model"] == "gpt-5.6-sol"
    assert result["reasoning_effort"] == "medium"
    assert result["fixture_variant"] == variant
    assert result["case_count"] == 100
    assert result["valid_case_count"] == 99
    assert result["correction_case_count"] == 1
    assert result["member_count"] == 88
    assert result["valid_member_count"] == 87
    assert result["correction_member_count"] == 1
    assert result["forbidden_binding_count"] == 46
    assert result["external_calls"] == 0
    assert result["test_invoker_used"] is True
    assert result["prediction_artifacts_read"] is False
    assert result["release_eligible"] is False

    assert stat.S_IMODE(output_root.stat().st_mode) == 0o700
    artifacts = [
        output_root / "fixture.json",
        output_root / "audit-plan.json",
        output_root / "audit-detail.json",
        output_root / "audit-summary.json",
        *(output_root / "calls").glob("*/*.json"),
    ]
    assert len(artifacts) == 79
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in artifacts)
    assert (output_root / "fixture.json").read_bytes() == fixture_path.read_bytes()

    key = audit.challenge._key(key_path)  # noqa: SLF001
    plan = json.loads((output_root / "audit-plan.json").read_text(encoding="utf-8"))
    detail = json.loads((output_root / "audit-detail.json").read_text(encoding="utf-8"))
    summary = json.loads(
        (output_root / "audit-summary.json").read_text(encoding="utf-8")
    )
    for artifact in (plan, detail, summary):
        assert artifact["fixture_variant"] == variant
        assert artifact["fixture_generator_version"] == audit.scale_builder.VERSION
        assert artifact["fixture_generator_sha256"] == (
            audit._APPROVED_SCALE_BUILDER_SHA256  # noqa: SLF001
        )
        assert artifact["fixture_generator_exact_bytes_verified"] is True
    assert audit._verify_signed(  # noqa: SLF001
        plan,
        key=key,
        domain=audit.PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
    )
    assert audit._verify_signed(  # noqa: SLF001
        detail,
        key=key,
        domain=audit.DETAIL_DOMAIN,
        signature_field="detail_hmac_sha256",
    )
    assert audit._verify_signed(  # noqa: SLF001
        summary,
        key=key,
        domain=audit.SUMMARY_DOMAIN,
        signature_field="summary_hmac_sha256",
    )
    assert detail["cases"][0]["case_id"] == corrected_case
    assert detail["cases"][0]["members"][0]["disposition"] == "correction_needed"
    for receipt_path in sorted((output_root / "calls").glob("*/receipt.json")):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert audit._verify_signed(  # noqa: SLF001
            receipt,
            key=key,
            domain=audit.RECEIPT_DOMAIN,
            signature_field="receipt_hmac_sha256",
        )


def test_arbitrary_relabeled_example_test_fixture_is_rejected_before_invocation(
    tmp_path: Path,
) -> None:
    fixture = deepcopy(audit.scale_builder.build_fixture(1))
    row = fixture["cases"][0]
    row["subject"] = "Unauthorized example.test event"
    row["body"] = "The Unauthorized example.test event is scheduled for August 1, 2026."
    row["members"][0]["subject"] = "Unauthorized example.test event"
    fixture_path, key_path, _ = _inputs(tmp_path, variant=1, fixture=fixture)
    invoked = False

    def invoke(*_args: Any) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        raise AssertionError

    with pytest.raises(
        audit.PublicGoldAuditError,
        match="not an approved deterministic scale fixture",
    ):
        audit.audit_public_gold(
            fixture_path,
            key_path,
            tmp_path / "audit",
            fixture_variant=1,
            invoke=invoke,
        )

    assert invoked is False
    assert not (tmp_path / "audit").exists()


def test_real_fixture_with_wrong_variant_is_rejected_before_invocation(
    tmp_path: Path,
) -> None:
    fixture_path, key_path, _ = _inputs(tmp_path, variant=2)
    invoked = False

    def invoke(*_args: Any) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        raise AssertionError

    with pytest.raises(
        audit.PublicGoldAuditError,
        match="not an approved deterministic scale fixture",
    ):
        audit.audit_public_gold(
            fixture_path,
            key_path,
            tmp_path / "audit",
            fixture_variant=1,
            invoke=invoke,
        )

    assert invoked is False
    assert not (tmp_path / "audit").exists()


def test_audit_rejects_incomplete_response_and_retains_attempt(
    tmp_path: Path,
) -> None:
    fixture_path, key_path, _ = _inputs(tmp_path, variant=1)

    def invoke(request: dict[str, Any], *_args: Any) -> dict[str, Any]:
        response = _response(request)
        response["cases"] = list(reversed(response["cases"]))
        return response

    output_root = tmp_path / "audit"
    with pytest.raises(audit.PublicGoldAuditError, match="case coverage"):
        audit.audit_public_gold(
            fixture_path,
            key_path,
            output_root,
            fixture_variant=1,
            invoke=invoke,
        )

    assert (output_root / "audit-plan.json").is_file()
    assert (output_root / "calls/001/request.json").is_file()
    assert not (output_root / "calls/001/response.json").exists()
    assert not (output_root / "audit-detail.json").exists()
    assert not (output_root / "audit-summary.json").exists()


def test_audit_requires_a_fresh_exclusive_output_root(tmp_path: Path) -> None:
    fixture_path, key_path, _ = _inputs(tmp_path, variant=1)
    calls = 0

    def invoke(request: dict[str, Any], *_args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _response(request)

    output_root = tmp_path / "audit"
    audit.audit_public_gold(
        fixture_path,
        key_path,
        output_root,
        fixture_variant=1,
        invoke=invoke,
    )
    before = (output_root / "audit-summary.json").read_bytes()

    with pytest.raises(audit.PublicGoldAuditError, match="fresh owner-only"):
        audit.audit_public_gold(
            fixture_path,
            key_path,
            output_root,
            fixture_variant=1,
            invoke=invoke,
        )

    assert calls == 25
    assert (output_root / "audit-summary.json").read_bytes() == before


def test_cli_rejects_unapproved_fixture_with_safe_aggregate_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = deepcopy(audit.scale_builder.build_fixture(1))
    sentinel = "DO-NOT-ECHO-UNAPPROVED-PUBLIC-SOURCE"
    fixture["cases"][0]["body"] = sentinel
    fixture_path, key_path, _ = _inputs(tmp_path, variant=1, fixture=fixture)
    argv = [
        str(MODULE_PATH),
        "--fixture",
        str(fixture_path),
        "--fixture-variant",
        "1",
        "--hmac-key",
        str(key_path),
        "--output-root",
        str(tmp_path / "audit"),
    ]
    previous = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as error:
            audit.main()
    finally:
        sys.argv = previous

    assert error.value.code == 2
    output = capsys.readouterr().out
    assert json.loads(output) == audit._safe_failure()  # noqa: SLF001
    assert sentinel not in output
    assert str(tmp_path) not in output


def test_cli_requires_fixture_variant(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture_path, key_path, _ = _inputs(tmp_path, variant=1)
    argv = [
        str(MODULE_PATH),
        "--fixture",
        str(fixture_path),
        "--hmac-key",
        str(key_path),
        "--output-root",
        str(tmp_path / "audit"),
    ]
    previous = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as error:
            audit.main()
    finally:
        sys.argv = previous

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--fixture-variant" in captured.err
    assert not (tmp_path / "audit").exists()
