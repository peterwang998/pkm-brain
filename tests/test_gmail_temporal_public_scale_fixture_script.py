from __future__ import annotations

import importlib.util
import json
import stat
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "build_gmail_temporal_public_scale_fixture.py"
RUNNER_PATH = ROOT / "scripts" / "run_gmail_temporal_public_challenge.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load("test_gmail_temporal_public_scale_fixture", MODULE_PATH)
challenge = _load("test_gmail_temporal_public_scale_fixture_contract", RUNNER_PATH)


def _gold(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": challenge.GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {
                "case_id": row["case_id"],
                "members": row["members"],
                "forbidden": row["forbidden"],
                "complete_group_required": row["complete_group_required"],
            }
            for row in fixture["cases"]
        ],
    }


def test_scale_fixture_meets_frozen_public_cohort_contract() -> None:
    fixture = builder.build_fixture()
    counts = builder._fixture_counts(fixture)  # noqa: SLF001

    assert fixture["version"] == "gmail_temporal_public_challenge_fixture_v3"
    assert fixture["public_synthetic"] is True
    assert fixture["contains_private_gmail"] is False
    assert fixture["release_eligible"] is False
    assert counts == {
        "cases": 100,
        "positive_cases": 60,
        "negative_cases": 40,
        "gold_members": 88,
        "supported_gold_members": 82,
        "uncertain_gold_members": 6,
        "canonical_subject_members": 63,
        "complete_group_cases": 14,
        "plausible_hard_negative_cases": 32,
        "structured_forbidden_bindings": 46,
    }
    challenge._validate_gold(_gold(fixture))  # noqa: SLF001


def test_scale_fixture_has_required_temporal_and_negative_strata() -> None:
    fixture = builder.build_fixture()
    case_ids = {row["case_id"] for row in fixture["cases"]}

    required_prefixes = {
        "positive-schedule-": 12,
        "positive-reschedule-": 8,
        "positive-cancellation-": 6,
        "positive-completion-": 6,
        "positive-alternatives-": 6,
        "positive-dense-": 6,
        "positive-deadline-": 6,
        "positive-subject-bridge-": 4,
        "positive-effective-": 2,
        "positive-open-close-": 2,
        "positive-relative-": 2,
        "hard-negative-negation-": 8,
        "hard-negative-hypothetical-": 6,
        "hard-negative-quoted-history-": 6,
        "hard-negative-transaction-": 6,
        "hard-negative-promotion-": 6,
        "negative-wrong-scope-": 4,
        "negative-metadata-noise-": 4,
    }
    for prefix, expected in required_prefixes.items():
        assert sum(case_id.startswith(prefix) for case_id in case_ids) == expected

    complete = [
        row for row in fixture["cases"] if row["complete_group_required"] is True
    ]
    assert all(
        "reschedule" in row["case_id"] or "alternatives" in row["case_id"]
        for row in complete
    )
    assert all(
        not row["members"]
        for row in fixture["cases"]
        if row["case_id"].startswith(builder.HARD_NEGATIVE_PREFIX)
    )

    source_day = datetime.fromisoformat(fixture["message_internal_at"]).date()
    completed_values = [
        date.fromisoformat(member["value"])
        for row in fixture["cases"]
        for member in row["members"]
        if member["lifecycle"] == "completed"
    ]
    assert completed_values
    assert all(value <= source_day for value in completed_values)


def test_scale_fixture_is_public_schema_only() -> None:
    fixture = builder.build_fixture()
    emails = [fixture["account_email"], *[row["sender"] for row in fixture["cases"]]]

    assert all(builder._PUBLIC_EMAIL_RE.fullmatch(value) for value in emails)  # noqa: SLF001
    assert all(value.casefold().endswith("example.test") for value in emails)
    assert len({row["case_id"] for row in fixture["cases"]}) == 100
    assert all(set(row) == builder._CASE_KEYS for row in fixture["cases"])  # noqa: SLF001
    assert all(
        set(binding) == {"subject", "relation", "lifecycle", "value"}
        for row in fixture["cases"]
        for binding in row["forbidden"]
    )


def test_scale_fixture_and_variants_are_deterministic_and_disjoint() -> None:
    primary_first = builder.build_fixture(1)
    primary_second = builder.build_fixture(1)
    confirmation_first = builder.build_fixture(2)
    confirmation_second = builder.build_fixture(2)

    assert builder._canonical_json(primary_first) == builder._canonical_json(  # noqa: SLF001
        primary_second
    )
    assert builder._canonical_json(confirmation_first) == builder._canonical_json(  # noqa: SLF001
        confirmation_second
    )
    primary_ids = {row["case_id"] for row in primary_first["cases"]}
    confirmation_ids = {row["case_id"] for row in confirmation_first["cases"]}
    primary_texts = {(row["subject"], row["body"]) for row in primary_first["cases"]}
    confirmation_texts = {
        (row["subject"], row["body"]) for row in confirmation_first["cases"]
    }
    primary_names = {
        member["subject"]
        for row in primary_first["cases"]
        for member in row["members"]
        if member.get("canonical_subject_required") is True
    }
    confirmation_names = {
        member["subject"]
        for row in confirmation_first["cases"]
        for member in row["members"]
        if member.get("canonical_subject_required") is True
    }
    assert primary_ids.isdisjoint(confirmation_ids)
    assert primary_texts.isdisjoint(confirmation_texts)
    assert primary_names.isdisjoint(confirmation_names)
    assert primary_first["created_at"] != confirmation_first["created_at"]
    assert confirmation_first["created_at"] < primary_first["created_at"]
    assert (
        primary_first["message_internal_at"]
        != confirmation_first["message_internal_at"]
    )
    challenge._validate_gold(_gold(confirmation_first))  # noqa: SLF001


def test_writer_emits_canonical_owner_only_fixture_and_aggregate_only(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "fixture.json"
    result = builder.write_fixture(output_path, variant=2)
    fixture = builder.build_fixture(2)

    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert output_path.read_bytes() == builder._canonical_json(fixture) + b"\n"  # noqa: SLF001
    assert result == {
        "version": builder.VERSION,
        "status": "complete",
        "variant": 2,
        **builder._fixture_counts(fixture),  # noqa: SLF001
        "external_calls": 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "source_content_printed": False,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "Arbor Vale" not in serialized
    assert str(tmp_path) not in serialized
