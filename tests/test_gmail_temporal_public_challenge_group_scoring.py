from __future__ import annotations

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
