from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .connector_auth import (
    OAUTH_PROVIDERS,
    canonical_oauth_scopes,
    connector_auth_status,
)
from .operations_policy import (
    CALENDAR_OWNED_READ_SCOPE,
    GMAIL_READ_SCOPE,
    OperationsPolicy,
    load_operations_policy,
    operations_policy_path,
)
from .paths import BrainPaths


DEFAULT_SHADOW_SOURCES = ("calendar", "gmail")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ShadowSetupError(RuntimeError):
    """The approved shadow defaults cannot yet be bound to authorized accounts."""


def ensure_default_operations_policy(
    paths: BrainPaths,
    *,
    timezone_name: str,
    sources: Sequence[str] = DEFAULT_SHADOW_SOURCES,
    status_reader: Callable[[BrainPaths, str], Mapping[str, Any] | None] = (
        connector_auth_status
    ),
) -> OperationsPolicy:
    """Create the owner-only approved V1 policy after separate OAuth grants exist.

    Existing policy is always authoritative and is never silently rewritten.
    The generated file contains stable account labels and policy, never tokens.
    """

    path = operations_policy_path(paths)
    if path.exists() or path.is_symlink():
        return load_operations_policy(paths)

    requested = _validated_sources(sources)
    if set(requested) != set(DEFAULT_SHADOW_SOURCES):
        raise ShadowSetupError(
            "the first shadow policy requires both Calendar and Gmail authorization"
        )
    identities: dict[str, str] = {}
    provider_subjects: dict[str, str | None] = {}
    for source in DEFAULT_SHADOW_SOURCES:
        status = status_reader(paths, source)
        if not isinstance(status, Mapping) or status.get("status") != "connected":
            raise ShadowSetupError(
                f"Connect the separate read-only {source.title()} grant before running Shadow."
            )
        account_label = str(status.get("account_label") or "").strip().lower()
        if not _EMAIL_RE.fullmatch(account_label):
            raise ShadowSetupError(
                f"The connected {source.title()} grant does not expose a valid account email."
            )
        identities[source] = account_label
        provider_subject = str(status.get("provider_subject") or "").strip()
        provider_subjects[source] = provider_subject or None

    payload = default_operations_policy_payload(
        timezone_name=timezone_name,
        calendar_email=identities["calendar"],
        gmail_email=identities["gmail"],
        calendar_provider_subject=provider_subjects["calendar"],
        gmail_provider_subject=provider_subjects["gmail"],
    )
    OperationsPolicy.from_dict(payload)
    _atomic_private_yaml(path, payload)
    try:
        return load_operations_policy(paths)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def default_operations_policy_payload(
    *,
    timezone_name: str,
    calendar_email: str,
    gmail_email: str,
    calendar_provider_subject: str | None = None,
    gmail_provider_subject: str | None = None,
) -> dict[str, Any]:
    """Return the explicitly approved, strict read-only local policy defaults."""

    return {
        "schema_version": 1,
        "policy_id": "personal-chief-of-staff-shadow",
        "policy_version": 1,
        "mode": "shadow_read_only",
        "operator": {
            "timezone": timezone_name,
            "calendar": _identity_payload(
                account_key="calendar.primary",
                email=calendar_email,
                provider_subject=calendar_provider_subject,
            ),
            "gmail": _identity_payload(
                account_key="gmail.primary",
                email=gmail_email,
                provider_subject=gmail_provider_subject,
            ),
        },
        "sources": {
            "calendar": {
                "enabled": True,
                "account_key": "calendar.primary",
                "calendar_id": "primary",
                "ownership": "owned",
                "scope": CALENDAR_OWNED_READ_SCOPE,
            },
            "gmail": {
                "enabled": True,
                "account_key": "gmail.primary",
                "scope": GMAIL_READ_SCOPE,
                "content_access_approved": True,
                "archive": {
                    "enabled": True,
                    "initial_days": 90,
                    "agent_access_approved": True,
                },
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
                "detector_calls_per_day": 100,
                "detector_input_tokens_per_day": 150_000,
                "detector_total_tokens_per_day": 180_000,
            },
        },
        "responsibility": {
            "owned": ["personal administration"],
            "shared": [],
            "adjacent": [],
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


def validate_operations_policy_auth_binding(
    paths: BrainPaths,
    policy: OperationsPolicy,
    *,
    sources: Sequence[str] = DEFAULT_SHADOW_SOURCES,
    status_reader: Callable[[BrainPaths, str], Mapping[str, Any] | None] = (
        connector_auth_status
    ),
) -> None:
    """Fail closed unless each requested grant is still the policy-bound account."""

    requested = _validated_sources(sources)
    identities = {
        "calendar": policy.operator.calendar,
        "gmail": policy.operator.gmail,
    }
    for source in requested:
        expected = identities[source]
        status = status_reader(paths, source)
        if not isinstance(status, Mapping) or status.get("status") != "connected":
            raise ShadowSetupError(
                f"The policy-bound {source.title()} account is not connected."
            )
        actual_email = str(status.get("account_label") or "").strip().lower()
        if not _EMAIL_RE.fullmatch(actual_email):
            raise ShadowSetupError(
                f"The connected {source.title()} grant does not expose a verifiable account email."
            )
        if actual_email != expected.email:
            raise ShadowSetupError(
                f"Connected {source.title()} account {actual_email} does not match "
                f"the policy-bound account {expected.email}."
            )
        granted_scopes = canonical_oauth_scopes(
            source,
            status.get("granted_scopes") or [],
        )
        approved_scopes = canonical_oauth_scopes(
            source,
            OAUTH_PROVIDERS[source].scopes,
        )
        if granted_scopes != approved_scopes:
            raise ShadowSetupError(
                f"The connected {source.title()} grant does not exactly match the "
                "approved read-only scopes; disconnect and authorize the separate "
                "narrow grant again."
            )
        if expected.provider_subject is not None:
            actual_subject = str(status.get("provider_subject") or "").strip()
            if not actual_subject:
                raise ShadowSetupError(
                    f"The connected {source.title()} grant is missing its stable provider identity."
                )
            if actual_subject != expected.provider_subject:
                raise ShadowSetupError(
                    f"The connected {source.title()} provider identity does not match policy."
                )


def _identity_payload(
    *,
    account_key: str,
    email: str,
    provider_subject: str | None,
) -> dict[str, Any]:
    output: dict[str, Any] = {"account_key": account_key, "email": email}
    if provider_subject:
        output["provider_subject"] = provider_subject
    return output


def shadow_policy_status(paths: BrainPaths) -> dict[str, Any]:
    path = operations_policy_path(paths)
    if not path.exists() and not path.is_symlink():
        return {
            "status": "not_initialized",
            "path": str(path),
            "message": "Authorize Calendar and Gmail, then run Shadow.",
        }
    try:
        policy = load_operations_policy(paths)
    except Exception as exc:
        return {
            "status": "invalid",
            "path": str(path),
            "message": str(exc),
        }
    return {
        "status": "ready",
        "path": str(path),
        "policy_version": policy.version_ref,
        "timezone": policy.operator.timezone,
        "sources": [
            source
            for source, enabled in (
                ("calendar", policy.sources.calendar.enabled),
                ("gmail", policy.sources.gmail.enabled),
            )
            if enabled
        ],
        "privacy": {
            "raw_cache_days": policy.privacy.raw_cache_days,
            "normalized_evidence_days": policy.privacy.normalized_evidence_days,
            "fetch_attachments": policy.privacy.fetch_attachments,
            "strip_quoted_history": policy.privacy.strip_quoted_history,
            "external_writes": policy.privacy.external_writes,
        },
        "gmail_archive": {
            "enabled": policy.sources.gmail.archive.enabled,
            "initial_days": policy.sources.gmail.archive.initial_days,
            "agent_access_approved": policy.sources.gmail.archive.agent_access_approved,
        },
    }


def _validated_sources(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        source = str(value).strip().casefold()
        if source not in DEFAULT_SHADOW_SOURCES:
            raise ShadowSetupError(f"unsupported Shadow source: {source or '(missing)'}")
        if source not in output:
            output.append(source)
    if not output:
        raise ShadowSetupError("at least one Shadow source is required")
    return tuple(output)


def _atomic_private_yaml(path: Path, payload: Mapping[str, Any]) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise ShadowSetupError("operations policy directory must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(os.stat(parent, follow_symlinks=False).st_mode):
        raise ShadowSetupError("operations policy parent is not a directory")
    temporary = parent / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            yaml.safe_dump(
                dict(payload),
                handle,
                sort_keys=True,
                allow_unicode=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
