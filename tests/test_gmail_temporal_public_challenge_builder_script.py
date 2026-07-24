from __future__ import annotations

import importlib.util
import json
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "build_gmail_temporal_public_challenge_v3.py"
SPEC = importlib.util.spec_from_file_location(
    "test_gmail_temporal_public_challenge_builder", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def _fixture() -> dict[str, Any]:
    return {
        "version": builder.FIXTURE_VERSION,
        "challenge_id": "public-v3-builder-test",
        "created_at": "2027-10-01T17:00:00+00:00",
        "message_internal_at": "2027-10-01T09:00:00-07:00",
        "account_email": "owner@public.example.test",
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "cases": [
            {
                "case_id": "scheduled-supported",
                "sender": "colleague@public.example.test",
                "subject": "Aster interview schedule",
                "body": "The Aster interview is scheduled for October 4, 2027.",
                "label_ids": ["CATEGORY_PERSONAL"],
                "members": [
                    {
                        "subject": "Aster interview",
                        "relation": "occurrence",
                        "lifecycle": "scheduled",
                        "value": "2027-10-04",
                        "expected_verdict": "supported",
                        "canonical_subject_required": True,
                    }
                ],
                "forbidden": [],
                "complete_group_required": False,
            },
            {
                "case_id": "options-and-action",
                "sender": "organizer@public.example.test",
                "subject": "Quince debrief options",
                "body": (
                    "The Quince project debrief may happen on October 5, 2027 "
                    "or October 6, 2027. I will confirm the final date tomorrow."
                ),
                "label_ids": ["CATEGORY_PERSONAL"],
                "members": [
                    {
                        "subject": "Quince project debrief",
                        "relation": "occurrence",
                        "lifecycle": "none",
                        "values": ["2027-10-05", "2027-10-06"],
                        "expected_verdict": "uncertain",
                    },
                    {
                        "subject": "confirm",
                        "relation": "unspecified",
                        "lifecycle": "none",
                        "value": "2027-10-02",
                        "expected_verdict": "supported",
                    },
                ],
                "forbidden": [
                    {
                        "subject": "Quince project debrief",
                        "relation": "occurrence",
                        "lifecycle": "none",
                        "value": "2027-10-02",
                    }
                ],
                "complete_group_required": True,
            },
            {
                "case_id": "promotion-negative",
                "sender": "offers@public.example.test",
                "subject": "Synthetic public sale",
                "body": (
                    "Advertisement: this synthetic sale ends October 8, 2027. "
                    "Shop now and unsubscribe anytime."
                ),
                "label_ids": ["CATEGORY_PROMOTIONS"],
                "members": [],
                "forbidden": [],
                "complete_group_required": False,
            },
        ],
    }


def _write_private(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _write_inputs(tmp_path: Path, fixture: dict[str, Any]) -> tuple[Path, Path]:
    fixture_path = _write_private(
        tmp_path / "fixture.json",
        builder._canonical_json(fixture) + b"\n",  # noqa: SLF001
    )
    key_path = _write_private(
        tmp_path / "key",
        b"public-v3-builder-test-key-at-least-32-bytes",
    )
    return fixture_path, key_path


def _freeze(tmp_path: Path, fixture: dict[str, Any]) -> dict[str, Any]:
    fixture_path, key_path = _write_inputs(tmp_path, fixture)
    return builder.freeze_public_challenge(
        fixture_path,
        key_path,
        tmp_path / "brain",
        tmp_path / "frozen",
    )


def test_freezer_builds_owner_only_v3_artifacts_without_external_calls(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    result = _freeze(tmp_path, fixture)

    assert result == {
        "version": builder.VERSION,
        "status": "complete",
        "cases": 3,
        "positive_cases": 2,
        "negative_cases": 1,
        "gold_members": 3,
        "supported_gold_members": 2,
        "uncertain_gold_members": 1,
        "canonical_subject_members": 1,
        "structured_forbidden_bindings": 1,
        "candidate_cases": 2,
        "zero_work_cases": 1,
        "positive_zero_work_cases": 0,
        "frontier_covered_gold_members": 3,
        "frontier_missing_gold_members": 0,
        "candidate_bearing_positive_cases": 2,
        "candidate_bearing_negative_cases": 0,
        "candidates": result["candidates"],
        "challenge_sha256": result["challenge_sha256"],
        "gold_sha256": result["gold_sha256"],
        "frontier_diagnostics_sha256": result["frontier_diagnostics_sha256"],
        "external_calls": 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "private_content_printed": False,
    }
    assert result["candidates"] >= 4

    output_root = tmp_path / "frozen"
    manifest_path = output_root / "challenge.json"
    gold_path = output_root / "gold.json"
    frontier_diagnostics_path = (
        output_root / builder.challenge.FRONTIER_DIAGNOSTICS_FILENAME
    )
    marker_path = (
        builder.BrainPaths.from_value(tmp_path / "brain").config_local
        / builder.challenge.PUBLIC_ROOT_AUTHORITY_FILENAME
    )
    for directory in (tmp_path / "brain", output_root):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for artifact in (
        manifest_path,
        gold_path,
        frontier_diagnostics_path,
        marker_path,
    ):
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600

    key = builder.challenge._key(tmp_path / "key")  # noqa: SLF001
    manifest, _ = builder.challenge._load_challenge(  # noqa: SLF001
        manifest_path,
        key=key,
    )
    assert manifest["version"] == builder.challenge.CHALLENGE_VERSION
    assert set(manifest) == builder.challenge._CHALLENGE_KEYS  # noqa: SLF001
    gold = builder.challenge._strict_json(  # noqa: SLF001
        gold_path.read_bytes(),
        label="gold",
    )
    builder.challenge._validate_gold(gold)  # noqa: SLF001
    assert gold["version"] == builder.challenge.GOLD_VERSION
    assert set(gold) == {"version", "created_before_predictions", "cases"}
    assert gold["cases"][1]["members"][1]["relation"] == "unspecified"
    assert gold["cases"][1]["members"][1]["expected_verdict"] == "supported"
    assert gold["cases"][0]["members"][0]["canonical_subject_required"] is True
    frontier_diagnostics_raw = frontier_diagnostics_path.read_bytes()
    frontier_diagnostics = builder.challenge._strict_json(  # noqa: SLF001
        frontier_diagnostics_raw,
        label="frontier diagnostics",
    )
    assert builder.challenge._verify_signed(  # noqa: SLF001
        frontier_diagnostics,
        key=key,
        domain=builder.challenge.FRONTIER_DIAGNOSTICS_DOMAIN,
        signature_field="frontier_diagnostics_hmac_sha256",
    )
    assert frontier_diagnostics["challenge_id"] == manifest["challenge_id"]
    assert (
        frontier_diagnostics["challenge_manifest_sha256"] == result["challenge_sha256"]
    )
    assert frontier_diagnostics["gold_sha256"] == result["gold_sha256"]
    assert frontier_diagnostics["fixture_sha256"] == builder._sha256(  # noqa: SLF001
        builder._canonical_json(fixture) + b"\n"  # noqa: SLF001
    )
    assert frontier_diagnostics["aggregates"] == {
        "cases": 3,
        "positive_cases": 2,
        "negative_cases": 1,
        "gold_members": 3,
        "frontier_covered_gold_members": 3,
        "frontier_missing_gold_members": 0,
        "positive_zero_work_cases": 0,
        "candidate_bearing_positive_cases": 2,
        "candidate_bearing_negative_cases": 0,
    }
    assert result["frontier_diagnostics_sha256"] == builder._sha256(  # noqa: SLF001
        frontier_diagnostics_raw
    )
    serialized_diagnostics = json.dumps(frontier_diagnostics, sort_keys=True)
    for source_value in (
        "Aster interview schedule",
        "Quince debrief options",
        "Quince project debrief",
        "colleague@public.example.test",
    ):
        assert source_value not in serialized_diagnostics

    serialized_result = json.dumps(result, sort_keys=True)
    for private_value in (
        "scheduled-supported",
        "options-and-action",
        "promotion-negative",
        "Quince project debrief",
        str(tmp_path),
    ):
        assert private_value not in serialized_result


def test_freezer_requires_fresh_exclusive_roots(tmp_path: Path) -> None:
    fixture_path, key_path = _write_inputs(tmp_path, _fixture())
    brain_home = tmp_path / "brain"
    output_root = tmp_path / "frozen"
    first = builder.freeze_public_challenge(
        fixture_path,
        key_path,
        brain_home,
        output_root,
    )
    challenge_before = (output_root / "challenge.json").read_bytes()
    gold_before = (output_root / "gold.json").read_bytes()

    with pytest.raises(
        builder.PublicChallengeFreezerError,
        match="fresh public output authority",
    ):
        builder.freeze_public_challenge(
            fixture_path,
            key_path,
            brain_home,
            output_root,
        )

    assert first["status"] == "complete"
    assert (output_root / "challenge.json").read_bytes() == challenge_before
    assert (output_root / "gold.json").read_bytes() == gold_before


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value["cases"][1].__setitem__("forbidden", ["2027-10-02"]),
        lambda value: value["cases"][0].__setitem__(
            "sender", "colleague@private.invalid"
        ),
        lambda value: value["cases"][0]["members"][0].pop("expected_verdict"),
        lambda value: value.__setitem__("created_at", "2027-09-30T17:00:00+00:00"),
    ),
)
def test_freezer_rejects_non_public_or_non_v4_fixture_contract(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    fixture = deepcopy(_fixture())
    mutate(fixture)
    fixture_path, key_path = _write_inputs(tmp_path, fixture)

    with pytest.raises(builder.PublicChallengeFreezerError):
        builder.freeze_public_challenge(
            fixture_path,
            key_path,
            tmp_path / "brain",
            tmp_path / "frozen",
        )

    assert not (tmp_path / "brain").exists()
    assert not (tmp_path / "frozen").exists()


def test_freezer_rejects_overlapping_authority_roots(tmp_path: Path) -> None:
    fixture_path, key_path = _write_inputs(tmp_path, _fixture())
    brain_home = tmp_path / "brain"

    with pytest.raises(
        builder.PublicChallengeFreezerError,
        match="roots must be disjoint",
    ):
        builder.freeze_public_challenge(
            fixture_path,
            key_path,
            brain_home,
            brain_home / "artifacts",
        )

    assert not brain_home.exists()


def test_freezer_rejects_vacuous_canonical_identity_denominator(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture["cases"][0]["members"][0].pop("canonical_subject_required")
    fixture_path, key_path = _write_inputs(tmp_path, fixture)

    with pytest.raises(
        builder.PublicChallengeFreezerError,
        match="requires a canonical named-event member",
    ):
        builder.freeze_public_challenge(
            fixture_path,
            key_path,
            tmp_path / "brain",
            tmp_path / "frozen",
        )

    assert not (tmp_path / "brain").exists()
    assert not (tmp_path / "frozen").exists()


def test_freezer_retains_gold_absent_from_production_frontier(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["cases"][0]["members"][0]["value"] = "2027-10-31"

    result = _freeze(tmp_path, fixture)

    assert result["frontier_covered_gold_members"] == 2
    assert result["frontier_missing_gold_members"] == 1
    assert result["positive_zero_work_cases"] == 0
    assert (tmp_path / "frozen" / "challenge.json").is_file()
    assert (tmp_path / "frozen" / "gold.json").is_file()


def test_freezer_retains_required_canonical_subject_absent_from_frontier(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture["cases"][1]["members"][1]["canonical_subject_required"] = True

    result = _freeze(tmp_path, fixture)

    assert result["frontier_covered_gold_members"] == 2
    assert result["frontier_missing_gold_members"] == 1


def test_freezer_retains_positive_case_with_zero_verifier_work(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["cases"][0]["body"] = "The Aster interview will happen later."

    result = _freeze(tmp_path, fixture)

    assert result["positive_cases"] == 2
    assert result["candidate_cases"] == 1
    assert result["zero_work_cases"] == 2
    assert result["positive_zero_work_cases"] == 1
    assert result["frontier_covered_gold_members"] == 2
    assert result["frontier_missing_gold_members"] == 1


def test_fixture_capacity_allows_a_128_case_public_challenge(tmp_path: Path) -> None:
    fixture = _fixture()
    cases = fixture["cases"]
    fixture["cases"] = [
        {
            **deepcopy(cases[index % len(cases)]),
            "case_id": f"scale-{index:03d}",
        }
        for index in range(128)
    ]
    fixture_path, _ = _write_inputs(tmp_path, fixture)

    loaded = builder._load_fixture(fixture_path)  # noqa: SLF001

    assert builder.MAX_CASES == 128
    assert len(loaded["cases"]) == 128


def test_unspecified_relation_accepts_uncertain_evidence_status(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["cases"][1]["members"][1]["expected_verdict"] = "uncertain"
    fixture_path, _ = _write_inputs(tmp_path, fixture)

    loaded = builder._load_fixture(fixture_path)  # noqa: SLF001

    action = loaded["cases"][1]["members"][1]
    assert action["relation"] == "unspecified"
    assert action["expected_verdict"] == "uncertain"


def test_cli_failure_is_aggregate_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _fixture()
    sentinel = "DO-NOT-ECHO-SYNTHETIC-SOURCE"
    fixture["cases"][0]["body"] = sentinel
    fixture["cases"][0]["sender"] = "private@example.invalid"
    fixture_path, key_path = _write_inputs(tmp_path, fixture)
    argv = [
        str(MODULE_PATH),
        "--fixture",
        str(fixture_path),
        "--hmac-key",
        str(key_path),
        "--brain-home",
        str(tmp_path / "brain"),
        "--output-root",
        str(tmp_path / "frozen"),
    ]

    previous = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as error:
            builder.main()
    finally:
        sys.argv = previous

    assert error.value.code == 2
    output = capsys.readouterr().out
    assert json.loads(output) == builder._safe_failure()  # noqa: SLF001
    assert sentinel not in output
    assert str(tmp_path) not in output
