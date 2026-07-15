from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from pkm_brain.operations_policy import (
    CALENDAR_OWNED_READ_SCOPE,
    GMAIL_READ_SCOPE,
    OperationsPolicy,
    OperationsPolicyError,
    load_operations_policy,
)


def valid_policy_dict() -> dict:
    return {
        "schema_version": 1,
        "policy_id": "personal-cos-shadow",
        "policy_version": 3,
        "mode": "shadow_read_only",
        "operator": {
            "timezone": "America/Los_Angeles",
            "calendar": {
                "account_key": "calendar.personal",
                "email": "owner@example.com",
            },
            "gmail": {
                "account_key": "gmail.personal",
                "email": "owner@example.com",
                "provider_subject": "google-subject-1",
            },
        },
        "sources": {
            "calendar": {
                "enabled": True,
                "account_key": "calendar.personal",
                "calendar_id": "primary",
                "ownership": "owned",
                "scope": CALENDAR_OWNED_READ_SCOPE,
            },
            "gmail": {
                "enabled": True,
                "account_key": "gmail.personal",
                "scope": GMAIL_READ_SCOPE,
                "content_access_approved": True,
            },
        },
        "privacy": {
            "raw_cache_days": 7,
            "normalized_evidence_days": 30,
            "fetch_attachments": False,
            "strip_quoted_history": True,
            "external_writes": False,
        },
        "budgets": {
            "calendar": {"requests_per_day": 500},
            "gmail": {
                "api_requests_per_day": 10_000,
                "detector_calls_per_day": 1_000,
                "detector_input_tokens_per_day": 250_000,
                "detector_total_tokens_per_day": 500_000,
            },
        },
        "responsibility": {
            "owned": ["personal administration", "pkm-brain"],
            "shared": ["household"],
            "adjacent": ["community"],
            "out_of_area_action": "demote",
            "unknown_action": "surface_unknown",
            "direct_obligations_remain_eligible": True,
            "high_consequence": {
                "categories": [
                    "legal",
                    "financial",
                    "security",
                    "safety",
                    "travel",
                    "direct_commitment",
                ],
                "remain_eligible": True,
                "never_auto_suppress": True,
            },
        },
    }


def write_policy(home: Path, payload: dict, *, mode: int = 0o600) -> Path:
    path = home / "config" / "local" / "operations.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    path.chmod(mode)
    return path


def test_strict_operations_policy_loads_from_private_local_config(
    tmp_path: Path,
) -> None:
    write_policy(tmp_path, valid_policy_dict())

    policy = load_operations_policy(tmp_path)

    assert policy.version_ref == "personal-cos-shadow@3"
    assert policy.operator.timezone == "America/Los_Angeles"
    assert policy.sources.calendar.calendar_id == "primary"
    assert policy.sources.gmail.account_key == "gmail.personal"
    assert policy.sources.gmail.archive.enabled is False
    assert policy.sources.gmail.archive.initial_days == 90
    assert policy.sources.gmail.archive.agent_access_approved is False
    assert policy.privacy.raw_cache_days == 7
    assert policy.privacy.normalized_evidence_days == 30
    assert policy.privacy.fetch_attachments is False
    assert policy.privacy.strip_quoted_history is True
    assert policy.privacy.external_writes is False
    assert policy.responsibility.out_of_area_action == "demote"
    assert policy.responsibility.high_consequence_never_auto_suppress is True


def test_policy_can_keep_gmail_disabled_without_implying_content_authorization() -> (
    None
):
    payload = valid_policy_dict()
    payload["sources"]["gmail"]["enabled"] = False
    payload["sources"]["gmail"]["content_access_approved"] = False

    policy = OperationsPolicy.from_dict(payload)

    assert policy.sources.gmail.enabled is False
    assert policy.sources.gmail.content_access_approved is False


def test_policy_accepts_explicit_complete_encrypted_gmail_history() -> None:
    payload = valid_policy_dict()
    payload["sources"]["gmail"]["archive"] = {
        "enabled": True,
        "initial_days": 90,
        "agent_access_approved": True,
    }

    policy = OperationsPolicy.from_dict(payload)

    assert policy.sources.gmail.archive.enabled is True
    assert policy.sources.gmail.archive.initial_days == 90


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("initial_days", 30, "initial_days must be 90"),
        ("agent_access_approved", False, "agent_access_approved"),
    ],
)
def test_enabled_gmail_history_rejects_unsafe_variants(
    field: str,
    value: object,
    match: str,
) -> None:
    payload = valid_policy_dict()
    payload["sources"]["gmail"]["archive"] = {
        "enabled": True,
        "initial_days": 90,
        "agent_access_approved": True,
    }
    payload["sources"]["gmail"]["archive"][field] = value

    with pytest.raises(OperationsPolicyError, match=match):
        OperationsPolicy.from_dict(payload)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda data: data.__setitem__("mode", "read_write"),
            "shadow_read_only",
        ),
        (
            lambda data: data["operator"]["gmail"].__setitem__(
                "account_key", "calendar.personal"
            ),
            "separate account_key",
        ),
        (
            lambda data: data["sources"]["calendar"].__setitem__("calendar_id", "all"),
            "owned primary calendar",
        ),
        (
            lambda data: data["sources"]["calendar"].__setitem__(
                "scope", "https://www.googleapis.com/auth/calendar.readonly"
            ),
            "calendar.events.owned.readonly",
        ),
        (
            lambda data: data["sources"]["gmail"].__setitem__(
                "content_access_approved", False
            ),
            "content_access_approved",
        ),
        (
            lambda data: data["privacy"].__setitem__("raw_cache_days", 8),
            "must be 7",
        ),
        (
            lambda data: data["privacy"].__setitem__("normalized_evidence_days", 31),
            "must be 30",
        ),
        (
            lambda data: data["privacy"].__setitem__("fetch_attachments", True),
            "attachment fetching",
        ),
        (
            lambda data: data["privacy"].__setitem__("strip_quoted_history", False),
            "quoted reply history",
        ),
        (
            lambda data: data["privacy"].__setitem__("external_writes", True),
            "external writes",
        ),
        (
            lambda data: data["responsibility"].__setitem__(
                "out_of_area_action", "exclude"
            ),
            "must be demote",
        ),
        (
            lambda data: data["responsibility"]["high_consequence"][
                "categories"
            ].remove("financial"),
            "financial",
        ),
        (
            lambda data: data["responsibility"]["high_consequence"].__setitem__(
                "never_auto_suppress", False
            ),
            "never auto-suppress",
        ),
    ],
)
def test_policy_rejects_unsafe_trial_variants(mutate, match: str) -> None:
    payload = valid_policy_dict()
    mutate(payload)

    with pytest.raises(OperationsPolicyError, match=match):
        OperationsPolicy.from_dict(payload)


def test_policy_rejects_unknown_fields_and_credential_material() -> None:
    payload = valid_policy_dict()
    payload["operator"]["gmail"]["refresh_token"] = "do-not-store"

    with pytest.raises(OperationsPolicyError, match="Keychain"):
        OperationsPolicy.from_dict(payload)

    payload = valid_policy_dict()
    payload["privacy"]["debug"] = True
    with pytest.raises(OperationsPolicyError, match="unknown privacy field"):
        OperationsPolicy.from_dict(payload)


def test_policy_rejects_invalid_timezone_overlapping_responsibility_and_budget() -> (
    None
):
    payload = valid_policy_dict()
    payload["operator"]["timezone"] = "Mars/Olympus"
    with pytest.raises(OperationsPolicyError, match="unknown operator timezone"):
        OperationsPolicy.from_dict(payload)

    payload = valid_policy_dict()
    payload["responsibility"]["adjacent"].append("pkm-brain")
    with pytest.raises(OperationsPolicyError, match="must be disjoint"):
        OperationsPolicy.from_dict(payload)

    payload = valid_policy_dict()
    payload["budgets"]["gmail"]["detector_calls_per_day"] = 0
    with pytest.raises(OperationsPolicyError, match="at least 1"):
        OperationsPolicy.from_dict(payload)


def test_private_policy_rejects_group_readable_file_and_symlink(tmp_path: Path) -> None:
    path = write_policy(tmp_path, valid_policy_dict(), mode=0o644)
    with pytest.raises(OperationsPolicyError, match="chmod 600"):
        load_operations_policy(path)

    path.chmod(0o600)
    link = tmp_path / "operations.yaml"
    link.symlink_to(path)
    with pytest.raises(OperationsPolicyError, match="non-symlink"):
        load_operations_policy(link)


def test_policy_requires_all_declared_contract_fields() -> None:
    payload = deepcopy(valid_policy_dict())
    del payload["privacy"]["external_writes"]

    with pytest.raises(OperationsPolicyError, match="missing privacy field"):
        OperationsPolicy.from_dict(payload)
