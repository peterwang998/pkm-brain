#!/usr/bin/env python3
"""Freeze a public-only Gmail temporal V3 challenge without running a model.

The input is an owner-only canonical JSON fixture containing synthetic sources
and semantic gold.  The freezer ingests those sources into a fresh isolated
Brain home, verifies that every gold member is represented in the deterministic
production frontier, and writes only hash-bound owner-only challenge artifacts.
It never invokes a model and never prints fixture content.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

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
from pkm_brain import gmail_temporal_review as production_review
from pkm_brain.gmail_temporal_runner import prepare_gmail_temporal_review
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.util import slugify


VERSION = "gmail_temporal_public_challenge_freezer_v1"
FIXTURE_VERSION = "gmail_temporal_public_challenge_fixture_v3"
ACCOUNT_DOMAIN = "public.example.test"
MAX_CASES = 32
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 20_000

_ROOT = Path(__file__).resolve().parents[1]
_CHALLENGE_RUNNER_PATH = _ROOT / "scripts" / "run_gmail_temporal_public_challenge.py"
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_PUBLIC_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9-]+\.)*example\.test$",
    re.IGNORECASE,
)
_SUBJECT_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_FIXTURE_KEYS = {
    "version",
    "challenge_id",
    "created_at",
    "message_internal_at",
    "account_email",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
}
_CASE_KEYS = {
    "case_id",
    "sender",
    "subject",
    "body",
    "label_ids",
    "members",
    "forbidden",
    "complete_group_required",
}
_ALLOWED_LABEL_IDS = {
    "CATEGORY_PERSONAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "IMPORTANT",
    "INBOX",
    "SENT",
    "STARRED",
}


class PublicChallengeFreezerError(ValueError):
    """Raised without reflecting source or gold content."""


def _load_challenge_contract() -> ModuleType:
    name = "_gmail_temporal_public_challenge_freezer_contract"
    spec = importlib.util.spec_from_file_location(name, _CHALLENGE_RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise PublicChallengeFreezerError("public challenge contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise PublicChallengeFreezerError(
            "public challenge contract could not be loaded"
        ) from exc
    return module


challenge = _load_challenge_contract()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PublicChallengeFreezerError("fixture is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _public_email(value: Any) -> bool:
    return isinstance(value, str) and _PUBLIC_EMAIL_RE.fullmatch(value) is not None


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        raw = challenge._private_file(path)  # noqa: SLF001
        value = challenge._strict_json(raw, label="public fixture")  # noqa: SLF001
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError("public fixture is invalid") from exc
    if set(value) != _FIXTURE_KEYS:
        raise PublicChallengeFreezerError("public fixture schema is invalid")
    created_at = challenge._aware_timestamp(value.get("created_at"))  # noqa: SLF001
    message_internal_at = challenge._aware_timestamp(  # noqa: SLF001
        value.get("message_internal_at")
    )
    if (
        value.get("version") != FIXTURE_VERSION
        or value.get("public_synthetic") is not True
        or value.get("contains_private_gmail") is not False
        or value.get("release_eligible") is not False
        or challenge._CHALLENGE_ID_RE.fullmatch(  # noqa: SLF001
            str(value.get("challenge_id") or "")
        )
        is None
        or created_at is None
        or message_internal_at is None
        or message_internal_at > created_at
        or not _public_email(value.get("account_email"))
    ):
        raise PublicChallengeFreezerError("public fixture authority is invalid")

    cases = value.get("cases")
    if not isinstance(cases, list) or not 2 <= len(cases) <= MAX_CASES:
        raise PublicChallengeFreezerError("public fixture case count is invalid")
    seen: set[str] = set()
    for row in cases:
        if not isinstance(row, Mapping) or set(row) != _CASE_KEYS:
            raise PublicChallengeFreezerError("public fixture case schema is invalid")
        case_id = row.get("case_id")
        subject = row.get("subject")
        body = row.get("body")
        labels = row.get("label_ids")
        members = row.get("members")
        forbidden = row.get("forbidden")
        if (
            not isinstance(case_id, str)
            or _CASE_ID_RE.fullmatch(case_id) is None
            or case_id in seen
            or not _public_email(row.get("sender"))
            or not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > MAX_SUBJECT_CHARS
            or "\x00" in subject
            or not isinstance(body, str)
            or not body.strip()
            or len(body) > MAX_BODY_CHARS
            or "\x00" in body
            or not isinstance(labels, list)
            or not labels
            or len(labels) != len(set(labels))
            or any(label not in _ALLOWED_LABEL_IDS for label in labels)
            or not isinstance(members, list)
            or not isinstance(forbidden, list)
            or any(not isinstance(item, Mapping) for item in forbidden)
            or not isinstance(row.get("complete_group_required"), bool)
        ):
            raise PublicChallengeFreezerError("public fixture case is invalid")
        seen.add(case_id)

    gold = _gold_value(value)
    try:
        challenge._validate_gold(gold)  # noqa: SLF001
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError("public fixture gold is invalid") from exc
    verdicts = [
        member.get("expected_verdict")
        for row in gold["cases"]
        for member in row["members"]
        if isinstance(member, Mapping)
    ]
    if "supported" not in verdicts or "uncertain" not in verdicts:
        raise PublicChallengeFreezerError(
            "public fixture must calibrate supported and uncertain members"
        )
    if not any(
        member.get("canonical_subject_required") is True
        for row in gold["cases"]
        for member in row["members"]
        if isinstance(member, Mapping)
    ):
        raise PublicChallengeFreezerError(
            "public fixture requires a canonical named-event member"
        )
    if not any(row["forbidden"] for row in gold["cases"]):
        raise PublicChallengeFreezerError(
            "public fixture requires structured forbidden bindings"
        )
    return dict(value)


def _gold_value(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": challenge.GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {
                "case_id": str(row["case_id"]),
                "members": list(row["members"]),
                "forbidden": [dict(item) for item in row["forbidden"]],
                "complete_group_required": bool(row["complete_group_required"]),
            }
            for row in fixture["cases"]
        ],
    }


def _utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC).isoformat()


def _ingest_case(
    paths: BrainPaths,
    *,
    account_email: str,
    message_internal_at: str,
    row: Mapping[str, Any],
) -> tuple[str, str]:
    case_id = str(row["case_id"])
    thread_id = f"public-v3-thread-{case_id}"
    message_id = f"public-v3-message-{case_id}"
    revision = _sha256(
        _canonical_json(
            {
                "account_email": account_email,
                "message_internal_at": message_internal_at,
                "case_id": case_id,
                "sender": row["sender"],
                "subject": row["subject"],
                "body": row["body"],
                "label_ids": row["label_ids"],
            }
        )
    )
    labels = tuple(str(item) for item in row["label_ids"])
    promotion = "CATEGORY_PROMOTIONS" in labels
    message = ArchiveOpenedMessage(
        message_id=message_id,
        thread_id=thread_id,
        internal_date=message_internal_at,
        date_header="",
        subject=str(row["subject"]),
        from_addresses=(str(row["sender"]),),
        to_addresses=(account_email,),
        cc_addresses=(),
        label_ids=labels,
        list_id=(f"offers.{ACCOUNT_DOMAIN}" if promotion else None),
        list_unsubscribe=(
            f"<mailto:unsubscribe@{ACCOUNT_DOMAIN}>" if promotion else None
        ),
        precedence=None,
        auto_submitted=None,
        body_text=str(row["body"]),
        attachments=(),
        account_key=account_email,
    )
    body_size = len(str(row["body"]).encode("utf-8"))
    snapshot = ArchiveThreadSnapshot(
        thread_id=thread_id,
        source_revision=revision,
        total_message_count=1,
        visible_message_count=1,
        deleted_message_count=0,
        hidden_message_count=0,
        created_at=message_internal_at,
        updated_at=message_internal_at,
        archive_updated_at=_utc_timestamp(message_internal_at),
        raw_size=body_size,
        account_key=account_email,
    )
    normalized = normalize_gmail_thread(
        snapshot,
        ArchiveThreadResult(
            thread_id=thread_id,
            total_messages=1,
            messages=(message,),
            truncated=False,
            account_key=account_email,
        ),
        operator_email=account_email,
    )
    session_id = gmail_projection_session_id(
        account_key=account_email,
        thread_id=thread_id,
        source_revision=revision,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    source = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    try:
        challenge._write_private_new(  # noqa: SLF001
            source, normalized.markdown.encode("utf-8")
        )
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError("public source could not be frozen") from exc
    ingestion = BrainService(paths).ingest(source=source)
    if ingestion.errors or ingestion.changed != 1:
        raise PublicChallengeFreezerError("public source ingestion failed")
    with connection(paths.sqlite_path) as conn:
        result = conn.execute(
            "SELECT id FROM documents WHERE source_path = ? AND status = 'active'",
            (str(source.resolve()),),
        ).fetchone()
    if result is None:
        raise PublicChallengeFreezerError("public source authority is unavailable")
    return str(result["id"]), message_id


def _member_values(member: Mapping[str, Any]) -> Sequence[str]:
    values = member.get("values")
    if isinstance(values, list):
        return tuple(str(item) for item in values)
    return (str(member["value"]),)


def _normalized_subject(value: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _SUBJECT_TOKEN_RE.findall(value))


def _subject_surfaces(authority: Any) -> dict[str, str]:
    text = authority.source.text
    surfaces: dict[str, str] = {}
    for mention in authority.analysis.mentions:
        if mention.start < 0 or mention.end <= mention.start or mention.end > len(text):
            raise PublicChallengeFreezerError("production subject authority is invalid")
        surfaces[mention.mention_id] = text[mention.start : mention.end]
    return surfaces


def _subject_family_ids(authority: Any) -> dict[str, str]:
    """Use the production review's source-verified alias authority directly."""

    candidates = tuple(
        candidate
        for batch in authority.batches
        for candidate in batch.frontier_candidates
    )
    try:
        return production_review._subject_alias_families(  # noqa: SLF001
            analysis=authority.analysis,
            batches=authority.batch_plan.batches,
            candidates=candidates,
        )
    except production_review.GmailTemporalReviewError as exc:
        raise PublicChallengeFreezerError(
            "production subject authority is invalid"
        ) from exc


def _member_has_frontier_authority(member: Mapping[str, Any], authority: Any) -> bool:
    subject_surfaces = _subject_surfaces(authority)
    family_ids = _subject_family_ids(authority)
    family_members = production_review._subject_alias_family_members(  # noqa: SLF001
        family_ids
    )
    subject_types = {
        mention.mention_id: mention.mention_type
        for mention in authority.analysis.mentions
    }
    family_surfaces: dict[str, set[str]] = {}
    for mention_id, family_id in family_ids.items():
        surface = subject_surfaces.get(mention_id)
        if surface is not None:
            family_surfaces.setdefault(family_id, set()).add(surface)
    candidates = tuple(
        candidate
        for batch in authority.batches
        for candidate in batch.frontier_candidates
    )
    expected_subject = str(member["subject"])
    for expected_value in _member_values(member):
        matched = False
        for candidate in candidates:
            if (
                candidate.relation != member["relation"]
                or candidate.lifecycle != member["lifecycle"]
                or candidate.normalized_value != expected_value
            ):
                continue
            surfaces = {subject_surfaces.get(candidate.subject_mention_id, "")}
            family_id = family_ids.get(candidate.subject_mention_id)
            if family_id is not None:
                surfaces.update(family_surfaces.get(family_id, ()))
            if any(
                surface
                and _normalized_subject(expected_subject)
                == _normalized_subject(surface)
                for surface in surfaces
            ):
                if member.get("canonical_subject_required") is True:
                    try:
                        _, _, canonical_id = (
                            production_review._subject_identity_metadata(  # noqa: SLF001
                                subject_mention_ids=(candidate.subject_mention_id,),
                                subject_types_by_id=subject_types,
                                subject_families=family_ids,
                                subject_family_members=family_members,
                            )
                        )
                    except production_review.GmailTemporalReviewError as exc:
                        raise PublicChallengeFreezerError(
                            "production canonical subject authority is invalid"
                        ) from exc
                    if canonical_id is None or _normalized_subject(
                        subject_surfaces.get(canonical_id, "")
                    ) != _normalized_subject(expected_subject):
                        continue
                matched = True
                break
        if not matched:
            return False
    return True


def _canonical_fresh_root(path: Path) -> Path:
    raw = path.expanduser().absolute()
    if raw.is_symlink():
        raise PublicChallengeFreezerError("fresh public root is invalid")
    return raw.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def freeze_public_challenge(
    fixture_path: Path,
    hmac_key_path: Path,
    brain_home: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Materialize one fresh public-only challenge without opening a model path."""

    fixture = _load_fixture(fixture_path)
    try:
        key = challenge._key(hmac_key_path)  # noqa: SLF001
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError("public challenge key is invalid") from exc
    resolved_home = _canonical_fresh_root(brain_home)
    resolved_output = _canonical_fresh_root(output_root)
    private_homes = challenge._known_private_brain_homes()  # noqa: SLF001
    if any(
        _is_within(resolved_home, private_home)
        or _is_within(resolved_output, private_home)
        for private_home in private_homes
    ):
        raise PublicChallengeFreezerError("private Brain home is forbidden")
    if _is_within(resolved_home, resolved_output) or _is_within(
        resolved_output, resolved_home
    ):
        raise PublicChallengeFreezerError("public roots must be disjoint")
    try:
        challenge._fresh_private_directory(resolved_home)  # noqa: SLF001
        challenge._fresh_private_directory(resolved_output)  # noqa: SLF001
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError(
            "fresh public output authority is required"
        ) from exc

    paths = BrainPaths.from_value(resolved_home)
    try:
        challenge._private_directory(  # noqa: SLF001
            paths.config_local,
            create=True,
        )
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError(
            "fresh public Brain authority is invalid"
        ) from exc
    BrainService(paths).init_workspace()
    challenge_rows: list[dict[str, str]] = []
    candidate_cases = 0
    total_candidates = 0
    import pkm_brain.gmail_temporal_runner as production_runner

    for row in fixture["cases"]:
        document_id, message_id = _ingest_case(
            paths,
            account_email=str(fixture["account_email"]),
            message_internal_at=str(fixture["message_internal_at"]),
            row=row,
        )
        preparation = prepare_gmail_temporal_review(
            paths,
            document_id=document_id,
            gmail_message_id=message_id,
        )
        authority = production_runner._build_authority(  # noqa: SLF001
            paths,
            document_id=document_id,
            gmail_message_id=message_id,
        )
        members = row["members"]
        if members and not preparation.requests:
            raise PublicChallengeFreezerError(
                "positive public case has no verifier authority"
            )
        if any(
            not _member_has_frontier_authority(member, authority) for member in members
        ):
            raise PublicChallengeFreezerError(
                "public gold member is absent from the production frontier"
            )
        candidate_cases += int(bool(preparation.requests))
        total_candidates += preparation.candidate_count
        challenge_rows.append(
            {
                "case_id": str(row["case_id"]),
                "document_id": document_id,
                "gmail_message_id": message_id,
                "source_sha256": preparation.source_sha256,
            }
        )

    gold = _gold_value(fixture)
    gold_raw = _canonical_json(gold) + b"\n"
    manifest = {
        "version": challenge.CHALLENGE_VERSION,
        "challenge_id": fixture["challenge_id"],
        "scope": challenge.PUBLIC_SCOPE,
        "created_at": fixture["created_at"],
        "brain_home": str(paths.home.resolve()),
        "gold_sha256": _sha256(gold_raw),
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "cases": challenge_rows,
    }
    manifest_raw = _canonical_json(manifest) + b"\n"
    marker = challenge._signed(  # noqa: SLF001
        {
            "version": challenge.PUBLIC_ROOT_AUTHORITY_VERSION,
            "challenge_id": fixture["challenge_id"],
            "scope": challenge.PUBLIC_SCOPE,
            "created_at": fixture["created_at"],
            "brain_home": str(paths.home.resolve()),
            "challenge_manifest_sha256": _sha256(manifest_raw),
            "gold_sha256": _sha256(gold_raw),
            "public_synthetic": True,
            "contains_private_gmail": False,
            "release_eligible": False,
            "cases": challenge_rows,
        },
        key=key,
        domain=challenge.PUBLIC_ROOT_AUTHORITY_DOMAIN,
        signature_field="authority_hmac_sha256",
    )
    try:
        challenge._write_private_new(  # noqa: SLF001
            paths.config_local / challenge.PUBLIC_ROOT_AUTHORITY_FILENAME,
            _canonical_json(marker) + b"\n",
        )
        challenge._write_private_new(  # noqa: SLF001
            resolved_output / "gold.json", gold_raw
        )
        challenge._write_private_new(  # noqa: SLF001
            resolved_output / "challenge.json", manifest_raw
        )
    except challenge.PublicChallengeError as exc:
        raise PublicChallengeFreezerError(
            "public challenge artifacts could not be frozen"
        ) from exc

    members = [member for row in gold["cases"] for member in row["members"]]
    return {
        "version": VERSION,
        "status": "complete",
        "cases": len(challenge_rows),
        "positive_cases": sum(bool(row["members"]) for row in gold["cases"]),
        "negative_cases": sum(not row["members"] for row in gold["cases"]),
        "gold_members": len(members),
        "supported_gold_members": sum(
            member["expected_verdict"] == "supported" for member in members
        ),
        "uncertain_gold_members": sum(
            member["expected_verdict"] == "uncertain" for member in members
        ),
        "canonical_subject_members": sum(
            member.get("canonical_subject_required") is True for member in members
        ),
        "structured_forbidden_bindings": sum(
            len(row["forbidden"]) for row in gold["cases"]
        ),
        "candidate_cases": candidate_cases,
        "zero_work_cases": len(challenge_rows) - candidate_cases,
        "candidates": total_candidates,
        "challenge_sha256": _sha256(manifest_raw),
        "gold_sha256": _sha256(gold_raw),
        "external_calls": 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "private_content_printed": False,
    }


def _safe_failure() -> dict[str, Any]:
    return {
        "version": VERSION,
        "status": "failed",
        "error": "public_temporal_challenge_freeze_failed",
        "external_calls": 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--brain-home", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = freeze_public_challenge(
            args.fixture,
            args.hmac_key,
            args.brain_home,
            args.output_root,
        )
    except Exception:  # noqa: BLE001 - this CLI is a no-source-output boundary.
        print(json.dumps(_safe_failure(), sort_keys=True, separators=(",", ":")))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
