from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import stat
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .paths import BrainPaths


OPERATIONS_POLICY_SCHEMA_VERSION = 1
OPERATIONS_POLICY_FILENAME = "operations.yaml"
SHADOW_READ_ONLY_MODE = "shadow_read_only"
CALENDAR_OWNED_READ_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events.owned.readonly"
)
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
REQUIRED_HIGH_CONSEQUENCE_CATEGORIES = frozenset(
    {"legal", "financial", "security", "safety", "travel", "direct_commitment"}
)
_ACCOUNT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SECRET_KEY_PARTS = {
    "access_token",
    "api_key",
    "client_secret",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "token",
}


class OperationsPolicyError(ValueError):
    """Raised when the local Chief-of-Staff policy is unsafe or malformed."""


@dataclass(frozen=True)
class ConnectorIdentity:
    account_key: str
    email: str
    provider_subject: str | None = None


@dataclass(frozen=True)
class OperatorPolicy:
    timezone: str
    calendar: ConnectorIdentity
    gmail: ConnectorIdentity


@dataclass(frozen=True)
class CalendarSourcePolicy:
    enabled: bool
    account_key: str
    calendar_id: str
    ownership: str
    scope: str


@dataclass(frozen=True)
class GmailArchivePolicy:
    enabled: bool
    initial_days: int
    agent_access_approved: bool


@dataclass(frozen=True)
class GmailSourcePolicy:
    enabled: bool
    account_key: str
    scope: str
    content_access_approved: bool
    archive: GmailArchivePolicy


@dataclass(frozen=True)
class SourcePolicy:
    calendar: CalendarSourcePolicy
    gmail: GmailSourcePolicy


@dataclass(frozen=True)
class PrivacyPolicy:
    raw_cache_days: int
    normalized_evidence_days: int
    fetch_attachments: bool
    strip_quoted_history: bool
    external_writes: bool


@dataclass(frozen=True)
class CalendarBudget:
    requests_per_day: int


@dataclass(frozen=True)
class GmailBudget:
    api_requests_per_day: int
    detector_calls_per_day: int
    detector_input_tokens_per_day: int
    detector_total_tokens_per_day: int


@dataclass(frozen=True)
class BudgetPolicy:
    calendar: CalendarBudget
    gmail: GmailBudget


@dataclass(frozen=True)
class ResponsibilityPolicy:
    owned: tuple[str, ...]
    shared: tuple[str, ...]
    adjacent: tuple[str, ...]
    out_of_area_action: str
    unknown_action: str
    direct_obligations_remain_eligible: bool
    high_consequence_categories: tuple[str, ...]
    high_consequence_remains_eligible: bool
    high_consequence_never_auto_suppress: bool


@dataclass(frozen=True)
class OperationsPolicy:
    schema_version: int
    policy_id: str
    policy_version: int
    mode: str
    operator: OperatorPolicy
    sources: SourcePolicy
    privacy: PrivacyPolicy
    budgets: BudgetPolicy
    responsibility: ResponsibilityPolicy

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OperationsPolicy":
        data = _mapping(raw, "operations policy")
        _reject_secret_fields(data)
        _only_keys(
            data,
            {
                "schema_version",
                "policy_id",
                "policy_version",
                "mode",
                "operator",
                "sources",
                "privacy",
                "budgets",
                "responsibility",
            },
            "operations policy",
        )
        schema_version = _integer(data, "schema_version", minimum=1)
        if schema_version != OPERATIONS_POLICY_SCHEMA_VERSION:
            raise OperationsPolicyError(
                f"unsupported operations policy schema_version: {schema_version}"
            )
        policy_id = _bounded_string(data, "policy_id", maximum=80)
        policy_version = _integer(data, "policy_version", minimum=1)
        mode = _bounded_string(data, "mode", maximum=40)
        if mode != SHADOW_READ_ONLY_MODE:
            raise OperationsPolicyError(
                "operations policy mode must be shadow_read_only for this release"
            )

        operator = _parse_operator(data.get("operator"))
        sources = _parse_sources(data.get("sources"), operator)
        privacy = _parse_privacy(data.get("privacy"))
        budgets = _parse_budgets(data.get("budgets"))
        responsibility = _parse_responsibility(data.get("responsibility"))
        return cls(
            schema_version=schema_version,
            policy_id=policy_id,
            policy_version=policy_version,
            mode=mode,
            operator=operator,
            sources=sources,
            privacy=privacy,
            budgets=budgets,
            responsibility=responsibility,
        )

    @property
    def version_ref(self) -> str:
        return f"{self.policy_id}@{self.policy_version}"


def operations_policy_path(home: str | Path | BrainPaths) -> Path:
    paths = home if isinstance(home, BrainPaths) else BrainPaths.from_value(home)
    return paths.config_local / OPERATIONS_POLICY_FILENAME


def load_operations_policy(
    home_or_path: str | Path | BrainPaths,
    *,
    enforce_private_permissions: bool = True,
) -> OperationsPolicy:
    if isinstance(home_or_path, BrainPaths):
        path = operations_policy_path(home_or_path)
    else:
        candidate = Path(home_or_path).expanduser()
        path = (
            candidate
            if candidate.name == OPERATIONS_POLICY_FILENAME
            else operations_policy_path(candidate)
        )
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_symlink() or not path.is_file():
        raise OperationsPolicyError(
            "operations policy must be a regular, non-symlink file"
        )
    if enforce_private_permissions:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise OperationsPolicyError(
                "operations policy must be owner-only (chmod 600)"
            )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OperationsPolicyError(f"invalid operations policy YAML: {exc}") from exc
    if raw is None:
        raise OperationsPolicyError("operations policy cannot be empty")
    return OperationsPolicy.from_dict(raw)


def _parse_operator(value: Any) -> OperatorPolicy:
    data = _mapping(value, "operator")
    _only_keys(data, {"timezone", "calendar", "gmail"}, "operator")
    timezone = _bounded_string(data, "timezone", maximum=80)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise OperationsPolicyError(f"unknown operator timezone: {timezone}") from exc
    calendar = _parse_identity(data.get("calendar"), "operator.calendar")
    gmail = _parse_identity(data.get("gmail"), "operator.gmail")
    if calendar.account_key == gmail.account_key:
        raise OperationsPolicyError(
            "Calendar and Gmail must use separate account_key values"
        )
    return OperatorPolicy(timezone=timezone, calendar=calendar, gmail=gmail)


def _parse_identity(value: Any, label: str) -> ConnectorIdentity:
    data = _mapping(value, label)
    _only_keys(
        data,
        {"account_key", "email", "provider_subject"},
        label,
        required={"account_key", "email"},
    )
    account_key = _account_key(data, "account_key", label)
    email = _bounded_string(data, "email", maximum=254).lower()
    if not _EMAIL_RE.fullmatch(email):
        raise OperationsPolicyError(f"{label}.email must be a valid email address")
    provider_subject = _optional_bounded_string(data, "provider_subject", maximum=255)
    return ConnectorIdentity(
        account_key=account_key,
        email=email,
        provider_subject=provider_subject,
    )


def _parse_sources(value: Any, operator: OperatorPolicy) -> SourcePolicy:
    data = _mapping(value, "sources")
    _only_keys(data, {"calendar", "gmail"}, "sources")

    calendar_data = _mapping(data.get("calendar"), "sources.calendar")
    _only_keys(
        calendar_data,
        {"enabled", "account_key", "calendar_id", "ownership", "scope"},
        "sources.calendar",
    )
    calendar = CalendarSourcePolicy(
        enabled=_boolean(calendar_data, "enabled"),
        account_key=_account_key(calendar_data, "account_key", "sources.calendar"),
        calendar_id=_bounded_string(calendar_data, "calendar_id", maximum=255),
        ownership=_bounded_string(calendar_data, "ownership", maximum=20),
        scope=_bounded_string(calendar_data, "scope", maximum=200),
    )
    if calendar.account_key != operator.calendar.account_key:
        raise OperationsPolicyError(
            "sources.calendar.account_key must match operator.calendar.account_key"
        )
    if calendar.calendar_id != "primary" or calendar.ownership != "owned":
        raise OperationsPolicyError(
            "Calendar trial access is limited to the owned primary calendar"
        )
    if calendar.scope != CALENDAR_OWNED_READ_SCOPE:
        raise OperationsPolicyError(
            "Calendar scope must be calendar.events.owned.readonly"
        )

    gmail_data = _mapping(data.get("gmail"), "sources.gmail")
    _only_keys(
        gmail_data,
        {
            "enabled",
            "account_key",
            "scope",
            "content_access_approved",
            "archive",
        },
        "sources.gmail",
        required={"enabled", "account_key", "scope", "content_access_approved"},
    )
    archive = _parse_gmail_archive(gmail_data.get("archive"))
    gmail = GmailSourcePolicy(
        enabled=_boolean(gmail_data, "enabled"),
        account_key=_account_key(gmail_data, "account_key", "sources.gmail"),
        scope=_bounded_string(gmail_data, "scope", maximum=200),
        content_access_approved=_boolean(gmail_data, "content_access_approved"),
        archive=archive,
    )
    if gmail.account_key != operator.gmail.account_key:
        raise OperationsPolicyError(
            "sources.gmail.account_key must match operator.gmail.account_key"
        )
    if gmail.account_key == calendar.account_key:
        raise OperationsPolicyError("Gmail must use a separate connector account")
    if gmail.scope != GMAIL_READ_SCOPE:
        raise OperationsPolicyError("Gmail scope must be gmail.readonly")
    if gmail.enabled and not gmail.content_access_approved:
        raise OperationsPolicyError(
            "enabled Gmail access requires content_access_approved: true"
        )
    if gmail.archive.enabled and not gmail.enabled:
        raise OperationsPolicyError(
            "enabled Gmail history requires sources.gmail.enabled: true"
        )
    if gmail.archive.enabled and not gmail.content_access_approved:
        raise OperationsPolicyError(
            "enabled Gmail history requires content_access_approved: true"
        )
    return SourcePolicy(calendar=calendar, gmail=gmail)


def _parse_gmail_archive(value: Any) -> GmailArchivePolicy:
    # Policies created before the encrypted archive existed remain valid and
    # fail closed. Enabling the archive is always an explicit policy change.
    if value is None:
        return GmailArchivePolicy(
            enabled=False,
            initial_days=90,
            agent_access_approved=False,
        )
    data = _mapping(value, "sources.gmail.archive")
    _only_keys(
        data,
        {
            "enabled",
            "initial_days",
            "agent_access_approved",
        },
        "sources.gmail.archive",
    )
    archive = GmailArchivePolicy(
        enabled=_boolean(data, "enabled"),
        initial_days=_integer(data, "initial_days", minimum=1),
        agent_access_approved=_boolean(data, "agent_access_approved"),
    )
    if archive.initial_days != 90:
        raise OperationsPolicyError(
            "sources.gmail.archive.initial_days must be 90 for this release"
        )
    if archive.enabled and not archive.agent_access_approved:
        raise OperationsPolicyError(
            "enabled Gmail archive requires agent_access_approved: true"
        )
    return archive


def _parse_privacy(value: Any) -> PrivacyPolicy:
    data = _mapping(value, "privacy")
    _only_keys(
        data,
        {
            "raw_cache_days",
            "normalized_evidence_days",
            "fetch_attachments",
            "strip_quoted_history",
            "external_writes",
        },
        "privacy",
    )
    privacy = PrivacyPolicy(
        raw_cache_days=_integer(data, "raw_cache_days", minimum=0),
        normalized_evidence_days=_integer(data, "normalized_evidence_days", minimum=1),
        fetch_attachments=_boolean(data, "fetch_attachments"),
        strip_quoted_history=_boolean(data, "strip_quoted_history"),
        external_writes=_boolean(data, "external_writes"),
    )
    if privacy.raw_cache_days != 7:
        raise OperationsPolicyError("privacy.raw_cache_days must be 7")
    if privacy.normalized_evidence_days != 30:
        raise OperationsPolicyError("privacy.normalized_evidence_days must be 30")
    if privacy.fetch_attachments:
        raise OperationsPolicyError("attachment fetching must remain disabled")
    if not privacy.strip_quoted_history:
        raise OperationsPolicyError(
            "quoted reply history stripping must remain enabled"
        )
    if privacy.external_writes:
        raise OperationsPolicyError("external writes are forbidden in shadow_read_only")
    return privacy


def _parse_budgets(value: Any) -> BudgetPolicy:
    data = _mapping(value, "budgets")
    _only_keys(data, {"calendar", "gmail"}, "budgets")
    calendar_data = _mapping(data.get("calendar"), "budgets.calendar")
    _only_keys(calendar_data, {"requests_per_day"}, "budgets.calendar")
    gmail_data = _mapping(data.get("gmail"), "budgets.gmail")
    _only_keys(
        gmail_data,
        {
            "api_requests_per_day",
            "detector_calls_per_day",
            "detector_input_tokens_per_day",
            "detector_total_tokens_per_day",
        },
        "budgets.gmail",
    )
    return BudgetPolicy(
        calendar=CalendarBudget(
            requests_per_day=_integer(calendar_data, "requests_per_day", minimum=1)
        ),
        gmail=GmailBudget(
            api_requests_per_day=_integer(
                gmail_data, "api_requests_per_day", minimum=1
            ),
            detector_calls_per_day=_integer(
                gmail_data, "detector_calls_per_day", minimum=1
            ),
            detector_input_tokens_per_day=_integer(
                gmail_data, "detector_input_tokens_per_day", minimum=1
            ),
            detector_total_tokens_per_day=_integer(
                gmail_data, "detector_total_tokens_per_day", minimum=1
            ),
        ),
    )


def _parse_responsibility(value: Any) -> ResponsibilityPolicy:
    data = _mapping(value, "responsibility")
    _only_keys(
        data,
        {
            "owned",
            "shared",
            "adjacent",
            "out_of_area_action",
            "unknown_action",
            "direct_obligations_remain_eligible",
            "high_consequence",
        },
        "responsibility",
    )
    owned = _string_list(data.get("owned"), "responsibility.owned")
    shared = _string_list(data.get("shared"), "responsibility.shared")
    adjacent = _string_list(data.get("adjacent"), "responsibility.adjacent")
    categories_by_area = {
        "owned": set(owned),
        "shared": set(shared),
        "adjacent": set(adjacent),
    }
    for first, second in (
        ("owned", "shared"),
        ("owned", "adjacent"),
        ("shared", "adjacent"),
    ):
        overlap = categories_by_area[first].intersection(categories_by_area[second])
        if overlap:
            joined = ", ".join(sorted(overlap))
            raise OperationsPolicyError(
                f"responsibility areas must be disjoint; {first}/{second}: {joined}"
            )
    out_of_area_action = _bounded_string(data, "out_of_area_action", maximum=30)
    if out_of_area_action != "demote":
        raise OperationsPolicyError("out_of_area_action must be demote")
    unknown_action = _bounded_string(data, "unknown_action", maximum=30)
    if unknown_action != "surface_unknown":
        raise OperationsPolicyError("unknown_action must be surface_unknown")
    direct_eligible = _boolean(data, "direct_obligations_remain_eligible")
    if not direct_eligible:
        raise OperationsPolicyError(
            "direct obligations must remain eligible outside owned areas"
        )

    high_data = _mapping(
        data.get("high_consequence"), "responsibility.high_consequence"
    )
    _only_keys(
        high_data,
        {"categories", "remain_eligible", "never_auto_suppress"},
        "responsibility.high_consequence",
    )
    categories = _string_list(
        high_data.get("categories"), "responsibility.high_consequence.categories"
    )
    missing = REQUIRED_HIGH_CONSEQUENCE_CATEGORIES.difference(categories)
    if missing:
        raise OperationsPolicyError(
            "high_consequence.categories is missing: " + ", ".join(sorted(missing))
        )
    remain_eligible = _boolean(high_data, "remain_eligible")
    never_auto_suppress = _boolean(high_data, "never_auto_suppress")
    if not remain_eligible or not never_auto_suppress:
        raise OperationsPolicyError(
            "high-consequence work must remain eligible and never auto-suppress"
        )
    return ResponsibilityPolicy(
        owned=owned,
        shared=shared,
        adjacent=adjacent,
        out_of_area_action=out_of_area_action,
        unknown_action=unknown_action,
        direct_obligations_remain_eligible=direct_eligible,
        high_consequence_categories=categories,
        high_consequence_remains_eligible=remain_eligible,
        high_consequence_never_auto_suppress=never_auto_suppress,
    )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationsPolicyError(f"{label} must be a mapping")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise OperationsPolicyError(f"{label} keys must be strings")
        result[key] = item
    return result


def _only_keys(
    data: Mapping[str, Any],
    allowed: set[str],
    label: str,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(data).difference(allowed))
    if unknown:
        raise OperationsPolicyError(f"unknown {label} field(s): {', '.join(unknown)}")
    missing = sorted((required if required is not None else allowed).difference(data))
    if missing:
        raise OperationsPolicyError(f"missing {label} field(s): {', '.join(missing)}")


def _reject_secret_fields(value: Any, path: str = "operations policy") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEY_PARTS or any(
                normalized.endswith(f"_{part}") for part in _SECRET_KEY_PARTS
            ):
                raise OperationsPolicyError(
                    f"{path}.{key} is credential-like and forbidden; use Keychain"
                )
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def _bounded_string(data: Mapping[str, Any], key: str, *, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationsPolicyError(f"{key} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise OperationsPolicyError(f"{key} exceeds {maximum} characters")
    return result


def _optional_bounded_string(
    data: Mapping[str, Any], key: str, *, maximum: int
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OperationsPolicyError(f"{key} must be null or a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise OperationsPolicyError(f"{key} exceeds {maximum} characters")
    return result


def _integer(data: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationsPolicyError(f"{key} must be an integer")
    if value < minimum:
        raise OperationsPolicyError(f"{key} must be at least {minimum}")
    return value


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise OperationsPolicyError(f"{key} must be true or false")
    return value


def _account_key(data: Mapping[str, Any], key: str, label: str) -> str:
    value = _bounded_string(data, key, maximum=64)
    if not _ACCOUNT_KEY_RE.fullmatch(value):
        raise OperationsPolicyError(
            f"{label}.{key} must use lowercase letters, digits, dot, dash, or underscore"
        )
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise OperationsPolicyError(f"{label} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise OperationsPolicyError(f"{label} entries must be non-empty strings")
        normalized = item.strip()
        if len(normalized) > 120:
            raise OperationsPolicyError(f"{label} entries exceed 120 characters")
        if normalized in result:
            raise OperationsPolicyError(f"{label} contains duplicate: {normalized}")
        result.append(normalized)
    return tuple(result)
