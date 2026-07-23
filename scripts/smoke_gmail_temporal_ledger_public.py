#!/usr/bin/env python3
"""Run a public synthetic smoke over the Gmail temporal review ledger.

This smoke uses a dedicated, explicitly marked temporary root.  It makes no
Gmail, network, provider, or model calls and never uses the production temporal
pipeline scope.  ``run`` seeds one review projection and launches ``resume`` in
a fresh Python process.  The resume phase proves exact replay, compare-and-swap
head rollback and clear, stale-source rejection, and coordinated database-pair
recovery into a quarantined isolated home.

The JSON report is aggregate-only.  It deliberately makes no semantic recall,
precision, external-invocation independence, freshness, or production-release
claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Mapping, Sequence

from pkm_brain.db import connection, init_db
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.gmail_temporal_frontier import (
    gmail_temporal_candidate_ensemble_policy_fingerprint,
)
from pkm_brain.gmail_temporal_persistence import (
    GmailTemporalHeadConflict,
    GmailTemporalPersistenceError,
    GmailTemporalSourceLocator,
    clear_gmail_temporal_review_head_for_source,
    get_gmail_temporal_review_head,
    gmail_temporal_message_scope_key,
    persist_gmail_temporal_review_projection,
    rollback_gmail_temporal_review_head,
)
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewArtifact,
    GmailTemporalReviewGroup,
    GmailTemporalReviewGroupMember,
    GmailTemporalReviewHypothesis,
    GmailTemporalReviewProjection,
    gmail_temporal_review_grouping_policy_fingerprint,
)
from pkm_brain.operational_service import OperationalService
from pkm_brain.paths import BrainPaths
from pkm_brain.recovery import (
    create_coordinated_recovery_set,
    restore_recovery_set_isolated,
    verify_recovery_set,
)
from pkm_brain.util import slugify


REPORT_VERSION = "gmail_temporal_ledger_public_smoke_v1"
MARKER_VERSION = "gmail_temporal_ledger_public_smoke_root_v2"
PIPELINE_SCOPE = "gmail-temporal-review/public-ledger-smoke-v1"
MARKER_NAME = "PUBLIC-SYNTHETIC-TEMPORAL-LEDGER-SMOKE.json"
HOME_MARKER_NAME = "PUBLIC-SYNTHETIC-TEMPORAL-LEDGER-HOME.json"

ACCOUNT_KEY = "public-owner@example.test"
THREAD_ID = "public-temporal-ledger-thread"
SOURCE_REVISION = "e" * 64
MESSAGE_ID = "public-temporal-message-1"
MESSAGE_INTERNAL_AT = "2027-07-22T12:00:00-07:00"
DOCUMENT_ID = "public-temporal-document-1"
DOCUMENT_TITLE = "Public temporal ledger smoke"
EVIDENCE_TEXT = "The public Project Atlas interview is scheduled for August 14, 2027."
THREAD_HEADING = "# Email thread: Public temporal ledger smoke"
MESSAGE_BLOCK = (
    f"## Message 1 — {MESSAGE_INTERNAL_AT} — {MESSAGE_ID}\n\n"
    "From: public-sender@example.test\n"
    f"To: {ACCOUNT_KEY}\n"
    "Direction: incoming (public synthetic)\n\n"
    f"{EVIDENCE_TEXT}"
)
MESSAGE_START = len(THREAD_HEADING) + 2
MESSAGE_END = MESSAGE_START + len(MESSAGE_BLOCK)
BODY = f"{THREAD_HEADING}\n\n{MESSAGE_BLOCK}"
MESSAGE_TIMESTAMPS = (
    {
        "message_id": MESSAGE_ID,
        "internal_date": MESSAGE_INTERNAL_AT,
        "start_offset": MESSAGE_START,
        "end_offset": MESSAGE_END,
    },
)
MARKDOWN = "\n".join(
    (
        "---",
        f"title: {DOCUMENT_TITLE}",
        "source_type: gmail_thread",
        f"gmail_account_key: {ACCOUNT_KEY}",
        f"gmail_thread_id: {THREAD_ID}",
        f"gmail_source_revision: {SOURCE_REVISION}",
        f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
        f"gmail_message_ids: {json.dumps([MESSAGE_ID])}",
        "gmail_message_timestamps_version: 1",
        f"gmail_message_timestamps: {json.dumps(MESSAGE_TIMESTAMPS)}",
        "retained_message_count: 1",
        "---",
        BODY,
        "",
    )
)
DOCUMENT_HASH = hashlib.sha256(MARKDOWN.encode("utf-8")).hexdigest()
SOURCE_HASH = hashlib.sha256(EVIDENCE_TEXT.encode("utf-8")).hexdigest()

TEMPORAL_TABLES = (
    "gmail_temporal_review_runs",
    "gmail_temporal_review_artifacts",
    "gmail_temporal_review_heads",
    "gmail_temporal_review_executions",
    "gmail_temporal_review_components",
)


class PublicTemporalLedgerSmokeError(RuntimeError):
    """Raised when the isolated public smoke contract is not satisfied."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _safety_contract() -> dict[str, object]:
    return {
        "classification": "public_synthetic",
        "gmail_provider_calls": 0,
        "network_calls": 0,
        "external_model_calls": 0,
        "private_data_accessed": False,
        "production_home_accessed": False,
        "production_pipeline_scope_used": False,
        "independent_invocations_verified": False,
        "semantic_metrics_evaluated": False,
        "release_claim": False,
    }


def _private_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _marker_path(root: Path) -> Path:
    return root / MARKER_NAME


def _home_marker_path(home: Path) -> Path:
    return home / HOME_MARKER_NAME


def _validate_root_separation(root: Path) -> None:
    production_home = BrainPaths.from_value().home
    if (
        root == production_home
        or root.is_relative_to(production_home)
        or production_home.is_relative_to(root)
    ):
        raise PublicTemporalLedgerSmokeError(
            "smoke root overlaps the configured production Brain home"
        )


def _validate_new_root(root: Path) -> Path:
    candidate = root.expanduser()
    if candidate.exists() or candidate.is_symlink():
        raise PublicTemporalLedgerSmokeError("smoke root already exists")
    resolved = candidate.resolve()
    _validate_root_separation(resolved)
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    _private_mode(resolved, 0o700)
    return resolved


def _validate_marked_root(root: Path) -> tuple[Path, str]:
    candidate = root.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise PublicTemporalLedgerSmokeError("smoke root is missing or unsafe")
    resolved = candidate.resolve()
    _validate_root_separation(resolved)
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise PublicTemporalLedgerSmokeError("smoke root is not owner-only")
    if resolved.stat().st_uid != os.getuid():
        raise PublicTemporalLedgerSmokeError("smoke root is not owned by this user")
    marker = _marker_path(resolved)
    if marker.is_symlink() or not marker.is_file():
        raise PublicTemporalLedgerSmokeError("public smoke marker is missing")
    if stat.S_IMODE(marker.stat().st_mode) & 0o077:
        raise PublicTemporalLedgerSmokeError("public smoke marker is not owner-only")
    if marker.stat().st_uid != os.getuid() or marker.stat().st_nlink != 1:
        raise PublicTemporalLedgerSmokeError("public smoke marker is unsafe")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicTemporalLedgerSmokeError("public smoke marker is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "classification",
        "root_token",
        "version",
    }:
        raise PublicTemporalLedgerSmokeError("public smoke marker is invalid")
    root_token = value.get("root_token")
    if (
        value.get("classification") != "public_synthetic"
        or value.get("version") != MARKER_VERSION
        or not isinstance(root_token, str)
        or len(root_token) != 64
        or any(character not in "0123456789abcdef" for character in root_token)
    ):
        raise PublicTemporalLedgerSmokeError("public smoke marker is invalid")
    return resolved, root_token


def _write_marker(root: Path) -> str:
    root_token = secrets.token_hex(32)
    marker = _marker_path(root)
    marker.write_bytes(
        _canonical_bytes(
            {
                "classification": "public_synthetic",
                "root_token": root_token,
                "version": MARKER_VERSION,
            }
        )
        + b"\n"
    )
    _private_mode(marker, 0o600)
    return root_token


def _write_home_marker(home: Path, root_token: str) -> None:
    marker = _home_marker_path(home)
    marker.write_bytes(
        _canonical_bytes(
            {
                "classification": "public_synthetic",
                "root_token": root_token,
                "version": MARKER_VERSION,
            }
        )
        + b"\n"
    )
    _private_mode(marker, 0o600)


def _validate_smoke_tree(root: Path, root_token: str) -> BrainPaths:
    candidate = root / "home"
    if candidate.is_symlink() or not candidate.is_dir():
        raise PublicTemporalLedgerSmokeError("public smoke home is missing or unsafe")
    home = candidate.resolve()
    if home != candidate or home.parent != root:
        raise PublicTemporalLedgerSmokeError("public smoke home escaped its root")
    if home.stat().st_uid != os.getuid():
        raise PublicTemporalLedgerSmokeError("public smoke home has an unsafe owner")
    for current, directories, filenames in os.walk(home, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *filenames):
            entry = current_path / name
            if entry.is_symlink():
                raise PublicTemporalLedgerSmokeError(
                    "public smoke home contains a redirected path"
                )
            metadata = entry.stat(follow_symlinks=False)
            if metadata.st_uid != os.getuid():
                raise PublicTemporalLedgerSmokeError(
                    "public smoke home contains an unsafe owner"
                )
            if name in filenames:
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise PublicTemporalLedgerSmokeError(
                        "public smoke home contains an unsafe file"
                    )
            elif not stat.S_ISDIR(metadata.st_mode):
                raise PublicTemporalLedgerSmokeError(
                    "public smoke home contains an unsafe directory"
                )
    marker = _home_marker_path(home)
    if not marker.is_file():
        raise PublicTemporalLedgerSmokeError("public smoke home marker is missing")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicTemporalLedgerSmokeError(
            "public smoke home marker is invalid"
        ) from exc
    if value != {
        "classification": "public_synthetic",
        "root_token": root_token,
        "version": MARKER_VERSION,
    }:
        raise PublicTemporalLedgerSmokeError("public smoke home marker is invalid")
    paths = BrainPaths.from_value(home)
    expected_source = _source_path(paths)
    if expected_source.is_symlink() or not expected_source.is_file():
        raise PublicTemporalLedgerSmokeError("public synthetic source is unsafe")
    return paths


def _source_path(paths: BrainPaths) -> Path:
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT_KEY,
        thread_id=THREAD_ID,
        source_revision=SOURCE_REVISION,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    return paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"


def _source_locator() -> GmailTemporalSourceLocator:
    return GmailTemporalSourceLocator(
        document_id=DOCUMENT_ID,
        document_content_hash=DOCUMENT_HASH,
        gmail_account_key=ACCOUNT_KEY,
        gmail_thread_id=THREAD_ID,
        gmail_source_revision=SOURCE_REVISION,
        gmail_message_id=MESSAGE_ID,
        message_internal_at=MESSAGE_INTERNAL_AT,
        message_start_offset=MESSAGE_START,
        message_end_offset=MESSAGE_END,
        source_sha256=SOURCE_HASH,
    )


def _projection_material(
    projection: GmailTemporalReviewProjection,
) -> dict[str, object]:
    value = asdict(projection)
    value.pop("projection_fingerprint")
    return value


def _hypothesis_id(
    *,
    expression_id: str,
    normalized_value: str,
    subject_type_references: tuple[tuple[str, str], ...],
) -> str:
    material = {
        "version": "gmail_temporal_review_hypothesis_v3",
        "signature": (
            expression_id,
            "scheduled_for",
            "planned",
            "scheduled",
            normalized_value,
        ),
        "subject_type_references": subject_type_references,
        "subject_alias_type_references": subject_type_references,
        "canonical_subject_mention_id": None,
    }
    return "gtrh_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _artifact_and_group(
    suffix: str,
) -> tuple[GmailTemporalReviewArtifact, GmailTemporalReviewGroup]:
    expression_id = f"public-expression-{suffix}"
    subject_id = f"public-event-subject-{suffix}"
    candidate_id = f"public-candidate-{suffix}"
    subject_types = ((subject_id, "event"),)
    normalized_value = "2027-08-14T09:00:00-07:00"
    hypothesis = GmailTemporalReviewHypothesis(
        version="gmail_temporal_review_hypothesis_v3",
        hypothesis_id=_hypothesis_id(
            expression_id=expression_id,
            normalized_value=normalized_value,
            subject_type_references=subject_types,
        ),
        expression_id=expression_id,
        subject_mention_ids=(subject_id,),
        subject_type_references=subject_types,
        subject_alias_mention_ids=(subject_id,),
        subject_alias_type_references=subject_types,
        canonical_subject_mention_id=None,
        lifecycle_mention_ids=(f"public-lifecycle-{suffix}",),
        relation="scheduled_for",
        kind="planned",
        lifecycle="scheduled",
        normalized_value=normalized_value,
        candidate_ids=(candidate_id,),
        candidate_requires_defer=False,
    )
    artifact = GmailTemporalReviewArtifact(
        version="gmail_temporal_review_artifact_v3",
        artifact_id=f"supported:{candidate_id}",
        kind="supported_citation",
        evidence_status="supported",
        batch_fingerprint=f"public-batch-{suffix}",
        frontier_fingerprint=f"public-frontier-{suffix}",
        parent_cluster_id=f"public-cluster-{suffix}",
        candidate_ids=(candidate_id,),
        hypotheses=(hypothesis,),
    )
    group_id = f"public-group-{suffix}"
    member_material = {
        "version": "gmail_temporal_review_group_member_v1",
        "group_id": group_id,
        "expression_id": expression_id,
        "role": "independent",
        "source_order": None,
    }
    member = GmailTemporalReviewGroupMember(
        version="gmail_temporal_review_group_member_v1",
        member_id=(
            "gtrgm_" + hashlib.sha256(_canonical_bytes(member_material)).hexdigest()
        ),
        expression_id=expression_id,
        role="independent",
        source_order=None,
        state="present",
        artifact_ids=(artifact.artifact_id,),
        cluster_review_ids=(),
        subject_family_ids=(f"public-family-{suffix}",),
        reasons=(),
    )
    group = GmailTemporalReviewGroup(
        version="gmail_temporal_review_group_v1",
        group_id=group_id,
        kind="single",
        coverage="complete",
        source_start=0,
        source_end=10,
        subject_family_id=f"public-family-{suffix}",
        members=(member,),
        reasons=(),
    )
    return artifact, group


def _projection(suffix: str) -> GmailTemporalReviewProjection:
    components = tuple(
        hashlib.sha256(
            f"public-synthetic-component-{suffix}-{ordinal}".encode("utf-8")
        ).hexdigest()
        for ordinal in range(1, 4)
    )
    artifact, group = _artifact_and_group(suffix)
    provisional = GmailTemporalReviewProjection(
        version="gmail_temporal_review_projection_v3",
        projection_fingerprint="",
        analysis_fingerprint=f"public-analysis-{suffix}",
        source_sha256=SOURCE_HASH,
        batch_plan_fingerprint=f"public-batch-plan-{suffix}",
        ensemble_policy_fingerprint=(
            gmail_temporal_candidate_ensemble_policy_fingerprint()
        ),
        grouping_policy_fingerprint=(
            gmail_temporal_review_grouping_policy_fingerprint()
        ),
        independent_invocations_verified=False,
        component_evidence_fingerprints=components,
        artifacts=(artifact,),
        cluster_reviews=(),
        groups=(group,),
        complete=True,
    )
    return replace(
        provisional,
        projection_fingerprint=(
            "gtrp_"
            + hashlib.sha256(
                _canonical_bytes(_projection_material(provisional))
            ).hexdigest()
        ),
    )


def _initialize_home(root: Path, *, root_token: str) -> BrainPaths:
    paths = BrainPaths.from_value(root / "home")
    init_db(paths.sqlite_path)
    operational = OperationalService(paths, writer_guard=lambda: None)
    operational.initialize()
    source_path = _source_path(paths)
    source_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_mode(source_path.parent, 0o700)
    source_path.write_text(MARKDOWN, encoding="utf-8")
    _private_mode(source_path, 0o600)
    _write_home_marker(paths.home, root_token)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', ?, ?, ?, ?, ?, ?, '[]', 'active')
            """,
            (
                DOCUMENT_ID,
                DOCUMENT_TITLE,
                str(source_path),
                str(paths.raw / "public-synthetic-missing.md"),
                DOCUMENT_HASH,
                "2027-07-22T19:00:00+00:00",
                "2027-07-22T19:01:00+00:00",
            ),
        )
    return paths


def _table_rows(paths: BrainPaths) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = {}
    with connection(paths.sqlite_path) as conn:
        for table in TEMPORAL_TABLES:
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            output[table] = sorted(
                rows,
                key=lambda row: _canonical_bytes(row),
            )
    return output


def _table_counts(
    rows: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, int]:
    return {
        "runs": len(rows["gmail_temporal_review_runs"]),
        "artifacts": len(rows["gmail_temporal_review_artifacts"]),
        "heads": len(rows["gmail_temporal_review_heads"]),
        "executions": len(rows["gmail_temporal_review_executions"]),
        "components": len(rows["gmail_temporal_review_components"]),
    }


def _assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise PublicTemporalLedgerSmokeError(f"{label} did not match")


def initialize_public_smoke(root: Path) -> dict[str, object]:
    resolved = _validate_new_root(root)
    root_token = _write_marker(resolved)
    _initialize_home(resolved, root_token=root_token)
    paths = _validate_smoke_tree(resolved, root_token)
    initial = persist_gmail_temporal_review_projection(
        paths,
        source=_source_locator(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection("a"),
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    rows = _table_rows(paths)
    _assert_equal(initial.replayed, False, "initial replay state")
    _assert_equal(initial.head_generation, 1, "initial head generation")
    _assert_equal(
        _table_counts(rows),
        {
            "runs": 1,
            "artifacts": 1,
            "heads": 1,
            "executions": 0,
            "components": 0,
        },
        "initial temporal row counts",
    )
    return {
        "version": REPORT_VERSION,
        "action": "initialize",
        "status": "passed",
        "checks": {
            "dedicated_root_marked": True,
            "initial_projection_persisted": True,
            "initial_head_generation": 1,
        },
        "counts": _table_counts(rows),
        **_safety_contract(),
    }


def _replay_and_advance(paths: BrainPaths) -> tuple[str, str, int]:
    initial_rows = _table_rows(paths)
    initial_heads = initial_rows["gmail_temporal_review_heads"]
    if len(initial_heads) != 1:
        raise PublicTemporalLedgerSmokeError("initial head is unavailable")
    initial_run_id = str(initial_heads[0]["run_id"])

    replay = persist_gmail_temporal_review_projection(
        paths,
        source=_source_locator(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection("a"),
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    _assert_equal(replay.replayed, True, "fresh-process replay state")
    _assert_equal(replay.head_changed, False, "fresh-process replay head")
    _assert_equal(_table_rows(paths), initial_rows, "exact replay temporal rows")

    second = persist_gmail_temporal_review_projection(
        paths,
        source=_source_locator(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection("b"),
        expected_head_run_id=initial_run_id,
        expected_head_generation=1,
    )
    _assert_equal(second.head_generation, 2, "second head generation")

    before_conflict = _table_rows(paths)
    try:
        persist_gmail_temporal_review_projection(
            paths,
            source=_source_locator(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=_projection("c"),
            expected_head_run_id=initial_run_id,
            expected_head_generation=1,
        )
    except GmailTemporalHeadConflict:
        pass
    else:
        raise PublicTemporalLedgerSmokeError("stale head CAS was accepted")
    _assert_equal(
        _table_rows(paths),
        before_conflict,
        "stale head CAS residue",
    )
    return initial_run_id, second.run_id, second.head_generation


def _rollback_clear_and_stale(
    paths: BrainPaths,
    *,
    initial_run_id: str,
    second_run_id: str,
    second_generation: int,
) -> dict[str, bool | int]:
    message_scope_key = gmail_temporal_message_scope_key(
        gmail_account_key=ACCOUNT_KEY,
        gmail_thread_id=THREAD_ID,
        gmail_message_id=MESSAGE_ID,
    )
    rolled_back = rollback_gmail_temporal_review_head(
        paths,
        message_scope_key=message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
        expected_run_id=second_run_id,
        expected_generation=second_generation,
        restore_run_id=initial_run_id,
    )
    _assert_equal(rolled_back.current_run_id, initial_run_id, "rollback target")
    _assert_equal(rolled_back.head_generation, 3, "rollback generation")
    replayed_rollback = rollback_gmail_temporal_review_head(
        paths,
        message_scope_key=rolled_back.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
        expected_run_id=second_run_id,
        expected_generation=second_generation,
        restore_run_id=initial_run_id,
    )
    _assert_equal(replayed_rollback.changed, False, "idempotent rollback")
    _assert_equal(replayed_rollback.head_generation, 3, "replayed rollback generation")
    cleared = clear_gmail_temporal_review_head_for_source(
        paths,
        source=_source_locator(),
        pipeline_scope=PIPELINE_SCOPE,
        expected_run_id=initial_run_id,
        expected_generation=3,
    )
    _assert_equal(cleared.current_run_id, None, "cleared head")
    _assert_equal(cleared.head_generation, 4, "clear generation")

    third = persist_gmail_temporal_review_projection(
        paths,
        source=_source_locator(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection("c"),
        expected_head_run_id=None,
        expected_head_generation=4,
    )
    _assert_equal(third.head_generation, 5, "post-clear generation")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET status='superseded' WHERE id=?",
            (DOCUMENT_ID,),
        )
    stale = get_gmail_temporal_review_head(
        paths,
        message_scope_key=rolled_back.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
    )
    if stale is None:
        raise PublicTemporalLedgerSmokeError("stale head disappeared")
    _assert_equal(stale.source_status, "stale", "superseded head status")
    _assert_equal(stale.run_id, third.run_id, "superseded head run")
    _assert_equal(stale.generation, 5, "superseded head generation")
    _assert_equal(
        stale.stale_reason,
        "source_document_not_active",
        "superseded head reason",
    )
    before_stale_restore = _table_rows(paths)
    try:
        rollback_gmail_temporal_review_head(
            paths,
            message_scope_key=rolled_back.message_scope_key,
            pipeline_scope=PIPELINE_SCOPE,
            expected_run_id=third.run_id,
            expected_generation=5,
            restore_run_id=initial_run_id,
        )
    except GmailTemporalHeadConflict as exc:
        raise PublicTemporalLedgerSmokeError(
            "stale source restore lost its compare-and-swap authority"
        ) from exc
    except GmailTemporalPersistenceError as exc:
        if str(exc) != "rollback target Gmail source authority is stale":
            raise PublicTemporalLedgerSmokeError(
                "stale source restore failed for an unexpected reason"
            ) from exc
    else:
        raise PublicTemporalLedgerSmokeError("stale source rollback was accepted")
    _assert_equal(
        _table_rows(paths),
        before_stale_restore,
        "stale source restore residue",
    )
    return {
        "cas_conflict_rejected_without_residue": True,
        "rollback_applied": True,
        "rollback_replay_idempotent": True,
        "source_bound_head_clear_applied": True,
        "post_clear_head_generation": 5,
        "superseded_source_marked_stale": True,
        "stale_source_restore_rejected": True,
    }


def _recover_and_compare(root: Path, paths: BrainPaths) -> dict[str, bool]:
    before = _table_rows(paths)
    service = OperationalService(paths, writer_guard=lambda: None)
    recovery_dir = root / "recovery-set"
    restored_home = root / "restored-home"
    recovery = create_coordinated_recovery_set(
        paths,
        service,
        output_dir=recovery_dir,
    )
    verification = verify_recovery_set(recovery_dir)
    restored = restore_recovery_set_isolated(recovery_dir, restored_home)
    restored_paths = BrainPaths.from_value(restored_home)
    after = _table_rows(restored_paths)
    _assert_equal(after, before, "restored temporal ledger snapshot")
    _assert_equal(verification["status"], "ok", "recovery verification")
    _assert_equal(restored["daemon_started"], False, "restored daemon state")
    quarantine = restored_paths.restore_quarantine_file
    if not quarantine.is_file():
        raise PublicTemporalLedgerSmokeError("restore quarantine is missing")
    quarantine_payload = json.loads(quarantine.read_text(encoding="utf-8"))
    _assert_equal(quarantine_payload.get("status"), "quarantined", "restore status")
    _assert_equal(
        quarantine_payload.get("activation_required"),
        True,
        "restore activation requirement",
    )
    _assert_equal(
        recovery.get("generation"),
        restored.get("generation"),
        "recovery generation",
    )
    return {
        "coordinated_recovery_verified": True,
        "temporal_rows_restored_exactly": True,
        "restored_home_quarantined": True,
        "restored_daemon_not_started": True,
    }


def resume_public_smoke(root: Path) -> dict[str, object]:
    resolved, root_token = _validate_marked_root(root)
    paths = _validate_smoke_tree(resolved, root_token)
    initial_run_id, second_run_id, second_generation = _replay_and_advance(paths)
    ledger_checks = _rollback_clear_and_stale(
        paths,
        initial_run_id=initial_run_id,
        second_run_id=second_run_id,
        second_generation=second_generation,
    )
    recovery_checks = _recover_and_compare(resolved, paths)
    counts = _table_counts(_table_rows(paths))
    _assert_equal(
        counts,
        {
            "runs": 3,
            "artifacts": 3,
            "heads": 1,
            "executions": 0,
            "components": 0,
        },
        "final temporal row counts",
    )
    return {
        "version": REPORT_VERSION,
        "action": "resume",
        "status": "passed",
        "checks": {
            "fresh_process_exact_replay": True,
            **ledger_checks,
            **recovery_checks,
        },
        "counts": counts,
        **_safety_contract(),
    }


def run_public_smoke(root: Path) -> dict[str, object]:
    initialized = initialize_public_smoke(root)
    script = Path(__file__).resolve()
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "resume",
            "--root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(script.parent.parent),
    )
    if completed.returncode != 0:
        raise PublicTemporalLedgerSmokeError("fresh-process resume failed")
    try:
        resumed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PublicTemporalLedgerSmokeError("fresh-process report is invalid") from exc
    if not isinstance(resumed, dict) or resumed.get("status") != "passed":
        raise PublicTemporalLedgerSmokeError("fresh-process resume did not pass")
    checks = resumed.get("checks")
    counts = resumed.get("counts")
    if not isinstance(checks, dict) or not isinstance(counts, dict):
        raise PublicTemporalLedgerSmokeError("fresh-process report is incomplete")
    return {
        "version": REPORT_VERSION,
        "action": "run",
        "status": "passed",
        "checks": {
            **dict(initialized["checks"]),
            **checks,
            "fresh_python_process_used": True,
        },
        "counts": counts,
        **_safety_contract(),
    }


def _failed_report(action: str) -> dict[str, object]:
    return {
        "version": REPORT_VERSION,
        "action": action,
        "status": "failed",
        "fatal": True,
        "error_buckets": {"public_temporal_ledger_smoke_failed": 1},
        **_safety_contract(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("initialize", "resume", "run"):
        child = subparsers.add_parser(action)
        child.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.action == "initialize":
            report = initialize_public_smoke(args.root)
        elif args.action == "resume":
            report = resume_public_smoke(args.root)
        else:
            report = run_public_smoke(args.root)
    except Exception:  # noqa: BLE001 - output must remain static and aggregate-only.
        report = _failed_report(args.action)
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
