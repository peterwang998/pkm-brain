from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence

from .chunking import strip_frontmatter
from .db import connection
from .gmail_temporal_review import (
    GmailTemporalReviewError,
    GmailTemporalReviewProjection,
    canonical_gmail_temporal_review_projection_bytes,
    gmail_temporal_review_projection_payload,
)
from .paths import BrainPaths
from .source_dates import (
    gmail_message_source_evidence,
    source_frontmatter_with_path,
    trusted_gmail_message_timestamps,
)
from .util import now_iso


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_GMAIL_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_PIPELINE_SCOPE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
_RUN_VERSION = "gmail_temporal_review_persistence_v1"
_LOCATOR_VERSION = "gmail_temporal_source_locator_v1"
_ARTIFACT_KINDS = {"supported_citation", "uncertainty_sidecar"}


class GmailTemporalPersistenceError(ValueError):
    """Raised when a projection cannot enter the review-only ledger."""


class GmailTemporalPersistenceConflict(GmailTemporalPersistenceError):
    """Raised when one immutable input key is paired with different bytes."""


class GmailTemporalHeadConflict(GmailTemporalPersistenceError):
    """Raised when a current-head compare-and-swap loses its authority."""


@dataclass(frozen=True)
class GmailTemporalSourceLocator:
    """Immutable Gmail Knowledge authority for one trusted projected message.

    This locator deliberately accepts the archive-derived 64-hex revision used
    by current Gmail Knowledge projections.  Operational mirror revisions have
    a different identity and never enter this main-DB review ledger.
    """

    document_id: str
    document_content_hash: str
    gmail_account_key: str
    gmail_thread_id: str
    gmail_source_revision: str
    gmail_message_id: str
    message_internal_at: str
    message_start_offset: int
    message_end_offset: int
    source_sha256: str
    version: str = _LOCATOR_VERSION

    def validated(self) -> GmailTemporalSourceLocator:
        if self.version != _LOCATOR_VERSION:
            raise GmailTemporalPersistenceError("unsupported Gmail source locator")
        document_id = _bounded_text(self.document_id, "document_id", 256)
        account_key = _bounded_text(self.gmail_account_key, "gmail_account_key", 512)
        thread_id = _bounded_text(self.gmail_thread_id, "gmail_thread_id", 2_000)
        message_id = _bounded_text(self.gmail_message_id, "gmail_message_id", 2_000)
        if not _GMAIL_MESSAGE_ID.fullmatch(message_id):
            raise GmailTemporalPersistenceError("invalid Gmail provider message id")
        content_hash = _sha256(self.document_content_hash, "document_content_hash")
        source_revision = _sha256(self.gmail_source_revision, "gmail_source_revision")
        source_sha256 = _sha256(self.source_sha256, "source_sha256")
        internal_at = _aware_timestamp(self.message_internal_at, "message_internal_at")
        if (
            isinstance(self.message_start_offset, bool)
            or isinstance(self.message_end_offset, bool)
            or not isinstance(self.message_start_offset, int)
            or not isinstance(self.message_end_offset, int)
            or self.message_start_offset < 0
            or self.message_end_offset <= self.message_start_offset
        ):
            raise GmailTemporalPersistenceError(
                "Gmail rendered-message range must be a non-empty half-open interval"
            )
        return replace(
            self,
            document_id=document_id,
            document_content_hash=content_hash,
            gmail_account_key=account_key,
            gmail_thread_id=thread_id,
            gmail_source_revision=source_revision,
            gmail_message_id=message_id,
            message_internal_at=internal_at,
            source_sha256=source_sha256,
        )


@dataclass(frozen=True)
class GmailTemporalReviewHead:
    message_scope_key: str
    pipeline_scope: str
    run_id: str | None
    generation: int
    updated_at: str
    source_status: str = "unchecked"
    stale_reason: str | None = None


@dataclass(frozen=True)
class GmailTemporalPersistenceResult:
    run_id: str
    input_key: str
    message_scope_key: str
    pipeline_scope: str
    projection_sha256: str
    artifact_set_sha256: str
    artifact_count: int
    head_generation: int
    replayed: bool
    head_changed: bool


@dataclass(frozen=True)
class GmailTemporalRollbackResult:
    message_scope_key: str
    pipeline_scope: str
    previous_run_id: str | None
    current_run_id: str | None
    head_generation: int
    changed: bool


@dataclass(frozen=True)
class _PreparedArtifact:
    id: str
    kind: str
    source_key: str
    candidate_authorization: bool
    payload_sha256: str
    payload_json: str


@dataclass(frozen=True)
class _PreparedProjection:
    run_id: str
    input_key: str
    message_scope_key: str
    pipeline_scope: str
    locator: GmailTemporalSourceLocator
    locator_hash: str
    locator_json: str
    projection_version: str
    analysis_fingerprint: str
    batch_plan_fingerprint: str
    ensemble_policy_fingerprint: str
    grouping_policy_fingerprint: str
    projection_fingerprint: str
    projection_sha256: str
    artifact_set_sha256: str
    projection_json: str
    artifacts: tuple[_PreparedArtifact, ...]


def gmail_temporal_message_scope_key(
    *, gmail_account_key: str, gmail_thread_id: str, gmail_message_id: str
) -> str:
    """Return the stable logical-message key used by the mutable head."""

    account = _bounded_text(gmail_account_key, "gmail_account_key", 512)
    thread = _bounded_text(gmail_thread_id, "gmail_thread_id", 2_000)
    message = _bounded_text(gmail_message_id, "gmail_message_id", 2_000)
    if not _GMAIL_MESSAGE_ID.fullmatch(message):
        raise GmailTemporalPersistenceError("invalid Gmail provider message id")
    material = {
        "version": "gmail_temporal_message_scope_v1",
        "gmail_account_key": account,
        "gmail_thread_id": thread,
        "gmail_message_id": message,
    }
    return "gtmsg_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def persist_gmail_temporal_review_projection(
    paths: BrainPaths,
    *,
    source: GmailTemporalSourceLocator,
    pipeline_scope: str,
    projection: GmailTemporalReviewProjection,
    expected_head_run_id: str | None,
    expected_head_generation: int | None,
) -> GmailTemporalPersistenceResult:
    """Atomically append one complete projection and CAS its current head.

    Exact replay reuses the immutable run and artifact rows.  A collision on the
    deterministic input key, any partial projection, or a stale head leaves no
    new durable state.
    """

    prepared = _prepare_projection(
        source=source,
        pipeline_scope=pipeline_scope,
        projection=projection,
    )
    _validate_expected_head(expected_head_run_id, expected_head_generation)
    created_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        with _savepoint(conn, "gmail_temporal_review_persist"):
            _validate_document_authority(conn, prepared.locator)
            existing = conn.execute(
                """
                SELECT * FROM gmail_temporal_review_runs
                WHERE input_key = ?
                """,
                (prepared.input_key,),
            ).fetchone()
            replayed = existing is not None
            if existing is None:
                _insert_run(conn, prepared, created_at=created_at)
                _insert_artifacts(conn, prepared, created_at=created_at)
            else:
                _validate_existing_run(conn, existing, prepared)
            head, head_changed = _advance_head(
                conn,
                message_scope_key=prepared.message_scope_key,
                pipeline_scope=prepared.pipeline_scope,
                run_id=prepared.run_id,
                expected_run_id=expected_head_run_id,
                expected_generation=expected_head_generation,
                updated_at=created_at,
            )
    return GmailTemporalPersistenceResult(
        run_id=prepared.run_id,
        input_key=prepared.input_key,
        message_scope_key=prepared.message_scope_key,
        pipeline_scope=prepared.pipeline_scope,
        projection_sha256=prepared.projection_sha256,
        artifact_set_sha256=prepared.artifact_set_sha256,
        artifact_count=len(prepared.artifacts),
        head_generation=head.generation,
        replayed=replayed,
        head_changed=head_changed,
    )


def get_gmail_temporal_review_head(
    paths: BrainPaths,
    *,
    message_scope_key: str,
    pipeline_scope: str,
) -> GmailTemporalReviewHead | None:
    message_scope = _scope_key(message_scope_key)
    pipeline = _pipeline_scope(pipeline_scope)
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT message_scope_key, pipeline_scope, run_id, generation, updated_at
            FROM gmail_temporal_review_heads
            WHERE message_scope_key = ? AND pipeline_scope = ?
            """,
            (message_scope, pipeline),
        ).fetchone()
        if row is None:
            return None
        head = _head_from_row(row)
        if head.run_id is None:
            return replace(head, source_status="cleared")
        try:
            run = _run_row(conn, head.run_id)
            _validate_document_authority(conn, _locator_from_run(run))
        except GmailTemporalHeadConflict:
            return replace(
                head,
                source_status="stale",
                stale_reason="source_document_not_active",
            )
        except GmailTemporalPersistenceError:
            return replace(
                head,
                source_status="stale",
                stale_reason="source_authority_invalid",
            )
        return replace(head, source_status="current")


def rollback_gmail_temporal_review_head(
    paths: BrainPaths,
    *,
    message_scope_key: str,
    pipeline_scope: str,
    expected_run_id: str | None,
    expected_generation: int,
    restore_run_id: str | None = None,
) -> GmailTemporalRollbackResult:
    """CAS one head to a prior complete run, or clear it without data loss."""

    message_scope = _scope_key(message_scope_key)
    pipeline = _pipeline_scope(pipeline_scope)
    _validate_expected_head(expected_run_id, expected_generation)
    restore = (
        _bounded_text(restore_run_id, "restore_run_id", 128)
        if restore_run_id is not None
        else None
    )
    updated_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        with _savepoint(conn, "gmail_temporal_review_rollback"):
            current_row = conn.execute(
                """
                SELECT message_scope_key, pipeline_scope, run_id, generation, updated_at
                FROM gmail_temporal_review_heads
                WHERE message_scope_key = ? AND pipeline_scope = ?
                """,
                (message_scope, pipeline),
            ).fetchone()
            if current_row is None:
                raise GmailTemporalHeadConflict(
                    "Gmail temporal review head does not exist"
                )
            current = _head_from_row(current_row)
            target = None
            if restore is not None:
                target = conn.execute(
                    """
                    SELECT gmail_temporal_review_runs.*,
                           rowid AS ledger_sequence
                    FROM gmail_temporal_review_runs
                    WHERE id = ? AND message_scope_key = ? AND pipeline_scope = ?
                      AND complete = 1 AND routable = 0
                    """,
                    (restore, message_scope, pipeline),
                ).fetchone()
                if target is None:
                    raise GmailTemporalPersistenceError(
                        "rollback target is not a complete run in this message scope"
                    )
                try:
                    _validate_document_authority(conn, _locator_from_run(target))
                except GmailTemporalPersistenceError as exc:
                    raise GmailTemporalPersistenceError(
                        "rollback target Gmail source authority is stale"
                    ) from exc
            if current.run_id == restore:
                return GmailTemporalRollbackResult(
                    message_scope_key=message_scope,
                    pipeline_scope=pipeline,
                    previous_run_id=expected_run_id,
                    current_run_id=restore,
                    head_generation=current.generation,
                    changed=False,
                )
            if (
                current.run_id != expected_run_id
                or current.generation != expected_generation
            ):
                raise GmailTemporalHeadConflict(
                    "Gmail temporal review head changed before rollback"
                )
            if target is not None:
                if current.run_id is not None:
                    current_run = conn.execute(
                        """
                        SELECT rowid AS ledger_sequence
                        FROM gmail_temporal_review_runs
                        WHERE id = ?
                        """,
                        (current.run_id,),
                    ).fetchone()
                    if current_run is None or int(target["ledger_sequence"]) >= int(
                        current_run["ledger_sequence"]
                    ):
                        raise GmailTemporalPersistenceError(
                            "rollback target must precede the current run"
                        )
            cursor = conn.execute(
                """
                UPDATE gmail_temporal_review_heads
                SET run_id = ?, generation = generation + 1, updated_at = ?
                WHERE message_scope_key = ? AND pipeline_scope = ?
                  AND generation = ?
                  AND ((run_id = ?) OR (run_id IS NULL AND ? IS NULL))
                """,
                (
                    restore,
                    updated_at,
                    message_scope,
                    pipeline,
                    expected_generation,
                    expected_run_id,
                    expected_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise GmailTemporalHeadConflict(
                    "Gmail temporal review head changed concurrently"
                )
            next_head = GmailTemporalReviewHead(
                message_scope_key=message_scope,
                pipeline_scope=pipeline,
                run_id=restore,
                generation=expected_generation + 1,
                updated_at=updated_at,
            )
    return GmailTemporalRollbackResult(
        message_scope_key=message_scope,
        pipeline_scope=pipeline,
        previous_run_id=expected_run_id,
        current_run_id=restore,
        head_generation=next_head.generation,
        changed=True,
    )


def _prepare_projection(
    *,
    source: GmailTemporalSourceLocator,
    pipeline_scope: str,
    projection: GmailTemporalReviewProjection,
) -> _PreparedProjection:
    locator = source.validated()
    pipeline = _pipeline_scope(pipeline_scope)
    if (
        not isinstance(projection, GmailTemporalReviewProjection)
        or projection.complete is not True
        or projection.requires_defer is not True
        or projection.routable is not False
        or projection.independent_invocations_verified is not False
    ):
        raise GmailTemporalPersistenceError(
            "only complete, deferred, non-routable review projections persist"
        )
    try:
        payload = gmail_temporal_review_projection_payload(projection)
        projection_bytes = canonical_gmail_temporal_review_projection_bytes(projection)
    except GmailTemporalReviewError as exc:
        raise GmailTemporalPersistenceError(
            "review projection failed canonical validation"
        ) from exc
    if not isinstance(payload, Mapping):
        raise GmailTemporalPersistenceError("review projection payload is malformed")
    if not isinstance(projection_bytes, bytes) or projection_bytes != _canonical_bytes(
        payload
    ):
        raise GmailTemporalPersistenceError("review projection is not canonical")
    if payload.get("complete") is not True:
        raise GmailTemporalPersistenceError("only complete review projections persist")
    if payload.get("routable") is not False:
        raise GmailTemporalPersistenceError("review projections must be non-routable")
    if payload.get("requires_defer") is not True:
        raise GmailTemporalPersistenceError("review projections must require deferral")
    if payload.get("independent_invocations_verified") is not False:
        raise GmailTemporalPersistenceError(
            "review persistence cannot attest independent model invocations"
        )
    if payload.get("source_sha256") != locator.source_sha256:
        raise GmailTemporalPersistenceError(
            "projection source hash does not match the Gmail source locator"
        )

    projection_version = _payload_text(payload, "version", 128)
    projection_fingerprint = _payload_text(payload, "projection_fingerprint", 256)
    analysis_fingerprint = _payload_text(payload, "analysis_fingerprint", 256)
    batch_plan_fingerprint = _payload_text(payload, "batch_plan_fingerprint", 256)
    ensemble_policy_fingerprint = _payload_text(
        payload, "ensemble_policy_fingerprint", 256
    )
    grouping_policy_fingerprint = _payload_text(
        payload, "grouping_policy_fingerprint", 256
    )
    raw_component_fingerprints = _payload_sequence(
        payload, "component_evidence_fingerprints"
    )
    if len(raw_component_fingerprints) != 3:
        raise GmailTemporalPersistenceError(
            "complete review persistence requires three component evidence hashes"
        )
    component_evidence_fingerprints = tuple(
        _sha256(value, "component_evidence_fingerprint")
        for value in raw_component_fingerprints
    )
    locator_payload = _source_locator_payload(locator)
    locator_bytes = _canonical_bytes(locator_payload)
    locator_hash = "gtloc_" + hashlib.sha256(locator_bytes).hexdigest()
    locator_json = locator_bytes.decode("utf-8")
    message_scope_key = gmail_temporal_message_scope_key(
        gmail_account_key=locator.gmail_account_key,
        gmail_thread_id=locator.gmail_thread_id,
        gmail_message_id=locator.gmail_message_id,
    )
    input_material = {
        "version": _RUN_VERSION,
        "pipeline_scope": pipeline,
        "source_locator_hash": locator_hash,
        "projection_version": projection_version,
        "analysis_fingerprint": analysis_fingerprint,
        "batch_plan_fingerprint": batch_plan_fingerprint,
        "ensemble_policy_fingerprint": ensemble_policy_fingerprint,
        "grouping_policy_fingerprint": grouping_policy_fingerprint,
        "component_evidence_fingerprints": component_evidence_fingerprints,
        "independent_invocations_verified": False,
    }
    input_key = "gtri_" + hashlib.sha256(_canonical_bytes(input_material)).hexdigest()
    run_id = "gtrr_" + hashlib.sha256(input_key.encode("ascii")).hexdigest()
    artifacts = _prepare_artifacts(
        run_id=run_id,
        artifacts=_payload_sequence(payload, "artifacts"),
        cluster_reviews=_payload_sequence(payload, "cluster_reviews"),
    )
    artifact_set_material = [
        {
            "artifact_kind": item.kind,
            "source_artifact_key": item.source_key,
            "candidate_authorization": item.candidate_authorization,
            "payload_sha256": item.payload_sha256,
        }
        for item in artifacts
    ]
    projection_sha256 = hashlib.sha256(projection_bytes).hexdigest()
    artifact_set_sha256 = hashlib.sha256(
        _canonical_bytes(artifact_set_material)
    ).hexdigest()
    return _PreparedProjection(
        run_id=run_id,
        input_key=input_key,
        message_scope_key=message_scope_key,
        pipeline_scope=pipeline,
        locator=locator,
        locator_hash=locator_hash,
        locator_json=locator_json,
        projection_version=projection_version,
        analysis_fingerprint=analysis_fingerprint,
        batch_plan_fingerprint=batch_plan_fingerprint,
        ensemble_policy_fingerprint=ensemble_policy_fingerprint,
        grouping_policy_fingerprint=grouping_policy_fingerprint,
        projection_fingerprint=projection_fingerprint,
        projection_sha256=projection_sha256,
        artifact_set_sha256=artifact_set_sha256,
        projection_json=projection_bytes.decode("utf-8"),
        artifacts=artifacts,
    )


def _prepare_artifacts(
    *,
    run_id: str,
    artifacts: Sequence[Any],
    cluster_reviews: Sequence[Any],
) -> tuple[_PreparedArtifact, ...]:
    prepared: list[_PreparedArtifact] = []
    seen: set[tuple[str, str]] = set()
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise GmailTemporalPersistenceError("review artifact is malformed")
        kind = _payload_text(raw, "kind", 64)
        if kind not in _ARTIFACT_KINDS:
            raise GmailTemporalPersistenceError("unknown review artifact kind")
        source_key = _payload_text(raw, "artifact_id", 512)
        if raw.get("candidate_authorization") is not True:
            raise GmailTemporalPersistenceError(
                "citation and uncertainty artifacts must retain candidate authority"
            )
        expected_evidence = "supported" if kind == "supported_citation" else "uncertain"
        if raw.get("evidence_status") != expected_evidence:
            raise GmailTemporalPersistenceError(
                "review artifact evidence status does not match its kind"
            )
        _validate_non_routable_artifact(raw)
        prepared.append(
            _prepared_artifact(
                run_id=run_id,
                kind=kind,
                source_key=source_key,
                candidate_authorization=True,
                payload=raw,
            )
        )
    for raw in cluster_reviews:
        if not isinstance(raw, Mapping):
            raise GmailTemporalPersistenceError("cluster review is malformed")
        source_key = _payload_text(raw, "review_id", 512)
        if raw.get("candidate_authorization") is not False:
            raise GmailTemporalPersistenceError(
                "split-semantic cluster review cannot authorize a candidate"
            )
        _validate_non_routable_artifact(raw)
        prepared.append(
            _prepared_artifact(
                run_id=run_id,
                kind="cluster_review",
                source_key=source_key,
                candidate_authorization=False,
                payload=raw,
            )
        )
    output = sorted(
        prepared,
        key=lambda item: (item.kind, item.source_key, item.payload_sha256),
    )
    for item in output:
        identity = (item.kind, item.source_key)
        if identity in seen:
            raise GmailTemporalPersistenceError("duplicate review artifact identity")
        seen.add(identity)
    return tuple(output)


def _prepared_artifact(
    *,
    run_id: str,
    kind: str,
    source_key: str,
    candidate_authorization: bool,
    payload: Mapping[str, Any],
) -> _PreparedArtifact:
    payload_bytes = _canonical_bytes(payload)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    identity = _canonical_bytes(
        {
            "run_id": run_id,
            "artifact_kind": kind,
            "source_artifact_key": source_key,
        }
    )
    return _PreparedArtifact(
        id="gtra_" + hashlib.sha256(identity).hexdigest(),
        kind=kind,
        source_key=source_key,
        candidate_authorization=candidate_authorization,
        payload_sha256=payload_sha256,
        payload_json=payload_bytes.decode("utf-8"),
    )


def _validate_non_routable_artifact(value: Mapping[str, Any]) -> None:
    if value.get("routable") is not False or value.get("requires_defer") is not True:
        raise GmailTemporalPersistenceError(
            "review artifacts must remain deferred and non-routable"
        )


def _validate_document_authority(
    conn: sqlite3.Connection, locator: GmailTemporalSourceLocator
) -> None:
    row = conn.execute(
        """
        SELECT *
        FROM documents
        WHERE id = ?
        """,
        (locator.document_id,),
    ).fetchone()
    if row is None:
        raise GmailTemporalPersistenceError("source document does not exist")
    if row["source_type"] != "gmail_thread":
        raise GmailTemporalPersistenceError("source document is not Gmail Knowledge")
    if row["content_hash"] != locator.document_content_hash:
        raise GmailTemporalPersistenceError("source document content hash changed")
    if row["status"] != "active":
        raise GmailTemporalHeadConflict(
            "source document is no longer the active Gmail revision"
        )
    document = dict(row)
    frontmatter, frontmatter_path = source_frontmatter_with_path(document)
    timestamps = (
        trusted_gmail_message_timestamps(document, frontmatter, frontmatter_path)
        if frontmatter_path is not None
        else None
    )
    if timestamps is None or frontmatter_path is None:
        raise GmailTemporalPersistenceError(
            "source document lacks trusted Gmail message authority"
        )
    expected_lineage = (
        locator.gmail_account_key,
        locator.gmail_thread_id,
        locator.gmail_source_revision,
    )
    actual_lineage = tuple(
        str(frontmatter.get(key) or "").strip()
        for key in (
            "gmail_account_key",
            "gmail_thread_id",
            "gmail_source_revision",
        )
    )
    if actual_lineage != expected_lineage:
        raise GmailTemporalPersistenceError(
            "Gmail source locator lineage does not match the immutable document"
        )
    matches = [
        item
        for item in timestamps
        if str(item.get("message_id") or "") == locator.gmail_message_id
    ]
    if len(matches) != 1:
        raise GmailTemporalPersistenceError(
            "Gmail source locator message does not exist in the immutable document"
        )
    message = matches[0]
    internal_at = message.get("internal_date")
    try:
        canonical_internal_at = _aware_timestamp(
            internal_at, "trusted Gmail message internal date"
        )
    except GmailTemporalPersistenceError as exc:
        raise GmailTemporalPersistenceError(
            "trusted Gmail message lacks an assertion clock"
        ) from exc
    if canonical_internal_at != locator.message_internal_at:
        raise GmailTemporalPersistenceError(
            "Gmail source locator clock does not match the immutable document"
        )
    if (
        message.get("start_offset") != locator.message_start_offset
        or message.get("end_offset") != locator.message_end_offset
    ):
        raise GmailTemporalPersistenceError(
            "Gmail source locator range does not match the immutable document"
        )
    try:
        source_bytes = frontmatter_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GmailTemporalPersistenceError(
            "trusted Gmail source could not be reproduced"
        ) from exc
    if hashlib.sha256(source_bytes).hexdigest() != locator.document_content_hash:
        raise GmailTemporalPersistenceError(
            "trusted Gmail source changed during authority validation"
        )
    body = strip_frontmatter(source_text)
    rendered_message = body[locator.message_start_offset : locator.message_end_offset]
    evidence = gmail_message_source_evidence(rendered_message)
    if evidence is None:
        raise GmailTemporalPersistenceError(
            "trusted Gmail selector input could not be reproduced"
        )
    if hashlib.sha256(evidence.text.encode("utf-8")).hexdigest() != (
        locator.source_sha256
    ):
        raise GmailTemporalPersistenceError(
            "Gmail source locator hash does not match reproduced selector input"
        )


def _insert_run(
    conn: sqlite3.Connection,
    prepared: _PreparedProjection,
    *,
    created_at: str,
) -> None:
    locator = prepared.locator
    conn.execute(
        """
        INSERT INTO gmail_temporal_review_runs(
          id, input_key, message_scope_key, pipeline_scope, document_id,
          document_content_hash, gmail_account_key, gmail_thread_id,
          gmail_source_revision, gmail_message_id, message_internal_at,
          message_start_offset, message_end_offset, source_sha256,
          source_locator_hash, source_locator_json, projection_version,
          analysis_fingerprint, batch_plan_fingerprint,
          ensemble_policy_fingerprint, grouping_policy_fingerprint,
          projection_fingerprint,
          projection_sha256, artifact_set_sha256, projection_json,
          complete, routable, created_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          1, 0, ?
        )
        """,
        (
            prepared.run_id,
            prepared.input_key,
            prepared.message_scope_key,
            prepared.pipeline_scope,
            locator.document_id,
            locator.document_content_hash,
            locator.gmail_account_key,
            locator.gmail_thread_id,
            locator.gmail_source_revision,
            locator.gmail_message_id,
            locator.message_internal_at,
            locator.message_start_offset,
            locator.message_end_offset,
            locator.source_sha256,
            prepared.locator_hash,
            prepared.locator_json,
            prepared.projection_version,
            prepared.analysis_fingerprint,
            prepared.batch_plan_fingerprint,
            prepared.ensemble_policy_fingerprint,
            prepared.grouping_policy_fingerprint,
            prepared.projection_fingerprint,
            prepared.projection_sha256,
            prepared.artifact_set_sha256,
            prepared.projection_json,
            created_at,
        ),
    )


def _insert_artifacts(
    conn: sqlite3.Connection,
    prepared: _PreparedProjection,
    *,
    created_at: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO gmail_temporal_review_artifacts(
          id, run_id, artifact_kind, source_artifact_key,
          candidate_authorization, payload_sha256, payload_json, routable,
          created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            (
                item.id,
                prepared.run_id,
                item.kind,
                item.source_key,
                int(item.candidate_authorization),
                item.payload_sha256,
                item.payload_json,
                created_at,
            )
            for item in prepared.artifacts
        ),
    )


def _validate_existing_run(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    prepared: _PreparedProjection,
) -> None:
    locator = prepared.locator
    expected: dict[str, Any] = {
        "id": prepared.run_id,
        "input_key": prepared.input_key,
        "message_scope_key": prepared.message_scope_key,
        "pipeline_scope": prepared.pipeline_scope,
        "document_id": locator.document_id,
        "document_content_hash": locator.document_content_hash,
        "gmail_account_key": locator.gmail_account_key,
        "gmail_thread_id": locator.gmail_thread_id,
        "gmail_source_revision": locator.gmail_source_revision,
        "gmail_message_id": locator.gmail_message_id,
        "message_internal_at": locator.message_internal_at,
        "message_start_offset": locator.message_start_offset,
        "message_end_offset": locator.message_end_offset,
        "source_sha256": locator.source_sha256,
        "source_locator_hash": prepared.locator_hash,
        "source_locator_json": prepared.locator_json,
        "projection_version": prepared.projection_version,
        "analysis_fingerprint": prepared.analysis_fingerprint,
        "batch_plan_fingerprint": prepared.batch_plan_fingerprint,
        "ensemble_policy_fingerprint": prepared.ensemble_policy_fingerprint,
        "grouping_policy_fingerprint": prepared.grouping_policy_fingerprint,
        "projection_fingerprint": prepared.projection_fingerprint,
        "projection_sha256": prepared.projection_sha256,
        "artifact_set_sha256": prepared.artifact_set_sha256,
        "projection_json": prepared.projection_json,
        "complete": 1,
        "routable": 0,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise GmailTemporalPersistenceConflict(
            "deterministic Gmail temporal review input produced different bytes"
        )
    stored = conn.execute(
        """
        SELECT id, artifact_kind, source_artifact_key, candidate_authorization,
               payload_sha256, payload_json, routable
        FROM gmail_temporal_review_artifacts
        WHERE run_id = ?
        ORDER BY artifact_kind, source_artifact_key, payload_sha256
        """,
        (prepared.run_id,),
    ).fetchall()
    expected_artifacts = [
        (
            item.id,
            item.kind,
            item.source_key,
            int(item.candidate_authorization),
            item.payload_sha256,
            item.payload_json,
            0,
        )
        for item in prepared.artifacts
    ]
    actual_artifacts = [
        (
            item["id"],
            item["artifact_kind"],
            item["source_artifact_key"],
            item["candidate_authorization"],
            item["payload_sha256"],
            item["payload_json"],
            item["routable"],
        )
        for item in stored
    ]
    if actual_artifacts != expected_artifacts:
        raise GmailTemporalPersistenceConflict(
            "stored Gmail temporal review artifacts do not match exact replay"
        )


def _advance_head(
    conn: sqlite3.Connection,
    *,
    message_scope_key: str,
    pipeline_scope: str,
    run_id: str,
    expected_run_id: str | None,
    expected_generation: int | None,
    updated_at: str,
) -> tuple[GmailTemporalReviewHead, bool]:
    row = conn.execute(
        """
        SELECT message_scope_key, pipeline_scope, run_id, generation, updated_at
        FROM gmail_temporal_review_heads
        WHERE message_scope_key = ? AND pipeline_scope = ?
        """,
        (message_scope_key, pipeline_scope),
    ).fetchone()
    if row is None:
        if expected_run_id is not None or expected_generation is not None:
            raise GmailTemporalHeadConflict(
                "Gmail temporal review head disappeared before persistence"
            )
        conn.execute(
            """
            INSERT INTO gmail_temporal_review_heads(
              message_scope_key, pipeline_scope, run_id, generation, updated_at
            ) VALUES (?, ?, ?, 1, ?)
            """,
            (message_scope_key, pipeline_scope, run_id, updated_at),
        )
        return (
            GmailTemporalReviewHead(
                message_scope_key=message_scope_key,
                pipeline_scope=pipeline_scope,
                run_id=run_id,
                generation=1,
                updated_at=updated_at,
            ),
            True,
        )
    current = _head_from_row(row)
    if current.run_id == run_id:
        return current, False
    if current.run_id != expected_run_id or current.generation != expected_generation:
        raise GmailTemporalHeadConflict(
            "Gmail temporal review head changed before persistence"
        )
    cursor = conn.execute(
        """
        UPDATE gmail_temporal_review_heads
        SET run_id = ?, generation = generation + 1, updated_at = ?
        WHERE message_scope_key = ? AND pipeline_scope = ? AND generation = ?
          AND ((run_id = ?) OR (run_id IS NULL AND ? IS NULL))
        """,
        (
            run_id,
            updated_at,
            message_scope_key,
            pipeline_scope,
            expected_generation,
            expected_run_id,
            expected_run_id,
        ),
    )
    if cursor.rowcount != 1:
        raise GmailTemporalHeadConflict(
            "Gmail temporal review head changed concurrently"
        )
    return (
        GmailTemporalReviewHead(
            message_scope_key=message_scope_key,
            pipeline_scope=pipeline_scope,
            run_id=run_id,
            generation=current.generation + 1,
            updated_at=updated_at,
        ),
        True,
    )


def _run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM gmail_temporal_review_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise GmailTemporalPersistenceError(
            "Gmail temporal review head references a missing run"
        )
    return row


def _locator_from_run(row: sqlite3.Row) -> GmailTemporalSourceLocator:
    return GmailTemporalSourceLocator(
        document_id=str(row["document_id"]),
        document_content_hash=str(row["document_content_hash"]),
        gmail_account_key=str(row["gmail_account_key"]),
        gmail_thread_id=str(row["gmail_thread_id"]),
        gmail_source_revision=str(row["gmail_source_revision"]),
        gmail_message_id=str(row["gmail_message_id"]),
        message_internal_at=str(row["message_internal_at"]),
        message_start_offset=int(row["message_start_offset"]),
        message_end_offset=int(row["message_end_offset"]),
        source_sha256=str(row["source_sha256"]),
    ).validated()


def _source_locator_payload(locator: GmailTemporalSourceLocator) -> dict[str, Any]:
    return {
        "version": locator.version,
        "document_id": locator.document_id,
        "document_content_hash": locator.document_content_hash,
        "gmail_account_key": locator.gmail_account_key,
        "gmail_thread_id": locator.gmail_thread_id,
        "gmail_source_revision": locator.gmail_source_revision,
        "gmail_message_id": locator.gmail_message_id,
        "message_internal_at": locator.message_internal_at,
        "message_start_offset": locator.message_start_offset,
        "message_end_offset": locator.message_end_offset,
        "source_sha256": locator.source_sha256,
    }


def _head_from_row(row: sqlite3.Row) -> GmailTemporalReviewHead:
    return GmailTemporalReviewHead(
        message_scope_key=str(row["message_scope_key"]),
        pipeline_scope=str(row["pipeline_scope"]),
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        generation=int(row["generation"]),
        updated_at=str(row["updated_at"]),
    )


def _payload_text(value: Mapping[str, Any], key: str, limit: int) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        raise GmailTemporalPersistenceError(f"projection {key} is missing")
    return _bounded_text(raw, key, limit)


def _payload_sequence(value: Mapping[str, Any], key: str) -> Sequence[Any]:
    raw = value.get(key)
    if not isinstance(raw, (list, tuple)):
        raise GmailTemporalPersistenceError(f"projection {key} is malformed")
    return raw


def _bounded_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise GmailTemporalPersistenceError(f"{name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise GmailTemporalPersistenceError(f"invalid {name}")
    return normalized


def _sha256(value: Any, name: str) -> str:
    normalized = _bounded_text(value, name, 64)
    if not _SHA256_HEX.fullmatch(normalized):
        raise GmailTemporalPersistenceError(f"invalid {name}")
    return normalized


def _aware_timestamp(value: Any, name: str) -> str:
    normalized = _bounded_text(value, name, 128)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalPersistenceError(f"invalid {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GmailTemporalPersistenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _pipeline_scope(value: str) -> str:
    normalized = _bounded_text(value, "pipeline_scope", 128).casefold()
    if not _PIPELINE_SCOPE.fullmatch(normalized):
        raise GmailTemporalPersistenceError("invalid pipeline_scope")
    return normalized


def _scope_key(value: str) -> str:
    normalized = _bounded_text(value, "message_scope_key", 70)
    if not normalized.startswith("gtmsg_") or not _SHA256_HEX.fullmatch(
        normalized.removeprefix("gtmsg_")
    ):
        raise GmailTemporalPersistenceError("invalid message_scope_key")
    return normalized


def _validate_expected_head(run_id: str | None, generation: int | None) -> None:
    if run_id is not None:
        _bounded_text(run_id, "expected_head_run_id", 128)
    if generation is None:
        if run_id is not None:
            raise GmailTemporalPersistenceError(
                "an expected head run requires its generation"
            )
        return
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise GmailTemporalPersistenceError(
            "expected head generation must be a positive integer"
        )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GmailTemporalPersistenceError(
            "review projection payload is not JSON-safe"
        ) from exc


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")
