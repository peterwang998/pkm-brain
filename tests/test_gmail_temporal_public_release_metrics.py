from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path


BASE_PATH = Path(__file__).with_name("test_gmail_temporal_public_challenge_script.py")
SPEC = importlib.util.spec_from_file_location(
    "test_gmail_temporal_public_release_metrics_base",
    BASE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)


challenge = base.challenge


def _rewrite_positive_gold_as_unmatched_deadline(
    fixture: dict[str, Path],
) -> None:
    gold = json.loads(fixture["gold"].read_text(encoding="utf-8"))
    member = gold["cases"][0]["members"][0]
    member.update(
        {
            "subject": "interview",
            "relation": "deadline",
            "lifecycle": "none",
            "canonical_subject_required": False,
        }
    )
    gold_raw = challenge._canonical_json(gold) + b"\n"  # noqa: SLF001
    fixture["gold"].write_bytes(gold_raw)

    manifest = json.loads(fixture["manifest"].read_text(encoding="utf-8"))
    manifest["gold_sha256"] = hashlib.sha256(gold_raw).hexdigest()
    manifest_raw = challenge._canonical_json(manifest) + b"\n"  # noqa: SLF001
    fixture["manifest"].write_bytes(manifest_raw)

    marker = json.loads(fixture["marker"].read_text(encoding="utf-8"))
    marker.pop("authority_hmac_sha256")
    marker["challenge_manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    marker["gold_sha256"] = manifest["gold_sha256"]
    marker = challenge._signed(  # noqa: SLF001
        marker,
        key=fixture["key"].read_bytes(),
        domain=challenge.PUBLIC_ROOT_AUTHORITY_DOMAIN,
        signature_field="authority_hmac_sha256",
    )
    fixture["marker"].write_bytes(
        challenge._canonical_json(marker) + b"\n"  # noqa: SLF001
    )


def test_deadline_is_an_explicit_critical_temporal_category() -> None:
    assert challenge._critical_temporal_categories_for_member(  # noqa: SLF001
        {
            "subject": "application",
            "relation": "deadline",
            "lifecycle": "none",
            "value": "2027-09-20",
        }
    ) == ("deadline",)


def test_unmatched_deadline_fails_explicit_release_recall_gate(
    tmp_path: Path,
) -> None:
    fixture = base._fixture(tmp_path)
    _rewrite_positive_gold_as_unmatched_deadline(fixture)
    output = tmp_path / "deadline-miss-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=base.FakeCodex(verdict="supported"),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    deadline = score["relation_metrics"]["deadline"]
    assert deadline["gold_members"] == 1
    assert deadline["matched_members"] == 0
    assert deadline["effective_member_recall"] == 0.0
    assert (
        score["personal_target_gates"]["deadline_relation_recall_at_least_0_95"]
        is False
    )


def test_reschedule_unit_requires_both_roles_in_one_group() -> None:
    members = [
        {
            "subject": "Aster interview",
            "relation": "occurrence",
            "lifecycle": "rescheduled_old",
            "value": "2027-09-20",
        },
        {
            "subject": "Aster interview",
            "relation": "occurrence",
            "lifecycle": "rescheduled_replacement",
            "value": "2027-09-22",
        },
    ]
    coherent = [
        {"matched": True, "exact": True, "structural_group_id": "group-a"},
        {"matched": True, "exact": True, "structural_group_id": "group-a"},
    ]
    split = [
        {"matched": True, "exact": True, "structural_group_id": "group-a"},
        {"matched": True, "exact": True, "structural_group_id": "group-b"},
    ]

    assert challenge._reschedule_unit_metrics(members, coherent) == {  # noqa: SLF001
        "reschedule_units": 1,
        "complete_reschedule_units": 1,
    }
    assert challenge._reschedule_unit_metrics(members, split) == {  # noqa: SLF001
        "reschedule_units": 1,
        "complete_reschedule_units": 0,
    }


def test_supported_critical_overclaim_is_a_personal_target_failure(
    tmp_path: Path,
) -> None:
    fixture = base._fixture(tmp_path, negative_candidate_bearing=True)
    output = tmp_path / "critical-overclaim-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=base.FakeCodex(verdict="supported"),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    assert score["critical_supported_overclaim_count"] > 0
    assert score["personal_target_gates"]["zero_supported_critical_overclaims"] is False


def test_supported_confidence_overclaim_on_uncertain_critical_gold_fails_gate(
    tmp_path: Path,
) -> None:
    fixture = base._fixture(
        tmp_path,
        expected_verdict="uncertain",
        gold_subject="interview",
        canonical_subject_required=False,
    )
    output = tmp_path / "critical-confidence-overclaim-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=base.FakeCodex(verdict="supported"),
        test_only_allow_injected_invoker=True,
    )
    score = challenge.score_public_challenge(
        fixture["manifest"],
        fixture["gold"],
        fixture["key"],
        output,
        evaluation_mode="development_replay",
    )

    assert score["matched_members"] == 1
    assert score["critical_artifact_overclaim_count"] == 0
    assert score["critical_supported_overclaim_count"] == 1
    assert score["personal_target_gates"]["zero_supported_critical_overclaims"] is False


def test_critical_confidence_flip_fails_verdict_stability(
    tmp_path: Path,
) -> None:
    fixture = base._fixture(
        tmp_path,
        gold_subject="interview",
        canonical_subject_required=False,
    )
    output = tmp_path / "critical-calibration-flip-run"

    challenge.run_public_challenge(
        fixture["manifest"],
        fixture["key"],
        output,
        invoke=base.SequencedFakeCodex(("supported", "uncertain", "supported")),
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
    assert stability["accepted_parent_clusters"]["gate_passed"] is True
    assert stability["accepted_gold_members"]["gate_passed"] is True
    assert stability["critical_candidate_verdict_agreement"]["gate_passed"] is False
    assert (
        stability["critical_gold_member_verdict_agreement"]["categories"]["scheduled"][
            "gate_passed"
        ]
        is False
    )
    assert stability["critical_gold_member_verdict_agreement"]["gate_passed"] is False
    assert (
        score["personal_target_gates"][
            "critical_candidate_verdict_stability_at_least_0_95"
        ]
        is False
    )
    assert (
        score["personal_target_gates"][
            "critical_gold_member_verdict_stability_at_least_0_95"
        ]
        is False
    )
    assert score["production_release_evidence_gate_passed"] is False
