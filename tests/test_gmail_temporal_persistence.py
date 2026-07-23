from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import pkm_brain.gmail_temporal_persistence as persistence_module
from pkm_brain.db import connection, init_db
from pkm_brain.gmail_temporal_persistence import (
    GmailTemporalHeadConflict,
    GmailTemporalPersistenceConflict,
    GmailTemporalPersistenceError,
    GmailTemporalSourceLocator,
    get_gmail_temporal_review_head,
    persist_gmail_temporal_review_projection,
    rollback_gmail_temporal_review_head,
)
from pkm_brain.gmail_temporal_frontier import (
    gmail_temporal_candidate_ensemble_policy_fingerprint,
)
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewArtifact,
    GmailTemporalReviewClusterReview,
    GmailTemporalReviewGroup,
    GmailTemporalReviewGroupMember,
    GmailTemporalReviewHypothesis,
    GmailTemporalReviewProjection,
    gmail_temporal_review_grouping_policy_fingerprint,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.util import slugify


PIPELINE_SCOPE = "gmail-temporal-review/v1"
ACCOUNT_KEY = "personal@example.test"
THREAD_ID = "thread-1"
SOURCE_REVISION = "e" * 64
MESSAGE_ONE_ID = "message-1"
MESSAGE_TWO_ID = "message-2"
MESSAGE_ONE_AT = "2026-07-22T12:00:00-07:00"
MESSAGE_TWO_AT = "2026-07-23T08:30:00-07:00"
MESSAGE_ONE_EVIDENCE = "one trusted Gmail message"
MESSAGE_TWO_EVIDENCE = "another trusted Gmail message"
SOURCE_SHA256 = hashlib.sha256(MESSAGE_ONE_EVIDENCE.encode("utf-8")).hexdigest()
SECOND_SOURCE_SHA256 = hashlib.sha256(MESSAGE_TWO_EVIDENCE.encode("utf-8")).hexdigest()
_THREAD_HEADING = "# Email thread: Mail"
_MESSAGE_ONE = (
    f"## Message 1 — {MESSAGE_ONE_AT} — {MESSAGE_ONE_ID}\n\n"
    "From: sender@example.test\n"
    "To: personal@example.test\n"
    "Direction: incoming (test)\n\n"
    f"{MESSAGE_ONE_EVIDENCE}"
)
_MESSAGE_TWO = (
    f"## Message 2 — {MESSAGE_TWO_AT} — {MESSAGE_TWO_ID}\n\n"
    "From: other@example.test\n"
    "To: personal@example.test\n"
    "Direction: incoming (test)\n\n"
    f"{MESSAGE_TWO_EVIDENCE}"
)
MESSAGE_ONE_START = len(_THREAD_HEADING) + 2
MESSAGE_ONE_END = MESSAGE_ONE_START + len(_MESSAGE_ONE)
MESSAGE_TWO_START = MESSAGE_ONE_END + 2
MESSAGE_TWO_END = MESSAGE_TWO_START + len(_MESSAGE_TWO)
_BODY = f"{_THREAD_HEADING}\n\n{_MESSAGE_ONE}\n\n{_MESSAGE_TWO}"
_MESSAGE_TIMESTAMPS = [
    {
        "message_id": MESSAGE_ONE_ID,
        "internal_date": MESSAGE_ONE_AT,
        "start_offset": MESSAGE_ONE_START,
        "end_offset": MESSAGE_ONE_END,
    },
    {
        "message_id": MESSAGE_TWO_ID,
        "internal_date": MESSAGE_TWO_AT,
        "start_offset": MESSAGE_TWO_START,
        "end_offset": MESSAGE_TWO_END,
    },
]
_MARKDOWN = "\n".join(
    (
        "---",
        "title: Mail",
        "source_type: gmail_thread",
        f"gmail_account_key: {ACCOUNT_KEY}",
        f"gmail_thread_id: {THREAD_ID}",
        f"gmail_source_revision: {SOURCE_REVISION}",
        f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
        f"gmail_message_ids: {json.dumps([MESSAGE_ONE_ID, MESSAGE_TWO_ID])}",
        "gmail_message_timestamps_version: 1",
        f"gmail_message_timestamps: {json.dumps(_MESSAGE_TIMESTAMPS)}",
        "retained_message_count: 2",
        "---",
        _BODY,
        "",
    )
)
DOCUMENT_HASH = hashlib.sha256(_MARKDOWN.encode("utf-8")).hexdigest()


def _workspace(tmp_path: Path) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_db(paths.sqlite_path)
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT_KEY,
        thread_id=THREAD_ID,
        source_revision=SOURCE_REVISION,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    source_path = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_MARKDOWN, encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', 'Mail', ?, ?, ?, ?, ?, '[]', 'active')
            """,
            (
                "doc-gmail-1",
                str(source_path),
                str(paths.raw / "gmail-knowledge" / "missing.md"),
                DOCUMENT_HASH,
                "2026-07-22T19:00:00+00:00",
                "2026-07-22T19:01:00+00:00",
            ),
        )
    return paths


def _source(
    *,
    document_hash: str = DOCUMENT_HASH,
    account_key: str = ACCOUNT_KEY,
    thread_id: str = THREAD_ID,
    source_revision: str = SOURCE_REVISION,
    message_id: str = MESSAGE_ONE_ID,
    message_internal_at: str = MESSAGE_ONE_AT,
    message_start_offset: int = MESSAGE_ONE_START,
    message_end_offset: int = MESSAGE_ONE_END,
    source_sha256: str = SOURCE_SHA256,
) -> GmailTemporalSourceLocator:
    return GmailTemporalSourceLocator(
        document_id="doc-gmail-1",
        document_content_hash=document_hash,
        gmail_account_key=account_key,
        gmail_thread_id=thread_id,
        gmail_source_revision=source_revision,
        gmail_message_id=message_id,
        message_internal_at=message_internal_at,
        message_start_offset=message_start_offset,
        message_end_offset=message_end_offset,
        source_sha256=source_sha256,
    )


def _projection(
    *, suffix: str = "a", source_sha256: str = SOURCE_SHA256
) -> GmailTemporalReviewProjection:
    supported_hypothesis = GmailTemporalReviewHypothesis(
        version="gmail_temporal_review_hypothesis_v1",
        hypothesis_id=_hypothesis_id(
            expression_id=f"expression-supported-{suffix}",
            relation="scheduled_for",
            kind="planned",
            lifecycle="scheduled",
            normalized_value="2026-08-01T17:00:00+00:00",
        ),
        expression_id=f"expression-supported-{suffix}",
        subject_mention_ids=(f"subject-supported-{suffix}",),
        lifecycle_mention_ids=(f"lifecycle-supported-{suffix}",),
        relation="scheduled_for",
        kind="planned",
        lifecycle="scheduled",
        normalized_value="2026-08-01T17:00:00+00:00",
        candidate_ids=(f"candidate-supported-{suffix}",),
        candidate_requires_defer=False,
    )
    uncertain_hypothesis = GmailTemporalReviewHypothesis(
        version="gmail_temporal_review_hypothesis_v1",
        hypothesis_id=_hypothesis_id(
            expression_id=f"expression-uncertain-{suffix}",
            relation="occurrence",
            kind="actual",
            lifecycle="unknown",
            normalized_value=None,
        ),
        expression_id=f"expression-uncertain-{suffix}",
        subject_mention_ids=(f"subject-uncertain-{suffix}",),
        lifecycle_mention_ids=(),
        relation="occurrence",
        kind="actual",
        lifecycle="unknown",
        normalized_value=None,
        candidate_ids=(f"candidate-uncertain-{suffix}",),
        candidate_requires_defer=True,
    )
    supported = GmailTemporalReviewArtifact(
        version="gmail_temporal_review_artifact_v1",
        artifact_id=f"supported:candidate-supported-{suffix}",
        kind="supported_citation",
        evidence_status="supported",
        batch_fingerprint=f"batch-supported-{suffix}",
        frontier_fingerprint=f"frontier-supported-{suffix}",
        parent_cluster_id=f"cluster-supported-{suffix}",
        candidate_ids=(f"candidate-supported-{suffix}",),
        hypotheses=(supported_hypothesis,),
    )
    uncertain = GmailTemporalReviewArtifact(
        version="gmail_temporal_review_artifact_v1",
        artifact_id=f"uncertainty:cluster-uncertain-{suffix}",
        kind="uncertainty_sidecar",
        evidence_status="uncertain",
        batch_fingerprint=f"batch-uncertain-{suffix}",
        frontier_fingerprint=f"frontier-uncertain-{suffix}",
        parent_cluster_id=f"cluster-uncertain-{suffix}",
        candidate_ids=(f"candidate-uncertain-{suffix}",),
        hypotheses=(uncertain_hypothesis,),
    )
    cluster_review = GmailTemporalReviewClusterReview(
        version="gmail_temporal_review_cluster_review_v1",
        review_id=f"cluster_review:cluster-split-{suffix}",
        batch_fingerprint=f"batch-split-{suffix}",
        frontier_fingerprint=f"frontier-split-{suffix}",
        cluster_id=f"cluster-split-{suffix}",
        expression_id=f"expression-split-{suffix}",
        candidate_ids=(f"candidate-split-a-{suffix}", f"candidate-split-b-{suffix}"),
        reason="split_semantics_unresolved",
    )
    groups = (
        _group(
            suffix=suffix,
            name="supported",
            artifact_ids=(supported.artifact_id,),
            start=0,
        ),
        _group(
            suffix=suffix,
            name="uncertain",
            artifact_ids=(uncertain.artifact_id,),
            start=20,
        ),
        _group(
            suffix=suffix,
            name="split",
            cluster_review_ids=(cluster_review.review_id,),
            kind="split_semantics",
            state="conflicted",
            start=40,
        ),
    )
    projection = GmailTemporalReviewProjection(
        version="gmail_temporal_review_projection_v1",
        projection_fingerprint="",
        analysis_fingerprint=f"analysis-{suffix}",
        source_sha256=source_sha256,
        batch_plan_fingerprint=f"batch-plan-{suffix}",
        ensemble_policy_fingerprint=(
            gmail_temporal_candidate_ensemble_policy_fingerprint()
        ),
        grouping_policy_fingerprint=(
            gmail_temporal_review_grouping_policy_fingerprint()
        ),
        independent_invocations_verified=False,
        component_evidence_fingerprints=_component_evidence_fingerprints(suffix),
        artifacts=(supported, uncertain),
        cluster_reviews=(cluster_review,),
        groups=groups,
        complete=True,
    )
    return _fingerprinted(projection)


def _group(
    *,
    suffix: str,
    name: str,
    start: int,
    artifact_ids: tuple[str, ...] = (),
    cluster_review_ids: tuple[str, ...] = (),
    kind: str = "single",
    state: str = "present",
) -> GmailTemporalReviewGroup:
    group_id = f"group-{name}-{suffix}"
    expression_id = f"expression-{name}-{suffix}"
    role = "unresolved" if kind == "split_semantics" else "independent"
    source_order = None
    reasons = ("split_semantics_unresolved",) if kind == "split_semantics" else ()
    member_material = {
        "version": "gmail_temporal_review_group_member_v1",
        "group_id": group_id,
        "expression_id": expression_id,
        "role": role,
        "source_order": source_order,
    }
    member = GmailTemporalReviewGroupMember(
        version="gmail_temporal_review_group_member_v1",
        member_id="gtrgm_"
        + hashlib.sha256(_canonical_bytes(member_material)).hexdigest(),
        expression_id=expression_id,
        role=role,
        source_order=source_order,
        state=state,  # type: ignore[arg-type]
        artifact_ids=artifact_ids,
        cluster_review_ids=cluster_review_ids,
        subject_family_ids=(f"family-{name}-{suffix}",),
        reasons=reasons,
    )
    return GmailTemporalReviewGroup(
        version="gmail_temporal_review_group_v1",
        group_id=group_id,
        kind=kind,  # type: ignore[arg-type]
        coverage="conflicted" if kind == "split_semantics" else "complete",
        source_start=start,
        source_end=start + 10,
        subject_family_id=(
            None if kind == "split_semantics" else f"family-{name}-{suffix}"
        ),
        members=(member,),
        reasons=reasons,
    )


def _hypothesis_id(
    *,
    expression_id: str,
    relation: str,
    kind: str,
    lifecycle: str,
    normalized_value: str | None,
) -> str:
    signature = (
        expression_id,
        relation,
        kind,
        lifecycle,
        normalized_value,
    )
    material = {
        "version": "gmail_temporal_review_hypothesis_v1",
        "signature": signature,
    }
    return "gtrh_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _component_evidence_fingerprints(suffix: str) -> tuple[str, str, str]:
    first = hashlib.sha256(f"checkpoint-one-{suffix}".encode("utf-8")).hexdigest()
    third = hashlib.sha256(f"checkpoint-three-{suffix}".encode("utf-8")).hexdigest()
    return first, first, third


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _fingerprinted(
    projection: GmailTemporalReviewProjection,
) -> GmailTemporalReviewProjection:
    material = asdict(projection)
    material.pop("projection_fingerprint")
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return replace(
        projection,
        projection_fingerprint="gtrp_" + hashlib.sha256(encoded).hexdigest(),
    )


def _counts(paths: BrainPaths) -> tuple[int, int, int]:
    with connection(paths.sqlite_path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM gmail_temporal_review_runs").fetchone()[
                0
            ],
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_artifacts"
            ).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM gmail_temporal_review_heads").fetchone()[
                0
            ],
        )


def test_complete_projection_persists_once_and_groups_are_not_artifacts(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)
    projection = _projection()

    first = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=projection,
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    replay = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=projection,
        expected_head_run_id=None,
        expected_head_generation=None,
    )

    assert first.replayed is False
    assert first.head_changed is True
    assert first.artifact_count == 3
    assert replay.replayed is True
    assert replay.head_changed is False
    assert replay.run_id == first.run_id
    assert replay.head_generation == 1
    assert _counts(paths) == (1, 3, 1)
    head = get_gmail_temporal_review_head(
        paths,
        message_scope_key=first.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
    )
    assert head is not None
    assert head.source_status == "current"
    assert head.stale_reason is None
    with connection(paths.sqlite_path) as conn:
        artifacts = conn.execute(
            """
            SELECT artifact_kind, candidate_authorization, routable
            FROM gmail_temporal_review_artifacts
            ORDER BY artifact_kind
            """
        ).fetchall()
        projection_payload = json.loads(
            conn.execute(
                "SELECT projection_json FROM gmail_temporal_review_runs"
            ).fetchone()[0]
        )
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        question_count = conn.execute("SELECT COUNT(*) FROM open_questions").fetchone()[
            0
        ]
    assert [tuple(row) for row in artifacts] == [
        ("cluster_review", 0, 0),
        ("supported_citation", 1, 0),
        ("uncertainty_sidecar", 1, 0),
    ]
    assert len(projection_payload["groups"]) == 3
    assert projection_payload["independent_invocations_verified"] is False
    assert projection_payload["component_evidence_fingerprints"] == list(
        _component_evidence_fingerprints("a")
    )
    assert fact_count == 0
    assert question_count == 0


def test_same_input_key_with_different_projection_bytes_fails_closed(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)
    original = _projection()
    first = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=original,
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    hypothesis = replace(
        original.artifacts[0].hypotheses[0],
        hypothesis_id=_hypothesis_id(
            expression_id=original.artifacts[0].hypotheses[0].expression_id,
            relation=original.artifacts[0].hypotheses[0].relation,
            kind=original.artifacts[0].hypotheses[0].kind,
            lifecycle=original.artifacts[0].hypotheses[0].lifecycle,
            normalized_value="2026-08-02T17:00:00+00:00",
        ),
        normalized_value="2026-08-02T17:00:00+00:00",
    )
    changed_artifact = replace(original.artifacts[0], hypotheses=(hypothesis,))
    changed = _fingerprinted(
        replace(
            original,
            projection_fingerprint="",
            artifacts=(changed_artifact, original.artifacts[1]),
        )
    )

    with pytest.raises(GmailTemporalPersistenceConflict):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=changed,
            expected_head_run_id=first.run_id,
            expected_head_generation=1,
        )

    assert _counts(paths) == (1, 3, 1)


def test_partial_projection_and_failed_artifact_write_leave_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _workspace(tmp_path)
    projection = _projection()
    with pytest.raises(GmailTemporalPersistenceError, match="only complete"):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=replace(projection, complete=False),  # type: ignore[arg-type]
            expected_head_run_id=None,
            expected_head_generation=None,
        )
    assert _counts(paths) == (0, 0, 0)
    no_component_evidence = _fingerprinted(
        replace(
            projection,
            projection_fingerprint="",
            component_evidence_fingerprints=(),
        )
    )
    with pytest.raises(GmailTemporalPersistenceError, match="three component"):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=no_component_evidence,
            expected_head_run_id=None,
            expected_head_generation=None,
        )
    assert _counts(paths) == (0, 0, 0)

    original_insert = persistence_module._insert_artifacts

    def fail_after_artifacts(*args: object, **kwargs: object) -> None:
        original_insert(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("simulated artifact failure")

    monkeypatch.setattr(persistence_module, "_insert_artifacts", fail_after_artifacts)
    with pytest.raises(RuntimeError, match="simulated artifact failure"):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=projection,
            expected_head_run_id=None,
            expected_head_generation=None,
        )
    assert _counts(paths) == (0, 0, 0)


def test_head_cas_rollback_and_clear_retain_all_immutable_rows(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)
    first = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection(suffix="a"),
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    second = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection(suffix="b"),
        expected_head_run_id=first.run_id,
        expected_head_generation=1,
    )
    assert second.head_generation == 2

    with pytest.raises(GmailTemporalHeadConflict):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=_projection(suffix="c"),
            expected_head_run_id=first.run_id,
            expected_head_generation=1,
        )
    assert _counts(paths) == (2, 6, 1)

    rolled_back = rollback_gmail_temporal_review_head(
        paths,
        message_scope_key=first.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
        expected_run_id=second.run_id,
        expected_generation=2,
        restore_run_id=first.run_id,
    )
    assert rolled_back.current_run_id == first.run_id
    assert rolled_back.head_generation == 3
    replayed_rollback = rollback_gmail_temporal_review_head(
        paths,
        message_scope_key=first.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
        expected_run_id=second.run_id,
        expected_generation=2,
        restore_run_id=first.run_id,
    )
    assert replayed_rollback.changed is False
    assert replayed_rollback.head_generation == 3
    with pytest.raises(GmailTemporalPersistenceError, match="must precede"):
        rollback_gmail_temporal_review_head(
            paths,
            message_scope_key=first.message_scope_key,
            pipeline_scope=PIPELINE_SCOPE,
            expected_run_id=first.run_id,
            expected_generation=3,
            restore_run_id=second.run_id,
        )

    cleared = rollback_gmail_temporal_review_head(
        paths,
        message_scope_key=first.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
        expected_run_id=first.run_id,
        expected_generation=3,
    )
    assert cleared.current_run_id is None
    assert cleared.head_generation == 4
    head = get_gmail_temporal_review_head(
        paths,
        message_scope_key=first.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
    )
    assert head is not None
    assert head.run_id is None
    assert head.generation == 4
    assert head.source_status == "cleared"

    with pytest.raises(GmailTemporalHeadConflict):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(),
            pipeline_scope=PIPELINE_SCOPE,
            projection=_projection(suffix="c"),
            expected_head_run_id=None,
            expected_head_generation=None,
        )
    third = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection(suffix="c"),
        expected_head_run_id=None,
        expected_head_generation=4,
    )
    assert third.head_generation == 5
    assert _counts(paths) == (3, 9, 1)


def test_source_authority_and_append_only_triggers_fail_closed(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    with pytest.raises(GmailTemporalPersistenceError, match="content hash changed"):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(document_hash="f" * 64),
            pipeline_scope=PIPELINE_SCOPE,
            projection=_projection(),
            expected_head_run_id=None,
            expected_head_generation=None,
        )
    assert _counts(paths) == (0, 0, 0)

    result = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection(),
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    with connection(paths.sqlite_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO gmail_temporal_review_heads(
                  message_scope_key, pipeline_scope, run_id, generation, updated_at
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (
                    "gtmsg_" + "f" * 64,
                    PIPELINE_SCOPE,
                    result.run_id,
                    "2026-07-22T20:00:00+00:00",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE gmail_temporal_review_runs SET projection_json='{}' WHERE id=?",
                (result.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM gmail_temporal_review_artifacts WHERE run_id=?",
                (result.run_id,),
            )
    assert _counts(paths) == (1, 3, 1)


@pytest.mark.parametrize(
    ("source_overrides", "projection_source_sha256", "expected_error"),
    (
        (
            {
                "message_id": MESSAGE_TWO_ID,
                "message_internal_at": MESSAGE_TWO_AT,
                "message_start_offset": MESSAGE_TWO_START,
                "message_end_offset": MESSAGE_TWO_END,
            },
            SOURCE_SHA256,
            "hash does not match",
        ),
        (
            {"account_key": "other@example.test"},
            SOURCE_SHA256,
            "lineage does not match",
        ),
        ({"thread_id": "thread-2"}, SOURCE_SHA256, "lineage does not match"),
        (
            {"source_revision": "f" * 64},
            SOURCE_SHA256,
            "lineage does not match",
        ),
        (
            {
                "message_start_offset": MESSAGE_TWO_START,
                "message_end_offset": MESSAGE_TWO_END,
            },
            SOURCE_SHA256,
            "range does not match",
        ),
        (
            {"source_sha256": SECOND_SOURCE_SHA256},
            SECOND_SOURCE_SHA256,
            "hash does not match",
        ),
        (
            {"message_internal_at": MESSAGE_TWO_AT},
            SOURCE_SHA256,
            "clock does not match",
        ),
    ),
)
def test_source_locator_substitution_is_rejected_without_residue(
    tmp_path: Path,
    source_overrides: dict[str, object],
    projection_source_sha256: str,
    expected_error: str,
) -> None:
    paths = _workspace(tmp_path)
    with pytest.raises(GmailTemporalPersistenceError, match=expected_error):
        persist_gmail_temporal_review_projection(
            paths,
            source=_source(**source_overrides),  # type: ignore[arg-type]
            pipeline_scope=PIPELINE_SCOPE,
            projection=_projection(source_sha256=projection_source_sha256),
            expected_head_run_id=None,
            expected_head_generation=None,
        )
    assert _counts(paths) == (0, 0, 0)


@pytest.mark.parametrize(
    ("source_mutation", "stale_reason"),
    (
        ("supersede", "source_document_not_active"),
        ("change_document_hash", "source_authority_invalid"),
        ("change_file", "source_authority_invalid"),
    ),
)
def test_stale_head_is_explicit_and_cannot_be_restored(
    tmp_path: Path,
    source_mutation: str,
    stale_reason: str,
) -> None:
    paths = _workspace(tmp_path)
    first = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection(suffix="a"),
        expected_head_run_id=None,
        expected_head_generation=None,
    )
    second = persist_gmail_temporal_review_projection(
        paths,
        source=_source(),
        pipeline_scope=PIPELINE_SCOPE,
        projection=_projection(suffix="b"),
        expected_head_run_id=first.run_id,
        expected_head_generation=1,
    )
    with connection(paths.sqlite_path) as conn:
        if source_mutation == "supersede":
            conn.execute(
                "UPDATE documents SET status='superseded' WHERE id='doc-gmail-1'"
            )
        elif source_mutation == "change_document_hash":
            conn.execute(
                "UPDATE documents SET content_hash=? WHERE id='doc-gmail-1'",
                ("f" * 64,),
            )
        else:
            source_path = Path(
                conn.execute(
                    "SELECT source_path FROM documents WHERE id='doc-gmail-1'"
                ).fetchone()[0]
            )
            source_path.write_text(_MARKDOWN + "mutated", encoding="utf-8")

    head = get_gmail_temporal_review_head(
        paths,
        message_scope_key=first.message_scope_key,
        pipeline_scope=PIPELINE_SCOPE,
    )
    assert head is not None
    assert head.run_id == second.run_id
    assert head.source_status == "stale"
    assert head.stale_reason == stale_reason
    with pytest.raises(
        GmailTemporalPersistenceError, match="source authority is stale"
    ):
        rollback_gmail_temporal_review_head(
            paths,
            message_scope_key=first.message_scope_key,
            pipeline_scope=PIPELINE_SCOPE,
            expected_run_id=second.run_id,
            expected_generation=2,
            restore_run_id=first.run_id,
        )
    assert _counts(paths) == (2, 6, 1)
    with connection(paths.sqlite_path) as conn:
        stored_head = conn.execute(
            """
            SELECT run_id, generation FROM gmail_temporal_review_heads
            WHERE message_scope_key = ? AND pipeline_scope = ?
            """,
            (first.message_scope_key, PIPELINE_SCOPE),
        ).fetchone()
    assert tuple(stored_head) == (second.run_id, 2)
