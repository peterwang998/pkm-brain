from __future__ import annotations

import stat
from pathlib import Path

import pytest

from pkm_brain.operations_policy import (
    CALENDAR_OWNED_READ_SCOPE,
    GMAIL_READ_SCOPE,
    load_operations_policy,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.operational_service import OperationalService
from pkm_brain.shadow_controller import ShadowTrialController
import pkm_brain.shadow_controller as shadow_controller_module
from pkm_brain.shadow_setup import (
    ShadowSetupError,
    ensure_default_operations_policy,
    shadow_policy_status,
    validate_operations_policy_auth_binding,
)


def connected_status(_paths: BrainPaths, source: str) -> dict:
    return {
        "status": "connected",
        "account_label": f"{source}@example.com",
        "provider_subject": f"google-subject-{source}",
        "granted_scopes": (
            [
                "openid",
                "email",
                "profile",
                CALENDAR_OWNED_READ_SCOPE,
            ]
            if source == "calendar"
            else ["openid", "email", "profile", GMAIL_READ_SCOPE]
        ),
    }


def test_approved_policy_is_created_private_and_never_contains_credentials(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    policy = ensure_default_operations_policy(
        paths,
        timezone_name="America/Los_Angeles",
        status_reader=connected_status,
    )

    assert policy.operator.calendar.email == "calendar@example.com"
    assert policy.operator.gmail.email == "gmail@example.com"
    assert policy.operator.calendar.provider_subject == "google-subject-calendar"
    assert policy.operator.gmail.provider_subject == "google-subject-gmail"
    assert policy.sources.calendar.calendar_id == "primary"
    assert policy.sources.calendar.scope == CALENDAR_OWNED_READ_SCOPE
    assert policy.sources.gmail.scope == GMAIL_READ_SCOPE
    assert policy.privacy.raw_cache_days == 7
    assert policy.privacy.normalized_evidence_days == 30
    assert policy.privacy.fetch_attachments is False
    assert policy.privacy.strip_quoted_history is True
    assert policy.privacy.external_writes is False
    path = paths.config_local / "operations.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    contents = path.read_text(encoding="utf-8")
    assert "access_token" not in contents
    assert "refresh_token" not in contents
    assert shadow_policy_status(paths)["status"] == "ready"


def test_existing_policy_is_loaded_without_reconsulting_auth(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    first = ensure_default_operations_policy(
        paths,
        timezone_name="America/Los_Angeles",
        status_reader=connected_status,
    )

    def fail_if_called(_paths: BrainPaths, _source: str) -> dict:
        raise AssertionError("existing policy must remain authoritative")

    second = ensure_default_operations_policy(
        paths,
        timezone_name="UTC",
        status_reader=fail_if_called,
    )

    assert second == first
    assert load_operations_policy(paths).operator.timezone == "America/Los_Angeles"


def test_policy_creation_requires_both_separate_connected_grants(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    def only_calendar(_paths: BrainPaths, source: str) -> dict:
        return {
            "status": "connected" if source == "calendar" else "ready",
            "account_label": f"{source}@example.com",
        }

    with pytest.raises(ShadowSetupError, match="separate read-only Gmail"):
        ensure_default_operations_policy(
            paths,
            timezone_name="America/Los_Angeles",
            status_reader=only_calendar,
        )
    assert not (paths.config_local / "operations.yaml").exists()


def test_policy_auth_binding_is_exact_and_fails_closed(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    policy = ensure_default_operations_policy(
        paths,
        timezone_name="America/Los_Angeles",
        status_reader=connected_status,
    )

    validate_operations_policy_auth_binding(
        paths,
        policy,
        status_reader=connected_status,
    )

    def google_identity_aliases(_paths: BrainPaths, source: str) -> dict:
        status = connected_status(_paths, source)
        status["granted_scopes"] = [
            {
                "email": "https://www.googleapis.com/auth/userinfo.email",
                "profile": "https://www.googleapis.com/auth/userinfo.profile",
            }.get(scope, scope)
            for scope in status["granted_scopes"]
        ]
        return status

    validate_operations_policy_auth_binding(
        paths,
        policy,
        status_reader=google_identity_aliases,
    )

    def wrong_calendar(_paths: BrainPaths, source: str) -> dict:
        status = connected_status(_paths, source)
        if source == "calendar":
            status["account_label"] = "other@example.com"
        return status

    with pytest.raises(ShadowSetupError, match="does not match the policy-bound"):
        validate_operations_policy_auth_binding(
            paths,
            policy,
            status_reader=wrong_calendar,
        )

    def missing_subject(_paths: BrainPaths, source: str) -> dict:
        status = connected_status(_paths, source)
        status["provider_subject"] = None
        return status

    with pytest.raises(ShadowSetupError, match="missing its stable provider identity"):
        validate_operations_policy_auth_binding(
            paths,
            policy,
            status_reader=missing_subject,
        )

    def broader_gmail_scope(_paths: BrainPaths, source: str) -> dict:
        status = connected_status(_paths, source)
        if source == "gmail":
            status["granted_scopes"].append(
                "https://www.googleapis.com/auth/gmail.modify"
            )
        return status

    with pytest.raises(ShadowSetupError, match="approved read-only scopes"):
        validate_operations_policy_auth_binding(
            paths,
            policy,
            status_reader=broader_gmail_scope,
        )


def test_shadow_controller_revalidates_binding_before_starting_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    policy = ensure_default_operations_policy(
        paths,
        timezone_name="America/Los_Angeles",
        status_reader=connected_status,
    )
    monkeypatch.setattr(
        shadow_controller_module,
        "ensure_default_operations_policy",
        lambda *_args, **_kwargs: policy,
    )

    def reject_binding(*_args, **_kwargs) -> None:
        raise ShadowSetupError("bound account changed")

    monkeypatch.setattr(
        shadow_controller_module,
        "validate_operations_policy_auth_binding",
        reject_binding,
    )
    controller = ShadowTrialController(
        paths,
        OperationalService(paths, writer_guard=lambda: None),
    )

    with pytest.raises(ShadowSetupError, match="bound account changed"):
        controller.start(
            timezone_name="America/Los_Angeles",
            sources=("calendar", "gmail"),
        )
