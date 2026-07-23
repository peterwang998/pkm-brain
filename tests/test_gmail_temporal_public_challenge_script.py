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

ACCOUNT = "owner@public.example.test"
INTERNAL_AT = "2027-09-12T09:00:00-07:00"


class FakeCodex:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
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
                            "verdict": "unsupported",
                        }
                        for cluster in payload["page"]["clusters"]
                        for candidate_id in cluster["candidate_ids"]
                    ],
                }
                for payload in request["requests"]
            ],
        }


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


def _fixture(tmp_path: Path, *, all_zero_work: bool = False) -> dict[str, Path]:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    scheduled = _ingest_case(
        paths,
        case_id="scheduled",
        subject="Public interview schedule",
        body=(
            "The public Aster interview is scheduled for September 20, 2027. "
            "Please keep this synthetic appointment on the planning list."
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
        subject="Public sale",
        body=(
            "Advertisement: this public sale ends September 21, 2027. "
            "Shop now, save 25 percent, and unsubscribe anytime."
        ),
        labels=("CATEGORY_PROMOTIONS",),
        sender="offers@public.example.test",
    )
    gold = {
        "version": challenge.GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {
                "case_id": "scheduled",
                "members": [
                    {
                        "subject": "public Aster interview",
                        "relation": "occurrence",
                        "lifecycle": "scheduled",
                        "value": "2027-09-20",
                    }
                ],
            },
            {"case_id": "advertising", "members": []},
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
    )
    assert score["gold_opened_after_prediction_seal"] is True
    assert score["selected_negative_cases"] == 0
    assert score["smoke_gate_passed"] is False
    assert score["test_invoker_used"] is True
    assert (output / "score.json").is_file()


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
    )

    assert score["gold_members"] == 1
    assert score["matched_members"] == 0
    assert score["effective_member_recall"] == 0.0
    assert score["confirmed_member_recall"] == 0.0
    assert score["supported_artifact_precision"] == 0.0
    assert score["review_output_precision"] == 0.0
    assert score["selected_negative_cases"] == 0
    assert score["smoke_gate_passed"] is False


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

    with pytest.raises(challenge.PublicChallengeError, match="cannot span"):
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


def test_gold_v2_rejects_opaque_forbidden_values() -> None:
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
