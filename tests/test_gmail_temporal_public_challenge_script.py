from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from pkm_brain.db import connection
from pkm_brain.gmail_archive import (
    ArchiveOpenedMessage,
    ArchiveThreadResult,
    ArchiveThreadSnapshot,
)
from pkm_brain.gmail_knowledge import normalize_gmail_thread
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.gmail_temporal_runner import (
    GMAIL_TEMPORAL_VERIFIER_MODEL,
    GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.util import slugify


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "run_gmail_temporal_public_challenge.py"
SPEC = importlib.util.spec_from_file_location(
    "test_gmail_temporal_public_challenge", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
challenge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = challenge
SPEC.loader.exec_module(challenge)
AUDIT_MODULE_PATH = ROOT / "scripts" / "audit_gmail_temporal_public_gold.py"
AUDIT_SPEC = importlib.util.spec_from_file_location(
    "test_gmail_temporal_public_challenge_gold_audit",
    AUDIT_MODULE_PATH,
)
assert AUDIT_SPEC is not None and AUDIT_SPEC.loader is not None
gold_audit = importlib.util.module_from_spec(AUDIT_SPEC)
sys.modules[AUDIT_SPEC.name] = gold_audit
AUDIT_SPEC.loader.exec_module(gold_audit)

ACCOUNT = "owner@public.example.test"
INTERNAL_AT = "2027-09-12T09:00:00-07:00"


class FakeCodex:
    def __init__(
        self,
        *,
        fail_at: int | None = None,
        verdict: str = "unsupported",
    ) -> None:
        self.fail_at = fail_at
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        request: Mapping[str, Any],
        _schema: Mapping[str, Any],
        model: str,
        reasoning_effort: str,
        _timeout: int,
    ) -> Mapping[str, Any]:
        self.calls.append(dict(request))
        assert model == GMAIL_TEMPORAL_VERIFIER_MODEL
        assert reasoning_effort == GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT
        if self.fail_at == len(self.calls):
            raise RuntimeError("synthetic provider failure with no source echo")
        return {
            "version": challenge.external.VERIFIER_RESPONSE_VERSION,
            "pages": [
                {
                    "request_fingerprint": payload["request_fingerprint"],
                    "verdicts": [
                        {
                            "candidate_id": candidate_id,
                            "verdict": self.verdict,
                        }
                        for cluster in payload["page"]["clusters"]
                        for candidate_id in cluster["candidate_ids"]
                    ],
                }
                for payload in request["requests"]
            ],
        }


class SequencedFakeCodex(FakeCodex):
    def __init__(self, verdicts: tuple[str, str, str]) -> None:
        super().__init__()
        self.verdicts = verdicts

    def __call__(
        self,
        request: Mapping[str, Any],
        schema: Mapping[str, Any],
        model: str,
        reasoning_effort: str,
        timeout: int,
    ) -> Mapping[str, Any]:
        self.verdict = self.verdicts[len(self.calls)]
        return super().__call__(request, schema, model, reasoning_effort, timeout)


def _write_key(path: Path) -> Path:
    path.write_bytes(b"public-challenge-test-key-32-bytes-minimum")
    path.chmod(0o600)
    return path


def _ingest_case(
    paths: BrainPaths,
    *,
    case_id: str,
    subject: str,
    body: str,
    labels: tuple[str, ...],
    sender: str,
) -> tuple[str, str]:
    thread_id = f"public-thread-{case_id}"
    message_id = f"public-message-{case_id}"
    revision = hashlib.sha256((case_id + body).encode("utf-8")).hexdigest()
    message = ArchiveOpenedMessage(
        message_id=message_id,
        thread_id=thread_id,
        internal_date=INTERNAL_AT,
        date_header="",
        subject=subject,
        from_addresses=(sender,),
        to_addresses=(ACCOUNT,),
        cc_addresses=(),
        label_ids=labels,
        list_id=(
            "offers.public.example.test" if "CATEGORY_PROMOTIONS" in labels else None
        ),
        list_unsubscribe=(
            "<mailto:unsubscribe@public.example.test>"
            if "CATEGORY_PROMOTIONS" in labels
            else None
        ),
        precedence=None,
        auto_submitted=None,
        body_text=body,
        attachments=(),
        account_key=ACCOUNT,
    )
    snapshot = ArchiveThreadSnapshot(
        thread_id=thread_id,
        source_revision=revision,
        total_message_count=1,
        visible_message_count=1,
        deleted_message_count=0,
        hidden_message_count=0,
        created_at=INTERNAL_AT,
        updated_at=INTERNAL_AT,
        archive_updated_at="2027-09-12T16:00:00+00:00",
        raw_size=len(body),
        account_key=ACCOUNT,
    )
    normalized = normalize_gmail_thread(
        snapshot,
        ArchiveThreadResult(
            thread_id=thread_id,
            total_messages=1,
            messages=(message,),
            truncated=False,
            account_key=ACCOUNT,
        ),
        operator_email=ACCOUNT,
    )
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT,
        thread_id=thread_id,
        source_revision=revision,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    source = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(normalized.markdown, encoding="utf-8")
    result = BrainService(paths).ingest(source=source)
    assert result.errors == []
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE source_path = ? AND status = 'active'",
            (str(source.resolve()),),
        ).fetchone()
    assert row is not None
    return str(row["id"]), message_id


def _fixture(
    tmp_path: Path,
    *,
    all_zero_work: bool = False,
    negative_candidate_bearing: bool = False,
    reschedule: bool = False,
    complete_group_required: bool | None = None,
    expected_verdict: str = "supported",
    gold_subject: str = "public Aster interview",
    canonical_subject_required: bool = True,
) -> dict[str, Path]:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    scheduled = _ingest_case(
        paths,
        case_id="scheduled",
        subject=(
            "Public Aster interview" if reschedule else "Public interview schedule"
        ),
        body=(
            "The public Aster interview was rescheduled to September 22, 2027 "
            "from September 20, 2027. Please keep both endpoints in the "
            "synthetic planning history."
            if reschedule
            else (
                "The public Aster interview is scheduled for September 20, 2027. "
                "Please keep this synthetic appointment on the planning list."
            )
        ),
        labels=(("CATEGORY_PROMOTIONS",) if all_zero_work else ("CATEGORY_PERSONAL",)),
        sender=(
            "offers@public.example.test"
            if all_zero_work
            else "colleague@public.example.test"
        ),
    )
    advertising = _ingest_case(
        paths,
        case_id="advertising",
        subject=(
            "Public Cedar review" if negative_candidate_bearing else "Public sale"
        ),
        body=(
            "The public Cedar review is scheduled for September 21, 2027. "
            "Please keep this synthetic appointment on the planning list."
            if negative_candidate_bearing
            else (
                "Advertisement: this public sale ends September 21, 2027. "
                "Shop now, save 25 percent, and unsubscribe anytime."
            )
        ),
        labels=(
            ("CATEGORY_PERSONAL",)
            if negative_candidate_bearing
            else ("CATEGORY_PROMOTIONS",)
        ),
        sender=(
            "colleague@public.example.test"
            if negative_candidate_bearing
            else "offers@public.example.test"
        ),
    )
    positive_members = (
        [
            {
                "subject": gold_subject,
                "relation": "occurrence",
                "lifecycle": lifecycle,
                "value": value,
                "expected_verdict": expected_verdict,
                "canonical_subject_required": canonical_subject_required,
            }
            for lifecycle, value in (
                ("rescheduled_old", "2027-09-20"),
                ("rescheduled_replacement", "2027-09-22"),
            )
        ]
        if reschedule
        else [
            {
                "subject": gold_subject,
                "relation": "occurrence",
                "lifecycle": "scheduled",
                "value": "2027-09-20",
                "expected_verdict": expected_verdict,
                "canonical_subject_required": canonical_subject_required,
            }
        ]
    )
    positive_case: dict[str, Any] = {
        "case_id": "scheduled",
        "members": positive_members,
        "forbidden": [],
        "complete_group_required": bool(complete_group_required),
    }
    gold = {
        "version": challenge.GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            positive_case,
            {
                "case_id": "advertising",
                "members": [],
                "forbidden": [],
                "complete_group_required": False,
            },
        ],
    }
    gold_raw = challenge._canonical_json(gold) + b"\n"  # noqa: SLF001
    gold_path = tmp_path / "gold.json"
    gold_path.write_bytes(gold_raw)
    gold_path.chmod(0o600)
    scheduled_source = challenge.prepare_gmail_temporal_review(
        paths,
        document_id=scheduled[0],
        gmail_message_id=scheduled[1],
    ).source_sha256
    advertising_source = challenge.prepare_gmail_temporal_review(
        paths,
        document_id=advertising[0],
        gmail_message_id=advertising[1],
    ).source_sha256
    manifest = {
        "version": challenge.CHALLENGE_VERSION,
        "challenge_id": "public-test-fixture",
        "scope": challenge.PUBLIC_SCOPE,
        "created_at": "2027-09-12T17:00:00+00:00",
        "brain_home": str(paths.home),
        "gold_sha256": hashlib.sha256(gold_raw).hexdigest(),
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "cases": [
            {
                "case_id": "scheduled",
                "document_id": scheduled[0],
                "gmail_message_id": scheduled[1],
                "source_sha256": scheduled_source,
            },
            {
                "case_id": "advertising",
                "document_id": advertising[0],
                "gmail_message_id": advertising[1],
                "source_sha256": advertising_source,
            },
        ],
    }
    manifest_path = tmp_path / "challenge.json"
    manifest_raw = challenge._canonical_json(manifest) + b"\n"  # noqa: SLF001
    manifest_path.write_bytes(manifest_raw)
    manifest_path.chmod(0o600)
    key_path = _write_key(tmp_path / "key.bin")
    marker_path = paths.config_local / challenge.PUBLIC_ROOT_AUTHORITY_FILENAME
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "version": challenge.PUBLIC_ROOT_AUTHORITY_VERSION,
        "challenge_id": manifest["challenge_id"],
        "scope": challenge.PUBLIC_SCOPE,
        "created_at": "2027-09-12T17:00:01+00:00",
        "brain_home": str(paths.home.resolve()),
        "challenge_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "gold_sha256": manifest["gold_sha256"],
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "cases": manifest["cases"],
    }
    marker = challenge._signed(  # noqa: SLF001
        marker,
        key=key_path.read_bytes(),
        domain=challenge.PUBLIC_ROOT_AUTHORITY_DOMAIN,
        signature_field="authority_hmac_sha256",
    )
    marker_path.write_bytes(challenge._canonical_json(marker) + b"\n")  # noqa: SLF001
    marker_path.chmod(0o600)
    return {
        "home": paths.home,
        "manifest": manifest_path,
        "gold": gold_path,
        "key": key_path,
        "marker": marker_path,
    }


def _write_frontier_diagnostics(
    fixture: Mapping[str, Path],
    *,
    candidate_count_delta: int = 0,
    fixture_sha256: str | None = None,
) -> Path:
    manifest_raw = fixture["manifest"].read_bytes()
    manifest = challenge._strict_json(  # noqa: SLF001
        manifest_raw,
        label="challenge",
    )
    gold_raw = fixture["gold"].read_bytes()
    gold = challenge._strict_json(gold_raw, label="gold")  # noqa: SLF001
    gold_by_case = {str(row["case_id"]): row for row in gold["cases"]}
    cases = challenge._prepare_cases(manifest)  # noqa: SLF001
    import pkm_brain.gmail_temporal_runner as production_runner

    paths = BrainPaths.from_value(str(manifest["brain_home"]))
    authorities = {
        case.case_id: production_runner._build_authority(  # noqa: SLF001
            paths,
            document_id=case.document_id,
            gmail_message_id=case.gmail_message_id,
        )
        for case in cases
    }
    recomputed = challenge._recompute_frontier_gold_coverage(  # noqa: SLF001
        authorities=authorities,
        gold_rows=gold_by_case,
        selected_ids=[case.case_id for case in cases],
    )
    coverage_by_case = {str(row["case_id"]): row for row in recomputed["cases"]}
    rows: list[dict[str, Any]] = []
    for ordinal, case in enumerate(cases):
        members = gold_by_case[case.case_id]["members"]
        coverage = coverage_by_case[case.case_id]
        candidate_count = case.preparation.candidate_count
        if ordinal == 0:
            candidate_count += candidate_count_delta
        positive = bool(members)
        zero_work = not case.preparation.requests
        rows.append(
            {
                "case_id": case.case_id,
                "gold_members": len(members),
                "frontier_covered_gold_members": coverage[
                    "frontier_covered_gold_members"
                ],
                "frontier_missing_gold_members": coverage[
                    "frontier_missing_gold_members"
                ],
                "positive": positive,
                "candidate_count": candidate_count,
                "candidate_bearing": candidate_count > 0,
                "verifier_request_count": len(case.preparation.requests),
                "zero_work": zero_work,
                "positive_zero_work": positive and zero_work,
            }
        )
    aggregates = {
        "cases": len(rows),
        "positive_cases": sum(bool(row["positive"]) for row in rows),
        "negative_cases": sum(not row["positive"] for row in rows),
        "gold_members": sum(row["gold_members"] for row in rows),
        "frontier_covered_gold_members": sum(
            row["frontier_covered_gold_members"] for row in rows
        ),
        "frontier_missing_gold_members": sum(
            row["frontier_missing_gold_members"] for row in rows
        ),
        "positive_zero_work_cases": sum(
            bool(row["positive_zero_work"]) for row in rows
        ),
        "candidate_bearing_positive_cases": sum(
            bool(row["positive"] and row["candidate_bearing"]) for row in rows
        ),
        "candidate_bearing_negative_cases": sum(
            bool(not row["positive"] and row["candidate_bearing"]) for row in rows
        ),
    }
    value = challenge._signed(  # noqa: SLF001
        {
            "version": challenge.FRONTIER_DIAGNOSTICS_VERSION,
            "challenge_id": manifest["challenge_id"],
            "challenge_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "gold_sha256": hashlib.sha256(gold_raw).hexdigest(),
            "fixture_sha256": fixture_sha256
            or hashlib.sha256(b"public test fixture").hexdigest(),
            "aggregates": aggregates,
            "cases": rows,
            "public_synthetic": True,
            "contains_private_gmail": False,
            "release_eligible": False,
        },
        key=fixture["key"].read_bytes(),
        domain=challenge.FRONTIER_DIAGNOSTICS_DOMAIN,
        signature_field="frontier_diagnostics_hmac_sha256",
    )
    path = fixture["manifest"].parent / challenge.FRONTIER_DIAGNOSTICS_FILENAME
    path.write_bytes(challenge._canonical_json(value) + b"\n")  # noqa: SLF001
    path.chmod(0o600)
    return path


def _write_gold_audit_root(
    fixture: Mapping[str, Path],
    *,
    summary_overrides: Mapping[str, Any] | None = None,
    generator_version: str = challenge.GOLD_AUDIT_FIXTURE_GENERATOR_VERSION,
    generator_sha256: str = challenge.GOLD_AUDIT_FIXTURE_GENERATOR_SHA256,
) -> tuple[Path, str]:
    manifest = json.loads(fixture["manifest"].read_text(encoding="utf-8"))
    gold = json.loads(fixture["gold"].read_text(encoding="utf-8"))
    fixture_cases = []
    for row in gold["cases"]:
        case_id = str(row["case_id"])
        fixture_cases.append(
            {
                "case_id": case_id,
                "sender": (
                    "offers@public.example.test"
                    if case_id == "advertising"
                    else "colleague@public.example.test"
                ),
                "subject": f"Public audit source {case_id}",
                "body": (
                    f"This is the complete public synthetic source for {case_id}."
                ),
                "label_ids": ["CATEGORY_PERSONAL"],
                "members": [dict(member) for member in row["members"]],
                "forbidden": [dict(binding) for binding in row["forbidden"]],
                "complete_group_required": bool(row["complete_group_required"]),
            }
        )
    fixture_value = {
        "version": challenge.GOLD_AUDIT_FIXTURE_VERSION,
        "challenge_id": manifest["challenge_id"],
        "created_at": manifest["created_at"],
        "message_internal_at": INTERNAL_AT,
        "account_email": ACCOUNT,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "cases": fixture_cases,
    }
    fixture_raw = challenge._canonical_json(fixture_value) + b"\n"  # noqa: SLF001
    fixture_sha256 = hashlib.sha256(fixture_raw).hexdigest()
    request = {
        "version": challenge.GOLD_AUDIT_REQUEST_VERSION,
        "phase": "prediction_blind_public_gold_audit",
        "contract": gold_audit._CONTRACT,  # noqa: SLF001
        "challenge_id": fixture_value["challenge_id"],
        "fixture_created_at": fixture_value["created_at"],
        "message_internal_at": fixture_value["message_internal_at"],
        "account_email": fixture_value["account_email"],
        "public_synthetic": True,
        "contains_private_gmail": False,
        "pipeline_predictions_present": False,
        "cases": [
            challenge._gold_audit_expected_request_case(row)  # noqa: SLF001
            for row in fixture_cases
        ],
    }
    request_raw = challenge._canonical_json(request) + b"\n"  # noqa: SLF001
    valid = {
        "disposition": "valid",
        "issue_codes": ["none"],
        "rationale": "The proposed public synthetic label is valid.",
    }
    response_value = {
        "version": challenge.GOLD_AUDIT_RESPONSE_VERSION,
        "cases": [
            {
                "case_id": row["case_id"],
                **valid,
                "members": [
                    {"member_ordinal": ordinal, **valid}
                    for ordinal, _ in enumerate(
                        row["proposed_gold"]["members"],
                        start=1,
                    )
                ],
                "forbidden_bindings": [
                    {"forbidden_ordinal": ordinal, **valid}
                    for ordinal, _ in enumerate(
                        row["proposed_gold"]["forbidden"],
                        start=1,
                    )
                ],
                "group_flag": dict(valid),
            }
            for row in request["cases"]
        ],
    }
    response = challenge._gold_audit_response(  # noqa: SLF001
        response_value,
        request=request,
    )
    response_raw = challenge._canonical_json(response) + b"\n"  # noqa: SLF001
    key = fixture["key"].read_bytes()
    plan = challenge._signed(  # noqa: SLF001
        {
            "version": challenge.GOLD_AUDIT_PLAN_VERSION,
            "created_at": "2020-01-01T00:00:00+00:00",
            "scope": challenge.GOLD_AUDIT_SCOPE,
            "fixture_version": challenge.GOLD_AUDIT_FIXTURE_VERSION,
            "fixture_variant": 2,
            "fixture_sha256": fixture_sha256,
            "fixture_generator_version": generator_version,
            "fixture_generator_sha256": generator_sha256,
            "fixture_generator_exact_bytes_verified": True,
            "case_count": len(fixture_cases),
            "batch_count": 1,
            "request_sha256": [hashlib.sha256(request_raw).hexdigest()],
            "provider": challenge.GOLD_AUDIT_PROVIDER,
            "model": challenge.GOLD_AUDIT_MODEL,
            "reasoning_effort": challenge.GOLD_AUDIT_REASONING_EFFORT,
            "public_synthetic": True,
            "contains_private_gmail": False,
            "pipeline_predictions_present": False,
            "prediction_artifacts_read": False,
            "diagnostic_only": True,
            "release_eligible": False,
        },
        key=key,
        domain=challenge.GOLD_AUDIT_PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
    )
    plan_raw = challenge._canonical_json(plan) + b"\n"  # noqa: SLF001
    receipt = challenge._signed(  # noqa: SLF001
        {
            "version": challenge.GOLD_AUDIT_RECEIPT_VERSION,
            "unit_ordinal": 1,
            "started_at": "2020-01-01T00:00:01+00:00",
            "completed_at": "2020-01-01T00:00:02+00:00",
            "provider": challenge.GOLD_AUDIT_PROVIDER,
            "model": challenge.GOLD_AUDIT_MODEL,
            "reasoning_effort": challenge.GOLD_AUDIT_REASONING_EFFORT,
            "request_sha256": hashlib.sha256(request_raw).hexdigest(),
            "response_sha256": hashlib.sha256(response_raw).hexdigest(),
            "case_count": len(request["cases"]),
            "public_synthetic": True,
            "contains_private_gmail": False,
            "pipeline_predictions_present": False,
            "restricted_execution": True,
            "ephemeral_execution": True,
            "local_model_used": False,
            "test_invoker_used": False,
        },
        key=key,
        domain=challenge.GOLD_AUDIT_RECEIPT_DOMAIN,
        signature_field="receipt_hmac_sha256",
    )
    receipt_raw = challenge._canonical_json(receipt) + b"\n"  # noqa: SLF001
    aggregates = challenge._gold_audit_aggregates(response["cases"])  # noqa: SLF001
    detail = challenge._signed(  # noqa: SLF001
        {
            "version": challenge.GOLD_AUDIT_DETAIL_VERSION,
            "status": "complete",
            "created_at": "2020-01-01T00:00:03+00:00",
            "scope": challenge.GOLD_AUDIT_SCOPE,
            "fixture_sha256": fixture_sha256,
            "fixture_variant": 2,
            "fixture_generator_version": generator_version,
            "fixture_generator_sha256": generator_sha256,
            "fixture_generator_exact_bytes_verified": True,
            "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
            "provider": challenge.GOLD_AUDIT_PROVIDER,
            "model": challenge.GOLD_AUDIT_MODEL,
            "reasoning_effort": challenge.GOLD_AUDIT_REASONING_EFFORT,
            "public_synthetic": True,
            "contains_private_gmail": False,
            "pipeline_predictions_present": False,
            "prediction_artifacts_read": False,
            "diagnostic_only": True,
            "release_eligible": False,
            "calls": [
                {
                    "unit_ordinal": 1,
                    "request_sha256": hashlib.sha256(request_raw).hexdigest(),
                    "response_sha256": hashlib.sha256(response_raw).hexdigest(),
                    "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
                    "case_count": len(request["cases"]),
                }
            ],
            "cases": response["cases"],
            "aggregates": aggregates,
        },
        key=key,
        domain=challenge.GOLD_AUDIT_DETAIL_DOMAIN,
        signature_field="detail_hmac_sha256",
    )
    detail_raw = challenge._canonical_json(detail) + b"\n"  # noqa: SLF001
    summary_value: dict[str, Any] = {
        "version": challenge.GOLD_AUDIT_SUMMARY_VERSION,
        "status": "complete",
        "created_at": "2020-01-01T00:00:04+00:00",
        "scope": challenge.GOLD_AUDIT_SCOPE,
        "fixture_sha256": fixture_sha256,
        "fixture_variant": 2,
        "fixture_generator_version": generator_version,
        "fixture_generator_sha256": generator_sha256,
        "fixture_generator_exact_bytes_verified": True,
        "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "detail_sha256": hashlib.sha256(detail_raw).hexdigest(),
        "batch_count": 1,
        "provider": challenge.GOLD_AUDIT_PROVIDER,
        "model": challenge.GOLD_AUDIT_MODEL,
        "reasoning_effort": challenge.GOLD_AUDIT_REASONING_EFFORT,
        "external_calls": 1,
        "restricted_execution": True,
        "ephemeral_execution": True,
        "local_model_used": False,
        "test_invoker_used": False,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "pipeline_predictions_present": False,
        "prediction_artifacts_read": False,
        "private_content_printed": False,
        "diagnostic_only": True,
        "release_eligible": False,
        **aggregates,
    }
    summary_value.update(summary_overrides or {})
    summary = challenge._signed(  # noqa: SLF001
        summary_value,
        key=key,
        domain=challenge.GOLD_AUDIT_SUMMARY_DOMAIN,
        signature_field="summary_hmac_sha256",
    )
    root = fixture["manifest"].parent / "gold-audit"
    challenge._fresh_private_directory(root)  # noqa: SLF001
    calls_root = root / "calls" / "001"
    challenge._private_directory(calls_root, create=True)  # noqa: SLF001
    for path, raw in (
        (root / "fixture.json", fixture_raw),
        (root / "audit-plan.json", plan_raw),
        (root / "audit-detail.json", detail_raw),
        (
            root / "audit-summary.json",
            challenge._canonical_json(summary) + b"\n",  # noqa: SLF001
        ),
        (calls_root / "request.json", request_raw),
        (calls_root / "response.json", response_raw),
        (calls_root / "receipt.json", receipt_raw),
    ):
        challenge._write_private_new(path, raw)  # noqa: SLF001
    return root, fixture_sha256


def _row(case_id: str, ordinal: int, filler_size: int) -> Any:
    fingerprint = f"gtrq_{ordinal:064x}"
    candidate_id = f"gtvc_{ordinal:032x}"
    return challenge._RequestRow(  # noqa: SLF001
        case_id=case_id,
        request_fingerprint=fingerprint,
        payload={
            "request_fingerprint": fingerprint,
            "page": {
                "clusters": [{"candidate_ids": [candidate_id]}],
                "padding": "x" * filler_size,
            },
        },
        batch_fingerprint=f"batch-{ordinal}",
        frontier_fingerprint=f"frontier-{ordinal}",
        page_plan_fingerprint=f"plan-{ordinal}",
        page_fingerprint=f"page-{ordinal}",
        candidate_ids=(candidate_id,),
    )


def _row_with_candidate_ids(
    case_id: str,
    ordinal: int,
    candidate_ids: tuple[str, ...],
) -> Any:
    row = _row(case_id, ordinal, 100)
    payload = dict(row.payload)
    page = dict(payload["page"])
    page["clusters"] = [{"candidate_ids": list(candidate_ids)}]
    payload["page"] = page
    return challenge._RequestRow(  # noqa: SLF001
        case_id=row.case_id,
        request_fingerprint=row.request_fingerprint,
        payload=payload,
        batch_fingerprint=row.batch_fingerprint,
        frontier_fingerprint=row.frontier_fingerprint,
        page_plan_fingerprint=row.page_plan_fingerprint,
        page_fingerprint=row.page_fingerprint,
        candidate_ids=candidate_ids,
    )


def _unit_response(unit: Any) -> dict[str, Any]:
    return {
        "version": challenge.external.VERIFIER_RESPONSE_VERSION,
        "pages": [
            {
                "request_fingerprint": row.request_fingerprint,
                "verdicts": [
                    {"candidate_id": candidate_id, "verdict": "unsupported"}
                    for candidate_id in row.candidate_ids
                ],
            }
            for row in unit.rows
        ],
    }


def test_bounded_units_use_shared_item_and_byte_ceiling() -> None:
    rows = tuple(_row(f"case-{index}", index, 14_000) for index in range(1, 6))

    units = challenge.bounded_public_call_units(rows)

    assert len(units) > 1
    assert [row.request_fingerprint for unit in units for row in unit.rows] == [
        row.request_fingerprint for row in rows
    ]
    for unit in units:
        assert len(unit.rows) <= challenge.external.MAX_VERIFIER_BATCH_SIZE
        assert (
            len(challenge._canonical_json(unit.request) + b"\n")  # noqa: SLF001
            <= challenge.external.MAX_VERIFIER_REQUEST_BYTES
        )


def test_bounded_units_pack_multi_page_cases_atomically() -> None:
    rows = (
        _row("case-one", 1, 100),
        _row("case-one", 2, 100),
        _row("case-two", 3, 100),
        _row("case-two", 4, 100),
        _row("case-two", 5, 100),
    )

    units = challenge.bounded_public_call_units(rows)

    assert [[row.case_id for row in unit.rows] for unit in units] == [
        ["case-one", "case-one"],
        ["case-two", "case-two", "case-two"],
    ]


def test_verifier_schema_pins_exact_call_identifiers_and_page_count() -> None:
    unit = challenge.bounded_public_call_units(
        (_row("case-one", 1, 100), _row("case-one", 2, 100))
    )[0]

    schema = challenge._verifier_response_schema(unit)  # noqa: SLF001
    pages = schema["properties"]["pages"]
    page_properties = pages["items"]["properties"]

    assert pages["minItems"] == pages["maxItems"] == 2
    assert page_properties["request_fingerprint"]["enum"] == [
        row.request_fingerprint for row in unit.rows
    ]
    assert set(
        page_properties["verdicts"]["items"]["properties"]["candidate_id"]["enum"]
    ) == {candidate_id for row in unit.rows for candidate_id in row.candidate_ids}


def test_verifier_response_uses_candidate_ids_and_canonicalizes_page_order() -> None:
    unit = challenge.bounded_public_call_units(
        (_row("case-one", 1, 100), _row("case-one", 2, 100))
    )[0]
    response = _unit_response(unit)
    response["pages"] = list(reversed(response["pages"]))

    validated = challenge._validate_response(unit, response)  # noqa: SLF001

    assert [page["request_fingerprint"] for page in validated["case-one"]] == [
        row.request_fingerprint for row in unit.rows
    ]
    assert [
        verdict["candidate_id"]
        for page in validated["case-one"]
        for verdict in page["verdicts"]
    ] == [candidate_id for row in unit.rows for candidate_id in row.candidate_ids]


@pytest.mark.parametrize("mutation", ("unknown", "duplicate"))
def test_verifier_response_rejects_non_bijective_page_authority(mutation: str) -> None:
    unit = challenge.bounded_public_call_units(
        (_row("case-one", 1, 100), _row("case-one", 2, 100))
    )[0]
    response = _unit_response(unit)
    if mutation == "duplicate":
        response["pages"][1]["request_fingerprint"] = response["pages"][0][
            "request_fingerprint"
        ]
    else:
        response["pages"][1]["request_fingerprint"] = (
            "gtrq_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        )

    with pytest.raises(challenge.PublicChallengeError, match="page authority"):
        challenge._validate_response(unit, response)  # noqa: SLF001


def test_verifier_response_rejects_nonempty_candidate_omission() -> None:
    candidate_ids = (
        "gtvc_00000000000000000000000000000001",
        "gtvc_00000000000000000000000000000002",
    )
    unit = challenge.bounded_public_call_units(
        (_row_with_candidate_ids("case-one", 1, candidate_ids),)
    )[0]
    response = _unit_response(unit)
    response["pages"][0]["verdicts"].pop()

    with pytest.raises(challenge.PublicChallengeError, match="candidate coverage"):
        challenge._validate_response(unit, response)  # noqa: SLF001


@pytest.mark.parametrize("mutation", ("duplicate", "unknown"))
def test_verifier_response_rejects_invalid_candidate_coverage(mutation: str) -> None:
    unit = challenge.bounded_public_call_units(
        (_row("case-one", 1, 100), _row("case-one", 2, 100))
    )[0]
    response = _unit_response(unit)
    response["pages"][1]["verdicts"][0]["candidate_id"] = (
        unit.rows[0].candidate_ids[0]
        if mutation == "duplicate"
        else "gtvc_ffffffffffffffffffffffffffffffff"
    )

    with pytest.raises(challenge.PublicChallengeError, match="candidate coverage"):
        challenge._validate_response(unit, response)  # noqa: SLF001


def test_bounded_units_reject_duplicate_candidate_authority_before_schema() -> None:
    first = _row("case-one", 1, 100)
    second = _row_with_candidate_ids("case-two", 2, first.candidate_ids)

    with pytest.raises(challenge.PublicChallengeError, match="candidate authority"):
        challenge.bounded_public_call_units((first, second))


def test_bounded_units_reject_non_string_candidate_authority_before_schema() -> None:
    malformed = _row_with_candidate_ids("case-one", 1, (None,))  # type: ignore[arg-type]

    with pytest.raises(challenge.PublicChallengeError, match="candidate authority"):
        challenge.bounded_public_call_units((malformed,))


@pytest.mark.parametrize("variant", ["missing", "tampered"])
def test_public_root_authority_fails_before_invoker_creation(
    tmp_path: Path, variant: str
) -> None:
    fixture = _fixture(tmp_path)
    marker = fixture["marker"]
    if variant == "missing":
        marker.unlink()
    else:
        value = json.loads(marker.read_text(encoding="utf-8"))
        value["gold_sha256"] = "0" * 64
        marker.write_bytes(challenge._canonical_json(value) + b"\n")  # noqa: SLF001
        marker.chmod(0o600)
    fake = FakeCodex()
    output = tmp_path / "must-not-start"

    with pytest.raises(challenge.PublicChallengeError, match="public root authority"):
        challenge.run_public_challenge(
            fixture["manifest"],
            fixture["key"],
            output,
            invoke=fake,
            test_only_allow_injected_invoker=True,
        )

    assert fake.calls == []
    assert not output.exists()


def test_run_never_reads_gold_or_frontier_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    frontier_diagnostics = _write_frontier_diagnostics(fixture)
    output = tmp_path / "run-read-boundary"
    reads: list[Path] = []
    original_private_file = challenge._private_file  # noqa: SLF001

    def tracked_private_file(path: Path) -> bytes:
        reads.append(Path(path))
        return original_private_file(path)

    monkeypatch.setattr(challenge, "_private_file", tracked_private_file)

    result = challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )

    assert result["gold_accessed"] is False
    assert fixture["gold"] not in reads
    assert frontier_diagnostics not in reads


def test_failed_external_call_retains_receipt_without_partial_publication(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "failed-run"
    fake = FakeCodex(fail_at=1)

    with pytest.raises(challenge.PublicChallengeError, match="external verifier"):
        challenge.run_public_challenge(
            fixture["manifest"],
            fixture["key"],
            output,
            invoke=fake,
            test_only_allow_injected_invoker=True,
        )

    assert len(fake.calls) == 1
    assert (output / "plan.json").is_file()
    assert (output / "calls/run-1/unit-001/request.json").is_file()
    assert (output / "calls/run-1/unit-001/started.json").is_file()
    receipt_path = output / "calls/run-1/unit-001/receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["response_sha256"] is None
    assert receipt["diagnostic_sha256"]
    assert not (output / "calls/run-1/unit-001/response.json").exists()
    assert not (output / "components").exists()
    assert not (output / "prediction-seal.json").exists()
    assert not (output / "results.json").exists()
    with connection(BrainPaths.from_value(fixture["home"]).sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_executions"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM gmail_temporal_review_runs").fetchone()[
                0
            ]
            == 0
        )


def test_success_seals_all_three_runs_before_results_and_keeps_files_private(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "successful-run"
    fake = FakeCodex()

    result = challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=fake,
        test_only_allow_injected_invoker=True,
    )

    assert result["status"] == "complete"
    assert result["invocations"] == 3
    assert result["external_calls"] == 0
    assert result["test_invoker_used"] is True
    assert result["gold_accessed"] is False
    assert len(fake.calls) == 3
    seal = json.loads((output / "prediction-seal.json").read_text(encoding="utf-8"))
    stored = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert seal["invocation_count"] == 3
    assert seal["external_call_count"] == 0
    assert seal["restricted_execution"] is False
    assert seal["test_invoker_used"] is True
    assert seal["gold_accessed"] is False
    assert stored["gold_accessed"] is False
    assert datetime_from_iso(seal["sealed_at"]) <= datetime_from_iso(
        stored["completed_at"]
    )
    for path in output.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected

    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="blind_first_use",
    )
    assert score["gold_opened_after_this_prediction_seal"] is True
    assert score["operator_asserted_evaluation_mode"] == "blind_first_use"
    assert score["first_use_blindness_claimed"] is True
    assert score["selected_negative_cases"] == 0
    assert score["canonical_subject_members"] == 1
    assert score["canonical_subject_members_recovered"] == 0
    assert score["canonical_subject_recall"] == 0.0
    assert score["cases"][0]["canonical_subject_members"] == 1
    assert score["cases"][0]["canonical_subject_members_recovered"] == 0
    assert score["gates"]["all_canonical_subjects_recovered"] is False
    assert score["smoke_gate_passed"] is False
    assert score["test_invoker_used"] is True
    assert score["frontier_diagnostics"] is None
    assert (output / "score.json").is_file()
    signed_score = json.loads((output / "score.json").read_text(encoding="utf-8"))
    assert signed_score["version"] == challenge.SCORE_VERSION
    assert signed_score["canonical_subject_members"] == 1
    assert signed_score["canonical_subject_members_recovered"] == 0
    assert signed_score["canonical_subject_recall"] == 0.0
    assert signed_score["cases"][0]["canonical_subject_members"] == 1
    assert signed_score["cases"][0]["canonical_subject_members_recovered"] == 0
    assert signed_score["gates"]["all_canonical_subjects_recovered"] is False
    assert signed_score["frontier_diagnostics"] is None


def test_score_binds_authenticated_frontier_diagnostics(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    frontier_diagnostics = _write_frontier_diagnostics(fixture)
    diagnostics_raw = frontier_diagnostics.read_bytes()
    output = tmp_path / "frontier-diagnostics-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
        frontier_diagnostics_path=frontier_diagnostics,
    )
    signed_score = json.loads((output / "score.json").read_text(encoding="utf-8"))

    expected_sha256 = hashlib.sha256(diagnostics_raw).hexdigest()
    for value in (score, signed_score):
        binding = value["frontier_diagnostics"]
        assert binding["version"] == challenge.FRONTIER_DIAGNOSTICS_VERSION
        assert binding["sha256"] == expected_sha256
        assert (
            binding["fixture_sha256"]
            == hashlib.sha256(b"public test fixture").hexdigest()
        )
        assert binding["aggregates"]["cases"] == 2
        assert binding["aggregates"]["gold_members"] == 1
        assert binding["aggregates"]["frontier_covered_gold_members"] == 0
        assert binding["aggregates"]["frontier_missing_gold_members"] == 1
        assert binding["aggregates"]["candidate_bearing_positive_cases"] == 1


def test_score_binds_zero_correction_sol_gold_audit_to_exact_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(
        tmp_path,
        gold_subject="interview",
        canonical_subject_required=False,
    )
    gold_audit_root, fixture_sha256 = _write_gold_audit_root(fixture)
    monkeypatch.setattr(
        challenge,
        "GOLD_AUDIT_APPROVED_FIXTURE_SHA256",
        {2: fixture_sha256},
    )
    frontier_diagnostics = _write_frontier_diagnostics(
        fixture,
        fixture_sha256=fixture_sha256,
    )
    audit_raw = (gold_audit_root / "audit-summary.json").read_bytes()
    output = tmp_path / "audited-gold-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
        frontier_diagnostics_path=frontier_diagnostics,
        gold_audit_root=gold_audit_root,
    )
    signed_score = json.loads((output / "score.json").read_text(encoding="utf-8"))

    for value in (score, signed_score):
        binding = value["gold_audit"]
        assert binding["version"] == challenge.GOLD_AUDIT_SUMMARY_VERSION
        assert binding["sha256"] == hashlib.sha256(audit_raw).hexdigest()
        assert (
            binding["fixture_sha256"] == value["frontier_diagnostics"]["fixture_sha256"]
        )
        assert binding["model"] == "gpt-5.6-sol"
        assert binding["reasoning_effort"] == "medium"
        assert binding["complete_evidence_chain"] is True
        assert binding["fixture_generator_version"] == (
            challenge.GOLD_AUDIT_FIXTURE_GENERATOR_VERSION
        )
        assert binding["fixture_generator_sha256"] == (
            challenge.GOLD_AUDIT_FIXTURE_GENERATOR_SHA256
        )
        assert (
            binding["plan_sha256"]
            == hashlib.sha256(
                (gold_audit_root / "audit-plan.json").read_bytes()
            ).hexdigest()
        )
        assert (
            binding["detail_sha256"]
            == hashlib.sha256(
                (gold_audit_root / "audit-detail.json").read_bytes()
            ).hexdigest()
        )
        assert binding["zero_corrections"] is True
        assert value["personal_target_gate_available"] is True
        assert (
            value["personal_target_gates"][
                "authenticated_zero_correction_sol_gold_audit"
            ]
            is True
        )
        assert value["frontier_member_recall"] == 1.0
        assert value["authenticated_positive_zero_work_cases"] == 0
        assert (
            value["personal_target_gates"]["frontier_member_recall_at_least_0_95"]
            is True
        )
        assert (
            value["personal_target_gates"][
                "zero_authenticated_positive_zero_work_cases"
            ]
            is True
        )
        assert value["personal_target_gates"][
            "canonical_title_recall_at_least_0_90"
        ] is (value["canonical_title_recall"] >= 0.90)
        assert value["personal_target_gates"][
            "critical_lifecycle_effective_member_recall_at_least_0_95"
        ] is (value["critical_lifecycle_metrics"]["effective_member_recall"] >= 0.95)
        # This tiny fixture has no candidate-bearing negative denominator.
        assert (
            value["personal_target_gates"][
                "candidate_bearing_negative_rejection_at_least_0_80"
            ]
            is False
        )
        # The injected prediction verifier is deliberately not external evidence.
        assert value["personal_target_gate_passed"] is False
        assert value["personal_target_gates"]["restricted_external_execution"] is False


def test_gold_audit_requires_authenticated_frontier_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gold_audit_root, _ = _write_gold_audit_root(fixture)
    output = tmp_path / "audit-without-frontier-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )

    with pytest.raises(challenge.PublicChallengeError, match="requires authenticated"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            gold_audit_root=gold_audit_root,
        )

    assert not (output / "score.json").exists()


@pytest.mark.parametrize(
    "overrides",
    (
        {"restricted_execution": False},
        {"test_invoker_used": True, "local_model_used": True},
        {"correction_case_count": 1, "valid_case_count": 1},
        {"fixture_sha256": "0" * 64},
        {"created_at": "2999-01-01T00:00:00+00:00"},
    ),
)
def test_score_rejects_non_authoritative_gold_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: Mapping[str, Any],
) -> None:
    fixture = _fixture(tmp_path)
    gold_audit_root, fixture_sha256 = _write_gold_audit_root(
        fixture,
        summary_overrides=overrides,
    )
    monkeypatch.setattr(
        challenge,
        "GOLD_AUDIT_APPROVED_FIXTURE_SHA256",
        {2: fixture_sha256},
    )
    frontier_diagnostics = _write_frontier_diagnostics(
        fixture,
        fixture_sha256=fixture_sha256,
    )
    output = tmp_path / "invalid-audit-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )

    with pytest.raises(challenge.PublicChallengeError, match="gold audit"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
            gold_audit_root=gold_audit_root,
        )

    assert not (output / "score.json").exists()


@pytest.mark.parametrize(
    "relative_path",
    (
        "fixture.json",
        "audit-detail.json",
        "calls/001/request.json",
        "calls/001/response.json",
        "calls/001/receipt.json",
    ),
)
def test_score_rejects_missing_gold_audit_chain_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    fixture = _fixture(tmp_path)
    gold_audit_root, fixture_sha256 = _write_gold_audit_root(fixture)
    monkeypatch.setattr(
        challenge,
        "GOLD_AUDIT_APPROVED_FIXTURE_SHA256",
        {2: fixture_sha256},
    )
    frontier_diagnostics = _write_frontier_diagnostics(
        fixture,
        fixture_sha256=fixture_sha256,
    )
    output = tmp_path / "missing-audit-evidence-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )
    (gold_audit_root / relative_path).unlink()

    with pytest.raises(challenge.PublicChallengeError, match="gold audit"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
            gold_audit_root=gold_audit_root,
        )

    assert not (output / "score.json").exists()


def test_score_rejects_tampered_gold_audit_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    gold_audit_root, fixture_sha256 = _write_gold_audit_root(fixture)
    monkeypatch.setattr(
        challenge,
        "GOLD_AUDIT_APPROVED_FIXTURE_SHA256",
        {2: fixture_sha256},
    )
    frontier_diagnostics = _write_frontier_diagnostics(
        fixture,
        fixture_sha256=fixture_sha256,
    )
    output = tmp_path / "tampered-audit-response-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )
    response_path = gold_audit_root / "calls/001/response.json"
    response = json.loads(response_path.read_text(encoding="utf-8"))
    response["cases"][0]["rationale"] = "Tampered after the signed receipt."
    response_path.write_bytes(challenge._canonical_json(response) + b"\n")  # noqa: SLF001
    response_path.chmod(0o600)

    with pytest.raises(challenge.PublicChallengeError, match="receipt authority"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
            gold_audit_root=gold_audit_root,
        )

    assert not (output / "score.json").exists()


def test_score_rejects_unapproved_gold_audit_fixture_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    unapproved_sha256 = hashlib.sha256(b"unapproved generator").hexdigest()
    gold_audit_root, fixture_sha256 = _write_gold_audit_root(
        fixture,
        generator_version="unapproved-generator-v1",
        generator_sha256=unapproved_sha256,
    )
    monkeypatch.setattr(
        challenge,
        "GOLD_AUDIT_APPROVED_FIXTURE_SHA256",
        {2: fixture_sha256},
    )
    frontier_diagnostics = _write_frontier_diagnostics(
        fixture,
        fixture_sha256=fixture_sha256,
    )
    output = tmp_path / "unapproved-audit-generator-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )

    with pytest.raises(challenge.PublicChallengeError, match="plan authority"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
            gold_audit_root=gold_audit_root,
        )

    assert not (output / "score.json").exists()


def test_score_rejects_signed_false_frontier_coverage_claim(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    frontier_diagnostics = _write_frontier_diagnostics(fixture)
    value = json.loads(frontier_diagnostics.read_text(encoding="utf-8"))
    value.pop("frontier_diagnostics_hmac_sha256")
    value["cases"][0]["frontier_covered_gold_members"] = 1
    value["cases"][0]["frontier_missing_gold_members"] = 0
    value["aggregates"]["frontier_covered_gold_members"] = 1
    value["aggregates"]["frontier_missing_gold_members"] = 0
    value = challenge._signed(  # noqa: SLF001
        value,
        key=fixture["key"].read_bytes(),
        domain=challenge.FRONTIER_DIAGNOSTICS_DOMAIN,
        signature_field="frontier_diagnostics_hmac_sha256",
    )
    frontier_diagnostics.write_bytes(
        challenge._canonical_json(value) + b"\n"  # noqa: SLF001
    )
    frontier_diagnostics.chmod(0o600)
    output = tmp_path / "false-frontier-coverage-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )

    with pytest.raises(
        challenge.PublicChallengeError,
        match="do not match production candidates",
    ):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
        )

    assert not (output / "score.json").exists()


def test_score_rejects_invalid_or_stale_frontier_diagnostics(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    frontier_diagnostics = _write_frontier_diagnostics(fixture)
    output = tmp_path / "invalid-frontier-diagnostics-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )

    tampered = json.loads(frontier_diagnostics.read_text(encoding="utf-8"))
    tampered["fixture_sha256"] = "0" * 64
    frontier_diagnostics.write_bytes(
        challenge._canonical_json(tampered) + b"\n"  # noqa: SLF001
    )
    frontier_diagnostics.chmod(0o600)
    with pytest.raises(challenge.PublicChallengeError, match="authentication"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
        )

    _write_frontier_diagnostics(fixture, candidate_count_delta=1)
    with pytest.raises(challenge.PublicChallengeError, match="case is stale"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            frontier_diagnostics_path=frontier_diagnostics,
        )

    assert not (output / "score.json").exists()


def test_score_separates_semantic_match_from_supported_calibration(
    tmp_path: Path,
) -> None:
    fixture = _fixture(
        tmp_path,
        expected_verdict="uncertain",
        gold_subject="interview",
        canonical_subject_required=False,
    )
    output = tmp_path / "overconfident-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(verdict="supported"),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    assert score["gold_members"] == 1
    assert score["matched_members"] == 1
    assert score["effective_member_recall"] == 1.0
    assert score["artifacts"] == 2
    assert score["matched_supported_artifacts"] == 0
    assert score["overconfident_artifacts"] == 1
    assert score["supported_overclaim_count"] == 1
    assert score["supported_artifact_precision"] == 0.0
    assert score["review_artifact_precision"] == 0.5
    assert score["review_output_precision"] == 0.5
    assert score["confirmed_members"] == 0
    assert score["positive_cases"] == 1
    assert score["positive_cases_fully_recovered"] == 1
    assert score["positive_case_completeness_recall"] == 1.0
    assert score["canonical_title_recall"] == score["canonical_subject_recall"]
    assert score["confidence_interval_method"] == "wilson_score_95_two_sided"
    intervals = score["metric_intervals_95"]
    assert set(intervals) == {
        "effective_member_recall",
        "confirmed_member_recall",
        "supported_artifact_precision",
        "review_artifact_precision",
        "canonical_subject_recall",
        "canonical_title_recall",
        "complete_group_recall",
        "complete_unit_recall",
        "exact_unit_recall",
        "critical_lifecycle_effective_member_recall",
        "critical_temporal_effective_member_recall",
        "deadline_effective_member_recall",
        "reschedule_unit_recall",
        "positive_case_completeness_recall",
    }
    assert intervals["effective_member_recall"]["numerator"] == 1
    assert intervals["effective_member_recall"]["denominator"] == 1
    assert intervals["review_artifact_precision"]["numerator"] == 1
    assert intervals["review_artifact_precision"]["denominator"] == 2
    assert intervals["canonical_title_recall"]["interval_defined"] is False
    assert score["semantic_units"] == 1
    assert score["complete_units"] == 1
    assert score["exact_units"] == 1
    assert score["complete_unit_recall"] == 1.0
    assert score["exact_unit_recall"] == 1.0
    assert score["lifecycle_metrics"]["scheduled"]["gold_members"] == 1
    assert score["lifecycle_metrics"]["scheduled"]["matched_members"] == 1
    assert score["lifecycle_metrics"]["unknown"]["gold_members"] == 0
    assert score["lifecycle_metrics"]["unknown"]["effective_member_recall"] is None
    assert score["critical_lifecycle_metrics"]["gold_members"] == 1
    assert score["critical_lifecycle_metrics"]["matched_members"] == 1
    assert score["critical_lifecycle_metrics"]["effective_member_recall"] == 1.0
    assert score["lifecycle_reporting_complete"] is True
    assert score["gates"]["zero_supported_overclaims"] is False
    assert "zero_supported_overclaims" not in score["personal_target_gates"]
    assert "no_selected_negative_cases" not in score["personal_target_gates"]
    assert score["gold_audit"] is None
    assert score["personal_target_gate_available"] is False
    assert score["personal_target_gate_passed"] is False
    assert (
        score["personal_target_gates"]["authenticated_zero_correction_sol_gold_audit"]
        is False
    )
    for metric in intervals.values():
        if metric["interval_defined"]:
            assert 0.0 <= metric["wilson_95_lower"]
            assert metric["wilson_95_lower"] <= metric["estimate"]
            assert metric["estimate"] <= metric["wilson_95_upper"]
            assert metric["wilson_95_upper"] <= 1.0


@pytest.mark.parametrize(
    ("verdict", "selected", "rejected", "rate"),
    [
        ("supported", 1, 0, 0.0),
        ("unsupported", 0, 1, 1.0),
    ],
)
def test_candidate_bearing_negative_rejection_uses_authenticated_preparation(
    tmp_path: Path,
    verdict: str,
    selected: int,
    rejected: int,
    rate: float,
) -> None:
    fixture = _fixture(tmp_path, negative_candidate_bearing=True)
    output = tmp_path / f"candidate-negative-{verdict}"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(verdict=verdict),
        test_only_allow_injected_invoker=True,
    )
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert plan["candidate_case_count"] == 2
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    assert score["negative_cases"] == 1
    assert score["candidate_bearing_negative_cases"] == 1
    assert score["selected_candidate_bearing_negative_cases"] == selected
    assert score["rejected_candidate_bearing_negative_cases"] == rejected
    assert score["candidate_bearing_negative_rejection_rate"] == rate
    assert score["personal_target_gates"][
        "candidate_bearing_negative_rejection_at_least_0_80"
    ] is (rate >= 0.80)
    assert score["candidate_bearing_negative_case_basis"] == (
        "authenticated_prediction_preparation_candidate_count"
    )
    negative = next(row for row in score["cases"] if row["gold_members"] == 0)
    assert negative["candidate_bearing"] is True
    assert negative["candidate_bearing_negative_selected"] is bool(selected)


@pytest.mark.parametrize(
    ("complete_group_required", "expected_group_components"),
    [(False, 0), (True, 1)],
)
def test_distinct_reschedule_title_and_explicit_complete_group_denominator(
    tmp_path: Path,
    complete_group_required: bool,
    expected_group_components: int,
) -> None:
    fixture = _fixture(
        tmp_path,
        reschedule=True,
        complete_group_required=complete_group_required,
    )
    output = tmp_path / f"reschedule-group-{complete_group_required}"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(verdict="supported"),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    assert score["canonical_subject_members"] == 2
    assert score["canonical_subject_members_recovered"] == 2
    assert score["canonical_subject_recall"] == 1.0
    assert score["canonical_titles"] == 1
    assert score["canonical_titles_recovered"] == 1
    assert score["canonical_title_recall"] == 1.0
    title_interval = score["metric_intervals_95"]["canonical_title_recall"]
    assert title_interval["numerator"] == 1
    assert title_interval["denominator"] == 1
    assert score["complete_group_components"] == expected_group_components
    assert score["complete_group_components_recovered"] == (expected_group_components)
    positive = next(row for row in score["cases"] if row["gold_members"])
    assert positive["canonical_subject_members"] == 2
    assert positive["canonical_titles"] == 1
    assert positive["canonical_titles_recovered"] == 1
    assert positive["complete_group_required"] is complete_group_required
    assert positive["complete_group_components"] == expected_group_components
    assert score["semantic_units"] == 1
    assert score["complete_units"] == 1
    assert score["exact_units"] == 1
    assert score["complete_unit_recall"] == 1.0
    assert score["exact_unit_recall"] == 1.0
    assert positive["semantic_units"] == 1
    assert positive["complete_units"] == 1
    assert positive["exact_units"] == 1
    assert score["lifecycle_metrics"]["rescheduled_old"]["gold_members"] == 1
    assert score["lifecycle_metrics"]["rescheduled_replacement"]["gold_members"] == 1
    stability = score["three_run_stability"]
    assert stability["accepted_parent_clusters"]["minimum_pairwise_jaccard"] == 1.0
    assert stability["accepted_gold_members"]["minimum_pairwise_jaccard"] == 1.0


def test_semantic_unit_exactness_requires_canonical_recovery() -> None:
    members = [
        {
            "subject": "Public Aster interview",
            "relation": "occurrence",
            "lifecycle": "rescheduled_old",
            "value": "2027-09-20",
            "expected_verdict": "supported",
            "canonical_subject_required": True,
        },
        {
            "subject": "Public Aster interview",
            "relation": "occurrence",
            "lifecycle": "rescheduled_replacement",
            "value": "2027-09-22",
            "expected_verdict": "supported",
            "canonical_subject_required": True,
        },
    ]
    outcomes = [
        {"matched": True, "exact": True, "structural_group_id": "group-one"},
        {"matched": True, "exact": False, "structural_group_id": "group-one"},
    ]

    metrics = challenge._semantic_unit_metrics(members, outcomes)  # noqa: SLF001

    assert metrics == {
        "semantic_units": 1,
        "complete_units": 1,
        "exact_units": 0,
    }


def test_three_run_stability_is_not_diluted_by_shared_negative_candidates(
    tmp_path: Path,
) -> None:
    fixture = _fixture(
        tmp_path,
        gold_subject="interview",
        canonical_subject_required=False,
    )
    output = tmp_path / "unstable-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=SequencedFakeCodex(("supported", "unsupported", "unsupported")),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    stability = score["three_run_stability"]
    parent = stability["accepted_parent_clusters"]
    members = stability["accepted_gold_members"]
    assert len(parent["pairwise"]) == 3
    assert len(members["pairwise"]) == 3
    assert parent["minimum_pairwise_jaccard"] == 0.0
    assert members["minimum_pairwise_jaccard"] == 0.0
    assert parent["gate_passed"] is False
    assert members["gate_passed"] is False
    assert (
        score["personal_target_gates"][
            "accepted_parent_cluster_stability_at_least_0_95"
        ]
        is False
    )
    assert (
        score["personal_target_gates"]["accepted_gold_member_stability_at_least_0_95"]
        is False
    )


def test_scorer_upgrade_accepts_authenticated_prior_prediction_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "prior-launcher-run"
    current_hashes = challenge._source_hashes()  # noqa: SLF001
    prior_artifact = tmp_path / "reviewed-prior-launcher.py"
    prior_artifact.write_bytes(b"# exact reviewed prior launcher artifact\n")
    prior_artifact.chmod(0o600)
    prior_sha256 = hashlib.sha256(prior_artifact.read_bytes()).hexdigest()
    prior_hashes = {**current_hashes, "launcher": prior_sha256}

    with monkeypatch.context() as run_patch:
        run_patch.setattr(challenge, "_source_hashes", lambda: prior_hashes)
        challenge.run_public_challenge(
            fixture["manifest"],
            fixture["key"],
            output,
            invoke=FakeCodex(),
            test_only_allow_injected_invoker=True,
        )

    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
        prediction_launcher_artifact=prior_artifact,
    )
    signed_score = json.loads((output / "score.json").read_text(encoding="utf-8"))

    for value in (score, signed_score):
        assert value["prediction_launcher_sha256"] == prior_sha256
        assert value["prediction_launcher_trust_basis"] == (
            "exact_prior_launcher_artifact"
        )
        assert value["prediction_launcher_exact_artifact_verified"] is True
        assert value["scorer_sha256"] == current_hashes["launcher"]


@pytest.mark.parametrize(
    "source_name",
    ("launcher", "production_runner", "shared_external_runner"),
)
def test_scorer_upgrade_rejects_authenticated_unknown_prediction_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_name: str,
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / f"unknown-{source_name}-run"
    simulated_hashes = {
        **challenge._source_hashes(),  # noqa: SLF001
        source_name: "1" * 64,
    }

    with monkeypatch.context() as run_patch:
        run_patch.setattr(challenge, "_source_hashes", lambda: simulated_hashes)
        challenge.run_public_challenge(
            fixture["manifest"],
            fixture["key"],
            output,
            invoke=FakeCodex(),
            test_only_allow_injected_invoker=True,
        )

    with pytest.raises(challenge.PublicChallengeError, match="provenance"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
        )

    assert not (output / "score.json").exists()


def test_scorer_upgrade_rejects_wrong_prior_launcher_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "wrong-prior-launcher-run"
    expected = tmp_path / "expected-prior-launcher.py"
    expected.write_bytes(b"# expected prior launcher\n")
    expected.chmod(0o600)
    wrong = tmp_path / "wrong-prior-launcher.py"
    wrong.write_bytes(b"# tampered prior launcher\n")
    wrong.chmod(0o600)
    simulated_hashes = {
        **challenge._source_hashes(),  # noqa: SLF001
        "launcher": hashlib.sha256(expected.read_bytes()).hexdigest(),
    }

    with monkeypatch.context() as run_patch:
        run_patch.setattr(challenge, "_source_hashes", lambda: simulated_hashes)
        challenge.run_public_challenge(
            fixture["manifest"],
            fixture["key"],
            output,
            invoke=FakeCodex(),
            test_only_allow_injected_invoker=True,
        )

    with pytest.raises(challenge.PublicChallengeError, match="provenance"):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="development_replay",
            prediction_launcher_artifact=wrong,
        )

    assert not (output / "score.json").exists()


def test_all_zero_work_challenge_scores_as_zero_recall_without_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, all_zero_work=True)
    output = tmp_path / "zero-work-run"

    def reject_invoker_construction(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("zero-work run must not construct an external invoker")

    monkeypatch.setattr(
        challenge.external,
        "RestrictedCodexInvoker",
        reject_invoker_construction,
    )

    result = challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
    )

    assert result["candidate_cases"] == 0
    assert result["zero_work_cases"] == 2
    assert result["invocations"] == 0
    assert result["external_calls"] == 0
    assert result["test_invoker_used"] is False
    assert not (output / "calls").exists()
    assert not (output / "components").exists()
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    seal = json.loads((output / "prediction-seal.json").read_text(encoding="utf-8"))
    for evidence in (plan, seal):
        assert evidence["restricted_execution"] is False
        assert evidence["ephemeral_execution"] is False
        assert evidence["local_model_used"] is False
    assert seal["external_call_count"] == 0

    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    assert score["gold_members"] == 1
    assert score["matched_members"] == 0
    assert score["effective_member_recall"] == 0.0
    assert score["confirmed_member_recall"] == 0.0
    assert score["canonical_subject_members"] == 1
    assert score["canonical_subject_members_recovered"] == 0
    assert score["canonical_subject_recall"] == 0.0
    assert score["supported_artifact_precision"] == 0.0
    assert score["review_output_precision"] == 0.0
    assert score["selected_negative_cases"] == 0
    assert score["positive_cases"] == 1
    assert score["positive_cases_fully_recovered"] == 0
    assert score["positive_case_completeness_recall"] == 0.0
    completeness = score["metric_intervals_95"]["positive_case_completeness_recall"]
    assert completeness["numerator"] == 0
    assert completeness["denominator"] == 1
    assert completeness["estimate"] == 0.0
    assert completeness["interval_defined"] is True
    assert score["candidate_bearing_negative_cases"] == 0
    assert score["selected_candidate_bearing_negative_cases"] == 0
    assert score["rejected_candidate_bearing_negative_cases"] == 0
    assert score["candidate_bearing_negative_rejection_rate"] is None
    assert score["smoke_gate_passed"] is False
    assert score["operator_asserted_evaluation_mode"] == "development_replay"
    assert score["first_use_blindness_claimed"] is False


def test_zero_work_does_not_claim_an_unused_injected_invoker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, all_zero_work=True)
    output = tmp_path / "zero-work-with-unused-invoker"
    fake = FakeCodex()

    result = challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=fake,
        test_only_allow_injected_invoker=True,
    )

    assert fake.calls == []
    assert result["invocations"] == 0
    assert result["external_calls"] == 0
    assert result["test_invoker_used"] is False
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    seal = json.loads((output / "prediction-seal.json").read_text(encoding="utf-8"))
    for evidence in (plan, seal):
        assert evidence["provider"] == challenge.PUBLIC_NO_CALL_PROVIDER
        assert evidence["test_invoker_used"] is False
        assert evidence["restricted_execution"] is False
        assert evidence["local_model_used"] is False


def datetime_from_iso(value: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value)


def test_injected_invoker_is_rejected_without_explicit_test_mode(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "unmarked-injection"
    fake = FakeCodex()

    with pytest.raises(challenge.PublicChallengeError, match="explicit test-only"):
        challenge.run_public_challenge(
            fixture["manifest"],
            fixture["key"],
            output,
            invoke=fake,
        )

    assert fake.calls == []
    assert not output.exists()


def test_case_atomic_ceiling_fails_before_any_invocation() -> None:
    rows = tuple(_row("one-dense-case", index, 14_000) for index in range(1, 6))

    with pytest.raises(challenge.PublicChallengeError, match="cannot fit"):
        challenge.bounded_public_call_units(rows)


@pytest.mark.parametrize(
    "attack", ["delete_calls", "delete_components", "mutate_source"]
)
def test_score_rejects_missing_evidence_or_stale_source(
    tmp_path: Path, attack: str
) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "attacked-run"
    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=FakeCodex(),
        test_only_allow_injected_invoker=True,
    )
    if attack == "delete_calls":
        shutil.rmtree(output / "calls")
    elif attack == "delete_components":
        shutil.rmtree(output / "components")
    else:
        manifest = json.loads(fixture["manifest"].read_text(encoding="utf-8"))
        with connection(BrainPaths.from_value(fixture["home"]).sqlite_path) as conn:
            row = conn.execute(
                "SELECT raw_path FROM documents WHERE id = ?",
                (manifest["cases"][0]["document_id"],),
            ).fetchone()
        assert row is not None
        source = Path(str(row["raw_path"]))
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "public Aster interview", "public altered interview"
            ),
            encoding="utf-8",
        )

    with pytest.raises(challenge.PublicChallengeError):
        challenge.score_public_challenge(
            fixture["manifest"],
            fixture["gold"],
            fixture["key"],
            output,
            evaluation_mode="blind_first_use",
        )


def test_artifact_match_requires_exact_subject_and_no_extra_value() -> None:
    member = {
        "subject": "Right event",
        "relation": "occurrence",
        "lifecycle": "scheduled",
        "value": "2027-09-20",
    }
    hypothesis = {
        "subject_mention_ids": ["subject-1"],
        "relation": "occurrence",
        "lifecycle": "scheduled",
        "normalized_value": "2027-09-20",
    }
    artifact = {"evidence_status": "supported", "hypotheses": [hypothesis]}

    assert challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        member,
        subject_surfaces={"subject-1": "Right event"},
    )
    assert not challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        member,
        subject_surfaces={"subject-1": "Wrong event"},
    )
    artifact["hypotheses"].append({**hypothesis, "normalized_value": "2027-09-19"})
    assert not challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        member,
        subject_surfaces={"subject-1": "Right event"},
    )


def test_gold_rejects_opaque_forbidden_values() -> None:
    with pytest.raises(challenge.PublicChallengeError, match="forbidden"):
        challenge._validate_gold(  # noqa: SLF001
            {
                "version": challenge.GOLD_VERSION,
                "created_before_predictions": True,
                "cases": [
                    {
                        "case_id": "positive",
                        "members": [
                            {
                                "subject": "Public event",
                                "relation": "occurrence",
                                "lifecycle": "scheduled",
                                "value": "2027-09-20",
                            }
                        ],
                        "forbidden": ["tomorrow_as_event_date"],
                    },
                    {"case_id": "negative", "members": []},
                ],
            }
        )


def test_legacy_v2_gold_keeps_bare_forbidden_values_and_rejects_bindings() -> None:
    legacy = {
        "version": challenge.LEGACY_GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {
                "case_id": "positive",
                "members": [
                    {
                        "subject": "Public event",
                        "relation": "occurrence",
                        "lifecycle": "scheduled",
                        "value": "2027-09-20",
                    }
                ],
                "forbidden": ["2027-09-19"],
            },
            {"case_id": "negative", "members": [], "forbidden": []},
        ],
    }
    challenge._validate_gold(legacy)  # noqa: SLF001

    legacy["cases"][0]["forbidden"] = [
        {
            "subject": "Public event",
            "relation": "occurrence",
            "lifecycle": "scheduled",
            "value": "2027-09-19",
        }
    ]
    with pytest.raises(challenge.PublicChallengeError, match="forbidden"):
        challenge._validate_gold(legacy)  # noqa: SLF001
