from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "run_gmail_temporal_public_challenge.py"
SPEC = importlib.util.spec_from_file_location(
    "test_gmail_temporal_public_challenge_group_scoring_module",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
challenge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = challenge
SPEC.loader.exec_module(challenge)


def _artifact(artifact_id: str, mention_id: str, value: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "evidence_status": "uncertain",
        "hypotheses": [
            {
                "subject_mention_ids": [mention_id],
                "relation": "occurrence",
                "lifecycle": "none",
                "normalized_value": value,
            }
        ],
    }


def test_multi_value_gold_matches_complete_per_expression_alternatives_group() -> None:
    projection = {
        "artifacts": [
            _artifact("a1", "m1", "2027-09-25"),
            _artifact("a2", "m1", "2027-09-26"),
        ],
        "groups": [
            {
                "group_id": "g1",
                "kind": "alternatives",
                "coverage": "complete",
                "subject_family_id": "family-1",
                "members": [
                    {
                        "role": "alternative",
                        "state": "present",
                        "artifact_ids": ["a1"],
                        "cluster_review_ids": [],
                    },
                    {
                        "role": "alternative",
                        "state": "present",
                        "artifact_ids": ["a2"],
                        "cluster_review_ids": [],
                    },
                ],
            }
        ],
    }
    member = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "values": ["2027-09-25", "2027-09-26"],
        "expected_verdict": "uncertain",
    }

    assert challenge._alternatives_artifacts(  # noqa: SLF001
        projection,
        member,
        subject_surfaces={"m1": "Marigold project debrief"},
    ) == (("a1", "a2"), "g1")

    for artifact in projection["artifacts"]:
        artifact["evidence_status"] = "supported"
    assert challenge._alternatives_artifacts(  # noqa: SLF001
        projection,
        member,
        subject_surfaces={"m1": "Marigold project debrief"},
    ) == (("a1", "a2"), "g1")


def test_alternatives_group_rejects_impure_or_incomplete_identity() -> None:
    projection = {
        "artifacts": [
            _artifact("a1", "m1", "2027-09-25"),
            _artifact("a2", "m2", "2027-09-26"),
        ],
        "groups": [
            {
                "group_id": "g1",
                "kind": "alternatives",
                "coverage": "complete",
                "subject_family_id": "family-1",
                "members": [
                    {
                        "role": "alternative",
                        "state": "present",
                        "artifact_ids": ["a1"],
                        "cluster_review_ids": [],
                    },
                    {
                        "role": "alternative",
                        "state": "present",
                        "artifact_ids": ["a2"],
                        "cluster_review_ids": [],
                    },
                ],
            }
        ],
    }
    member = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "values": ["2027-09-25", "2027-09-26"],
        "expected_verdict": "uncertain",
    }

    assert (
        challenge._alternatives_artifacts(  # noqa: SLF001
            projection,
            member,
            subject_surfaces={
                "m1": "Marigold project debrief",
                "m2": "confirm",
            },
        )
        is None
    )


def test_bare_forbidden_value_remains_case_wide_after_gold_is_frozen() -> None:
    confirm_hypothesis = {
        "subject_mention_ids": ["confirm"],
        "relation": "unspecified",
        "lifecycle": "none",
        "normalized_value": "2027-09-19",
    }
    surfaces = {
        "confirm": "confirm",
    }

    assert challenge._forbidden_hypothesis_matches(  # noqa: SLF001
        confirm_hypothesis,
        "2027-09-19",
        subject_surfaces=surfaces,
    )


def test_structured_forbidden_binding_can_share_date_with_distinct_action() -> None:
    forbidden = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "value": "2027-09-19",
    }
    confirm_hypothesis = {
        "subject_mention_ids": ["confirm"],
        "relation": "unspecified",
        "lifecycle": "none",
        "normalized_value": "2027-09-19",
    }

    assert not challenge._forbidden_hypothesis_matches(  # noqa: SLF001
        confirm_hypothesis,
        forbidden,
        subject_surfaces={"confirm": "confirm"},
    )


def test_gold_accepts_structured_forbidden_distinct_binding() -> None:
    challenge._validate_gold(  # noqa: SLF001
        {
            "version": challenge.GOLD_VERSION,
            "created_before_predictions": True,
            "cases": [
                {
                    "case_id": "positive",
                    "members": [
                        {
                            "subject": "Marigold project debrief",
                            "relation": "occurrence",
                            "lifecycle": "none",
                            "value": "2027-09-25",
                            "expected_verdict": "supported",
                        }
                    ],
                    "forbidden": [
                        {
                            "subject": "confirm final date",
                            "relation": "deadline",
                            "lifecycle": "none",
                            "value": "2027-09-25",
                        }
                    ],
                },
                {"case_id": "negative", "members": []},
            ],
        }
    )


def test_gold_rejects_alias_equivalent_structured_forbidden_binding() -> None:
    with pytest.raises(challenge.PublicChallengeError, match="contradicts"):
        challenge._validate_gold(  # noqa: SLF001
            {
                "version": challenge.GOLD_VERSION,
                "created_before_predictions": True,
                "cases": [
                    {
                        "case_id": "positive",
                        "members": [
                            {
                                "subject": "Lumen Quay planning session",
                                "relation": "occurrence",
                                "lifecycle": "scheduled",
                                "value": "2027-09-20",
                                "expected_verdict": "supported",
                            }
                        ],
                        "forbidden": [
                            {
                                "subject": "Lumen Quay",
                                "relation": "occurrence",
                                "lifecycle": "scheduled",
                                "value": "2027-09-20",
                            }
                        ],
                    },
                    {"case_id": "negative", "members": []},
                ],
            }
        )


@pytest.mark.parametrize(
    ("expected", "actual"),
    (
        ("Lumen Quay planning session", "Lumen Quay"),
        ("Lumen Quay", "Lumen Quay planning session"),
        ("Lumen Quay planning session", "Lumen Quay session"),
        (
            "Reminder Lumen Quay planning session update",
            "Lumen Quay session",
        ),
        ("Northstar design review", "Northstar review"),
    ),
)
def test_subject_alias_matching_preserves_structurally_compatible_aliases(
    expected: str,
    actual: str,
) -> None:
    assert challenge._subject_matches(expected, actual)  # noqa: SLF001


@pytest.mark.parametrize(
    "actual",
    (
        "Lumen Quay billing review",
        "Lumen Quay design review",
        "Lumen Quay interview",
        "Lumen Quay design session",
    ),
)
def test_subject_alias_matching_does_not_collapse_distinct_named_events(
    actual: str,
) -> None:
    assert not challenge._subject_matches(  # noqa: SLF001
        "Lumen Quay planning session",
        actual,
    )


def test_supported_gold_counts_pure_uncertainty_as_effective_not_confirmed() -> None:
    artifact = _artifact("a1", "m1", "2027-09-25")
    member = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "value": "2027-09-25",
    }

    assert challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        member,
        subject_surfaces={"m1": "Marigold project debrief"},
    )
    assert not challenge._artifacts_confirm_supported_member(  # noqa: SLF001
        (artifact,),
        member,
    )

    supported = {**artifact, "evidence_status": "supported"}
    assert challenge._artifacts_confirm_supported_member(  # noqa: SLF001
        (supported,),
        member,
    )
    assert (
        challenge._supported_artifact_calibration(  # noqa: SLF001
            supported,
            "supported",
        )
        == "calibrated"
    )


def test_uncertain_gold_keeps_supported_semantic_match_but_flags_overconfidence() -> (
    None
):
    artifact = {
        **_artifact("a1", "m1", "2027-09-25"),
        "evidence_status": "supported",
    }
    member = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "value": "2027-09-25",
        "expected_verdict": "uncertain",
    }
    uncertain_artifact = {**artifact, "evidence_status": "uncertain"}

    assert challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        member,
        subject_surfaces={"m1": "Marigold project debrief"},
    )
    assert not challenge._artifacts_confirm_supported_member(  # noqa: SLF001
        (artifact,),
        member,
    )
    assert (
        challenge._supported_artifact_calibration(  # noqa: SLF001
            artifact,
            "uncertain",
        )
        == "overconfident"
    )
    assert challenge._exact_artifact_match(  # noqa: SLF001
        uncertain_artifact,
        member,
        subject_surfaces={"m1": "Marigold project debrief"},
    )
    assert (
        challenge._supported_artifact_calibration(  # noqa: SLF001
            uncertain_artifact,
            "uncertain",
        )
        is None
    )
    for invalid_status in ("unsupported", None):
        assert not challenge._exact_artifact_match(  # noqa: SLF001
            {**artifact, "evidence_status": invalid_status},
            member,
            subject_surfaces={"m1": "Marigold project debrief"},
        )


def test_exact_match_selection_is_order_independent_and_calibration_preferred() -> None:
    uncertain = _artifact("z-uncertain", "m1", "2027-09-25")
    supported = _artifact("a-supported", "m1", "2027-09-25")
    supported["evidence_status"] = "supported"
    artifacts = {
        "z-uncertain": uncertain,
        "a-supported": supported,
    }
    member = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "value": "2027-09-25",
        "expected_verdict": "supported",
    }

    assert (
        challenge._best_exact_artifact_id(  # noqa: SLF001
            artifacts,
            member,
            subject_surfaces={"m1": "Marigold project debrief"},
            artifact_subject_aliases={},
            excluded_artifact_ids=set(),
        )
        == "a-supported"
    )
    assert (
        challenge._best_exact_artifact_id(  # noqa: SLF001
            dict(reversed(tuple(artifacts.items()))),
            {**member, "expected_verdict": "uncertain"},
            subject_surfaces={"m1": "Marigold project debrief"},
            artifact_subject_aliases={},
            excluded_artifact_ids=set(),
        )
        == "z-uncertain"
    )


def test_cluster_reviews_are_unscored_workload_not_false_artifacts() -> None:
    precision, all_outputs_scored = challenge._review_artifact_metrics(  # noqa: SLF001
        artifact_count=2,
        matched_artifact_count=2,
        cluster_review_count=1,
        gold_member_count=2,
    )

    assert precision == 1.0
    assert all_outputs_scored is False


def test_complete_production_subject_family_expands_only_its_artifact() -> None:
    artifact = {
        **_artifact("a1", "bare", "2027-09-22"),
        "evidence_status": "supported",
        "parent_cluster_id": "cluster-1",
    }
    analysis_fingerprint = "analysis-1"
    family_id = (
        "gtrsf_"
        + hashlib.sha256(
            challenge._canonical_json(  # noqa: SLF001
                {
                    "analysis_fingerprint": analysis_fingerprint,
                    "subject_mention_ids": ["bare", "full"],
                }
            )
        ).hexdigest()
    )
    projection = {
        "version": challenge._LEGACY_PROJECTION_VERSION,  # noqa: SLF001
        "analysis_fingerprint": analysis_fingerprint,
        "artifacts": [artifact],
        "groups": [
            {
                "group_id": "g1",
                "kind": "single",
                "coverage": "complete",
                "subject_family_id": family_id,
                "members": [
                    {
                        "role": "independent",
                        "state": "present",
                        "artifact_ids": ["a1"],
                        "cluster_review_ids": [],
                        "subject_family_ids": [family_id],
                        "reasons": [],
                    }
                ],
            }
        ],
    }
    surfaces = {
        "bare": "session",
        "full": "Lumen Quay planning session",
        "different": "Lumen Quay design review",
    }
    aliases = challenge._artifact_subject_aliases(  # noqa: SLF001
        projection,
        subject_surfaces=surfaces,
        parent_cluster_subject_ids={
            "cluster-1": frozenset({"bare", "full"}),
        },
    )
    planning_member = {
        "subject": "Lumen Quay planning session",
        "relation": "occurrence",
        "lifecycle": "none",
        "value": "2027-09-22",
    }

    assert aliases == {"a1": frozenset({"session", "Lumen Quay planning session"})}
    assert not challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        planning_member,
        subject_surfaces=surfaces,
    )
    assert challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        planning_member,
        subject_surfaces=surfaces,
        subject_alias_surfaces=aliases["a1"],
    )
    assert not challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        {**planning_member, "subject": "Lumen Quay design review"},
        subject_surfaces=surfaces,
        subject_alias_surfaces=aliases["a1"],
    )

    projection["groups"][0]["coverage"] = "incomplete"
    assert (
        challenge._artifact_subject_aliases(  # noqa: SLF001
            projection,
            subject_surfaces=surfaces,
            parent_cluster_subject_ids={
                "cluster-1": frozenset({"bare", "full"}),
            },
        )
        == {}
    )


def test_v3_projection_scores_only_its_exported_subject_alias_family() -> None:
    artifact = {
        **_artifact("a1", "bare", "2027-09-22"),
        "evidence_status": "supported",
    }
    hypothesis = artifact["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis.update(
        {
            "subject_alias_mention_ids": ["bare", "full"],
            "subject_alias_type_references": [
                ["bare", "event_reference"],
                ["full", "event_title_candidate"],
            ],
            "canonical_subject_mention_id": "full",
        }
    )
    surfaces = {
        "bare": "session",
        "full": "Lumen Quay planning session",
        "different": "Lumen Quay design review",
    }
    aliases = challenge._artifact_subject_aliases(  # noqa: SLF001
        {
            "version": challenge._PROJECTION_VERSION,  # noqa: SLF001
            "artifacts": [artifact],
        },
        subject_surfaces=surfaces,
        parent_cluster_subject_ids={
            "untrusted-cluster": frozenset({"bare", "full", "different"})
        },
    )
    planning_member = {
        "subject": "Lumen Quay planning session",
        "relation": "occurrence",
        "lifecycle": "none",
        "value": "2027-09-22",
    }

    assert aliases == {"a1": frozenset({"session", "Lumen Quay planning session"})}
    assert challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        planning_member,
        subject_surfaces=surfaces,
        subject_alias_surfaces=aliases["a1"],
    )
    assert not challenge._exact_artifact_match(  # noqa: SLF001
        artifact,
        {**planning_member, "subject": "Lumen Quay design review"},
        subject_surfaces=surfaces,
        subject_alias_surfaces=aliases["a1"],
    )


def test_canonical_subject_recovery_requires_every_exact_named_title() -> None:
    member = {
        "subject": "Lumen Quay planning session",
        "relation": "occurrence",
        "lifecycle": "scheduled",
        "value": "2027-09-22",
        "expected_verdict": "supported",
        "canonical_subject_required": True,
    }
    hypothesis = {
        "subject_mention_ids": ["bare"],
        "subject_alias_mention_ids": ["bare", "full"],
        "canonical_subject_mention_id": "full",
        "relation": "occurrence",
        "lifecycle": "scheduled",
        "normalized_value": "2027-09-22",
    }
    artifact = {"evidence_status": "supported", "hypotheses": [hypothesis]}
    surfaces = {
        "bare": "session",
        "full": "Lumen Quay planning session",
        "short": "Lumen Quay session",
    }

    assert challenge._artifacts_recover_canonical_subject(  # noqa: SLF001
        (artifact,),
        member,
        subject_surfaces=surfaces,
    )

    for canonical_id in (None, "short", "missing"):
        changed = {
            **artifact,
            "hypotheses": [
                {**hypothesis, "canonical_subject_mention_id": canonical_id}
            ],
        }
        assert not challenge._artifacts_recover_canonical_subject(  # noqa: SLF001
            (changed,),
            member,
            subject_surfaces=surfaces,
        )

    mixed = {
        **artifact,
        "hypotheses": [
            hypothesis,
            {**hypothesis, "canonical_subject_mention_id": None},
        ],
    }
    assert not challenge._artifacts_recover_canonical_subject(  # noqa: SLF001
        (mixed,),
        member,
        subject_surfaces=surfaces,
    )


def test_canonical_subject_flag_is_current_v4_only_and_must_be_boolean() -> None:
    member = {
        "subject": "Lumen Quay planning session",
        "relation": "occurrence",
        "lifecycle": "scheduled",
        "value": "2027-09-22",
        "expected_verdict": "supported",
        "canonical_subject_required": True,
    }
    current = {
        "version": challenge.GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {"case_id": "positive", "members": [member]},
            {"case_id": "negative", "members": []},
        ],
    }

    challenge._validate_gold(current)  # noqa: SLF001

    malformed = {
        **current,
        "cases": [
            {
                "case_id": "positive",
                "members": [{**member, "canonical_subject_required": 1}],
            },
            {"case_id": "negative", "members": []},
        ],
    }
    with pytest.raises(challenge.PublicChallengeError, match="member schema"):
        challenge._validate_gold(malformed)  # noqa: SLF001

    legacy = {
        **current,
        "version": challenge.LEGACY_STRUCTURED_GOLD_VERSION,
    }
    with pytest.raises(challenge.PublicChallengeError, match="member schema"):
        challenge._validate_gold(legacy)  # noqa: SLF001


def test_gold_rejects_calibration_only_duplicate_members() -> None:
    member = {
        "subject": "Lumen Quay planning session",
        "relation": "occurrence",
        "lifecycle": "scheduled",
        "value": "2027-09-22",
        "expected_verdict": "supported",
        "canonical_subject_required": True,
    }
    gold = {
        "version": challenge.GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {
                "case_id": "positive",
                "members": [
                    member,
                    {
                        **member,
                        "expected_verdict": "uncertain",
                        "canonical_subject_required": False,
                    },
                ],
            },
            {"case_id": "negative", "members": []},
        ],
    }

    with pytest.raises(challenge.PublicChallengeError, match="duplicated"):
        challenge._validate_gold(gold)  # noqa: SLF001


def test_structural_component_keys_keep_independent_groups_separate() -> None:
    first_options = {
        "subject": "Marigold project debrief",
        "relation": "occurrence",
        "lifecycle": "none",
        "values": ["2027-09-25", "2027-09-26"],
        "expected_verdict": "uncertain",
    }
    second_options = {
        "subject": "Cedar planning session",
        "relation": "occurrence",
        "lifecycle": "none",
        "values": ["2027-10-01", "2027-10-02"],
        "expected_verdict": "uncertain",
    }
    old = {
        "subject": "Lumen Quay planning session",
        "relation": "occurrence",
        "lifecycle": "rescheduled_old",
        "value": "2027-09-19",
    }
    replacement = {
        **old,
        "lifecycle": "rescheduled_replacement",
        "value": "2027-09-22",
    }

    assert challenge._structural_component_key(  # noqa: SLF001
        first_options, 0
    ) != challenge._structural_component_key(second_options, 1)  # noqa: SLF001
    assert challenge._structural_component_key(  # noqa: SLF001
        old, 2
    ) == challenge._structural_component_key(replacement, 3)  # noqa: SLF001
