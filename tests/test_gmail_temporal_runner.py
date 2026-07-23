from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

import pytest

import pkm_brain.gmail_temporal_runner as runner
from pkm_brain.db import connection, init_db
from pkm_brain.gmail_archive import (
    ArchiveOpenedMessage,
    ArchiveThreadResult,
    ArchiveThreadSnapshot,
)
from pkm_brain.gmail_knowledge import normalize_gmail_thread
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GMAIL_MESSAGE_POLICY_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.gmail_temporal_persistence import get_gmail_temporal_review_head
from pkm_brain.paths import BrainPaths
from pkm_brain.util import slugify


ACCOUNT = "personal@example.test"
THREAD = "thread-runner"
REVISION = "f" * 64
MESSAGE = "message-runner"
INTERNAL_AT = "2027-07-01T09:00:00-07:00"
DOCUMENT = "doc-runner"


def _workspace(
    tmp_path: Path,
    *,
    body: str = "The hiring interview is scheduled for August 14, 2027.",
    subject: str = "Hiring interview",
    admitted: bool = True,
    importance: str = "durable_candidate",
    actionability: str = "informational",
    delivery: str = "human",
    deleted: bool = False,
    advertising_bases: tuple[str, ...] = (),
    provider_important: bool = False,
    provider_starred: bool = False,
    human_signal_basis: str | None = None,
    fact_admission_basis: str | None = None,
) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_db(paths.sqlite_path)
    rendered = "\n".join(
        (
            f"## Message 1 — {INTERNAL_AT} — {MESSAGE}",
            "",
            "From: sender@example.test",
            "To: personal@example.test",
            "Direction: incoming (test)",
            f"Subject: {subject}",
            "",
            body,
        )
    )
    thread_heading = "# Email thread: Runner"
    message_start = len(thread_heading) + 2
    message_end = message_start + len(rendered)
    markdown_body = f"{thread_heading}\n\n{rendered}"
    timestamps = [
        {
            "message_id": MESSAGE,
            "internal_date": INTERNAL_AT,
            "start_offset": message_start,
            "end_offset": message_end,
        }
    ]
    effective_human_basis = human_signal_basis or (
        "provider_sent" if delivery == "human" else "none"
    )
    effective_fact_basis = fact_admission_basis or (
        "durable_human_candidate" if admitted else "none"
    )
    message_policies = [
        {
            "message_id": MESSAGE,
            "delivery_kind": delivery,
            "advertising_bases": list(advertising_bases),
            "fact_admission_basis": effective_fact_basis,
            "provider_important": provider_important,
            "provider_starred": provider_starred,
            "human_signal_basis": effective_human_basis,
            "operator_message_after": False,
        }
    ]
    frontmatter = (
        "---",
        "title: Runner",
        "source_type: gmail_thread",
        f"gmail_account_key: {ACCOUNT}",
        f"gmail_thread_id: {THREAD}",
        f"gmail_source_revision: {REVISION}",
        f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
        f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}",
        f"gmail_message_ids: {json.dumps([MESSAGE])}",
        "gmail_message_timestamps_version: 1",
        f"gmail_message_timestamps: {json.dumps(timestamps)}",
        f"gmail_message_policy_version: {GMAIL_MESSAGE_POLICY_VERSION}",
        f"gmail_message_policies: {json.dumps(message_policies)}",
        "retained_message_count: 1",
        f"gmail_fact_admitted_message_ids: {json.dumps([MESSAGE] if admitted else [])}",
        f"fact_eligible: {'true' if admitted else 'false'}",
        f"fact_importance: {importance}",
        f"actionability: {actionability}",
        f"delivery_kind: {delivery}",
        f"classification: {delivery}",
        "importance_confidence: 0.99",
        "gmail_human_signal_basis: provider_sent",
        f"deleted: {'true' if deleted else 'false'}",
        "---",
    )
    markdown = "\n".join((*frontmatter, markdown_body, ""))
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT,
        thread_id=THREAD,
        source_revision=REVISION,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    source_path = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(markdown, encoding="utf-8")
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', 'Runner', ?, ?, ?, ?, ?, '[]', 'active')
            """,
            (
                DOCUMENT,
                str(source_path),
                str(paths.raw / "missing.md"),
                content_hash,
                "2027-07-01T16:00:00+00:00",
                "2027-07-01T16:01:00+00:00",
            ),
        )
    return paths


def _components(
    tmp_path: Path,
    paths: BrainPaths,
    *,
    mutate: Callable[[dict[str, Any], int], None] | None = None,
) -> tuple[Path, ...]:
    authority = runner._build_authority(  # noqa: SLF001
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    requests = {item.page_fingerprint: item for item in authority.requests}
    component_paths: list[Path] = []
    for ordinal in range(1, 4):
        pages = []
        first = True
        for batch in authority.batches:
            frontier = runner.build_gmail_temporal_candidate_frontier(
                analysis=authority.analysis,
                batch=batch.batch,
            )
            for page in batch.page_plan.pages:
                request = requests[page.page_fingerprint]
                candidate_ids = [
                    candidate_id
                    for cluster in page.clusters
                    for candidate_id in cluster.candidate_ids
                ]
                pages.append(
                    {
                        "request_fingerprint": request.request_fingerprint,
                        "batch_fingerprint": request.batch_fingerprint,
                        "frontier_fingerprint": frontier.frontier_fingerprint,
                        "page_plan_fingerprint": batch.page_plan.plan_fingerprint,
                        "page_fingerprint": page.page_fingerprint,
                        "verdicts": [
                            {
                                "candidate_id": candidate_id,
                                "verdict": (
                                    "supported"
                                    if first and index == 0
                                    else "unsupported"
                                ),
                            }
                            for index, candidate_id in enumerate(candidate_ids)
                        ],
                    }
                )
                first = False
        value = {
            "version": runner.GMAIL_TEMPORAL_COMPONENT_VERSION,
            "run_ordinal": ordinal,
            "invocation_id": f"external-run-{ordinal}",
            "provider": runner.GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
            "model": runner.GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": runner.GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "started_at": f"2027-07-01T16:0{ordinal}:00+00:00",
            "completed_at": f"2027-07-01T16:0{ordinal}:30+00:00",
            "runner_policy_fingerprint": runner.gmail_temporal_runner_policy_fingerprint(),
            "admission_policy_fingerprint": runner.gmail_temporal_admission_policy_fingerprint(),
            "verifier_policy_fingerprint": runner.gmail_temporal_verifier_policy_fingerprint(),
            "source_sha256": authority.source.locator.source_sha256,
            "analysis_fingerprint": authority.analysis.snapshot_fingerprint,
            "batch_plan_fingerprint": authority.batch_plan.plan_fingerprint,
            "target_fingerprint": authority.target_fingerprint,
            "pages": pages,
            "complete": True,
            "routable": False,
        }
        if mutate is not None:
            mutate(value, ordinal)
        path = tmp_path / f"component-{ordinal}.json"
        path.write_bytes(runner._canonical_bytes(value) + b"\n")  # noqa: SLF001
        path.chmod(0o600)
        component_paths.append(path)
    return tuple(component_paths)


def _mixed_policy_workspace(tmp_path: Path) -> BrainPaths:
    snapshot = ArchiveThreadSnapshot(
        thread_id=THREAD,
        source_revision=REVISION,
        total_message_count=3,
        visible_message_count=3,
        deleted_message_count=0,
        hidden_message_count=0,
        created_at=INTERNAL_AT,
        updated_at="2027-07-02T09:00:00-07:00",
        archive_updated_at="2027-07-02T16:00:00+00:00",
        raw_size=2_000,
        account_key=ACCOUNT,
    )

    def message(
        message_id: str,
        *,
        labels: tuple[str, ...],
        sender: str,
        subject: str,
        body: str,
    ) -> ArchiveOpenedMessage:
        return ArchiveOpenedMessage(
            message_id=message_id,
            thread_id=THREAD,
            internal_date=INTERNAL_AT,
            date_header="Thu, 1 Jul 2027 09:00:00 -0700",
            subject=subject,
            from_addresses=(sender,),
            to_addresses=(ACCOUNT,),
            cc_addresses=(),
            label_ids=labels,
            list_id=(
                "offers.example.test" if "CATEGORY_PROMOTIONS" in labels else None
            ),
            list_unsubscribe=(
                "<mailto:leave@example.test>"
                if "CATEGORY_PROMOTIONS" in labels
                else None
            ),
            precedence=None,
            auto_submitted=None,
            body_text=body,
            attachments=(),
            account_key=ACCOUNT,
        )

    promotion_id = "message-promotion"
    interview_id = "message-interview"
    normalized = normalize_gmail_thread(
        snapshot,
        ArchiveThreadResult(
            thread_id=THREAD,
            total_messages=3,
            messages=(
                message(
                    promotion_id,
                    labels=("CATEGORY_PROMOTIONS",),
                    sender="offers@example.test",
                    subject="Limited-time offer ends August 14",
                    body="Shop now and save 25% with this promotional code. " * 4,
                ),
                message(
                    interview_id,
                    labels=("CATEGORY_FORUMS",),
                    sender="events@community.example.test",
                    subject="Hiring interview confirmed",
                    body=(
                        "The hiring interview is scheduled for August 14, 2027. "
                        "Please confirm attendance soon so the recruiting team can "
                        "finalize the video link, participant list, and instructions "
                        "for the conversation."
                    ),
                ),
                message(
                    "message-owner-reply",
                    labels=("SENT",),
                    sender=ACCOUNT,
                    subject="Re: Hiring interview confirmed",
                    body=(
                        "I will attend the hiring interview. Please keep my place on "
                        "the participant list and send the final video link."
                    ),
                ),
            ),
            truncated=False,
            account_key=ACCOUNT,
        ),
    )
    # Aggregate summaries are intentionally hostile. Target-message policy must
    # remain the sole admission/suppression authority in a mixed thread.
    markdown = normalized.markdown.replace(
        "fact_importance: durable_candidate",
        "fact_importance: advertising",
        1,
    ).replace("actionability: informational", "actionability: promotional", 1)
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_db(paths.sqlite_path)
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT,
        thread_id=THREAD,
        source_revision=REVISION,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    source_path = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(markdown, encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', 'Mixed runner', ?, ?, ?, ?, ?, '[]', 'active')
            """,
            (
                DOCUMENT,
                str(source_path),
                str(paths.raw / "missing.md"),
                hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                "2027-07-01T16:00:00+00:00",
                "2027-07-01T16:01:00+00:00",
            ),
        )
    return paths


def test_authoritative_runner_recomputes_and_persists_exact_three_runs(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)
    preparation = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )

    assert preparation.disposition == "complete_review_projection"
    assert preparation.admission_basis == "fact"
    assert preparation.candidate_count > 0
    assert preparation.page_count == len(preparation.requests)
    assert all(request.payload for request in preparation.requests)

    components = _components(tmp_path, paths)
    result = runner.run_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=components,
    )

    assert result.disposition == "complete_review_projection"
    assert result.persisted is True
    assert result.component_count == 3
    assert result.artifact_count >= 1
    assert result.private_content_printed is False
    head = get_gmail_temporal_review_head(
        paths,
        message_scope_key=result.message_scope_key,
        pipeline_scope=runner.GMAIL_TEMPORAL_PIPELINE_SCOPE,
    )
    assert head is not None and head.run_id == result.run_id
    assert head.source_status == "current"
    with connection(paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM open_questions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_executions"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_components"
            ).fetchone()[0]
            == 3
        )
        stored_components = conn.execute(
            """
            SELECT run_ordinal, payload_json
            FROM gmail_temporal_review_components ORDER BY run_ordinal
            """
        ).fetchall()
    assert [row["run_ordinal"] for row in stored_components] == [1, 2, 3]
    assert all(row["payload_json"].endswith("\n") for row in stored_components)

    replay = runner.run_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=components,
    )
    assert replay.run_id == result.run_id
    assert replay.head_generation == result.head_generation
    assert replay.execution_id == result.execution_id
    assert replay.replayed is True


def test_runner_uses_recall_rescue_but_hard_suppresses_advertising(
    tmp_path: Path,
) -> None:
    rescue_paths = _workspace(tmp_path / "rescue", admitted=False)
    rescued = runner.prepare_gmail_temporal_review(
        rescue_paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert rescued.admission_basis == "temporal_rescue"
    assert rescued.disposition == "complete_review_projection"

    advertising_paths = _workspace(
        tmp_path / "advertising",
        admitted=False,
        importance="advertising",
        actionability="promotional",
        delivery="bulk",
        advertising_bases=("provider_category_promotions",),
    )
    suppressed = runner.prepare_gmail_temporal_review(
        advertising_paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert suppressed.admission_basis == "not_admitted"
    assert suppressed.disposition == "not_admitted"
    assert suppressed.requests == ()
    result = runner.run_gmail_temporal_review(
        advertising_paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=(),
    )
    assert result.persisted is True
    assert result.component_count == 0
    with connection(advertising_paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_executions"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_components"
            ).fetchone()[0]
            == 0
        )


def test_mixed_thread_aggregate_cannot_poison_target_message_policy(
    tmp_path: Path,
) -> None:
    paths = _mixed_policy_workspace(tmp_path)

    promotion = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id="message-promotion",
    )
    interview = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id="message-interview",
    )

    assert promotion.admission_basis == "not_admitted"
    assert promotion.requests == ()
    assert interview.admission_basis == "temporal_rescue"
    assert interview.disposition == "complete_review_projection"
    assert interview.candidate_count > 0


@pytest.mark.parametrize(
    ("body", "subject"),
    [
        ("The policy takes effect August 14, 2027.", "Policy update"),
        ("Registration closes August 14, 2027.", "Registration"),
        ("The portal opens August 14, 2027.", "Portal update"),
    ],
)
def test_temporal_rescue_includes_shared_event_predicate_subjects(
    tmp_path: Path, body: str, subject: str
) -> None:
    paths = _workspace(
        tmp_path,
        body=body,
        subject=subject,
        admitted=False,
    )

    preparation = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )

    assert preparation.admission_basis == "temporal_rescue"
    assert preparation.disposition == "complete_review_projection"
    assert preparation.candidate_count > 0


def test_inconsistent_advertising_fact_membership_fails_policy_validation(
    tmp_path: Path,
) -> None:
    paths = _workspace(
        tmp_path,
        admitted=True,
        importance="advertising",
        actionability="promotional",
        delivery="bulk",
        advertising_bases=("content_pattern",),
    )

    with pytest.raises(runner.GmailTemporalRunnerError, match="policy is invalid"):
        runner.prepare_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
        )


def test_inconsistent_deleted_fact_membership_fails_policy_validation(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path, admitted=True, deleted=True)

    with pytest.raises(runner.GmailTemporalRunnerError, match="policy is invalid"):
        runner.prepare_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
        )


def test_bulk_temporal_rescue_requires_a_target_message_relevance_signal(
    tmp_path: Path,
) -> None:
    routine = _workspace(
        tmp_path / "routine",
        admitted=False,
        delivery="bulk",
    )
    suppressed = runner.prepare_gmail_temporal_review(
        routine,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert suppressed.admission_basis == "not_admitted"

    important = _workspace(
        tmp_path / "important",
        admitted=False,
        delivery="bulk",
        provider_important=True,
    )
    rescued = runner.prepare_gmail_temporal_review(
        important,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert rescued.admission_basis == "temporal_rescue"
    assert rescued.disposition == "complete_review_projection"


def test_lexical_advertising_hint_is_weak_and_star_can_override_promotions(
    tmp_path: Path,
) -> None:
    lexical = _workspace(
        tmp_path / "lexical",
        admitted=False,
        delivery="transactional",
        advertising_bases=("content_pattern",),
        provider_important=True,
    )
    lexical_review = runner.prepare_gmail_temporal_review(
        lexical,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert lexical_review.admission_basis == "temporal_rescue"

    starred_promotion = _workspace(
        tmp_path / "starred",
        admitted=False,
        delivery="bulk",
        advertising_bases=("provider_category_promotions",),
        provider_starred=True,
    )
    starred_review = runner.prepare_gmail_temporal_review(
        starred_promotion,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert starred_review.admission_basis == "temporal_rescue"


def test_zero_expression_is_an_honest_no_model_disposition(tmp_path: Path) -> None:
    paths = _workspace(tmp_path, body="Thanks for the update.")
    preparation = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    assert preparation.disposition == "no_recognized_expression"
    assert preparation.requests == ()

    result = runner.run_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=(),
    )
    assert result.disposition == "no_recognized_expression"
    assert result.component_count == 0
    assert result.persisted is True
    replay = runner.run_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=(),
    )
    assert replay.execution_id == result.execution_id
    assert replay.replayed is True
    with connection(paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_executions"
            ).fetchone()[0]
            == 1
        )


def test_expression_without_a_legal_subject_needs_no_model_call(
    tmp_path: Path,
) -> None:
    paths = _workspace(
        tmp_path,
        body="August 14, 2027.",
        subject="FYI",
    )

    preparation = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )

    assert preparation.disposition == "no_verification_candidate"
    assert preparation.expression_count == 1
    assert preparation.candidate_count == 0
    assert preparation.requests == ()
    result = runner.run_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=(),
    )
    assert result.disposition == "no_verification_candidate"
    assert result.persisted is True
    assert result.component_count == 0


def test_bounded_hint_graph_does_not_become_a_recall_gate(tmp_path: Path) -> None:
    body = "\n\n".join(f"August 14, 2027. Item {index}." for index in range(1, 66))
    paths = _workspace(tmp_path, body=body)

    authority = runner._build_authority(  # noqa: SLF001
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )

    assert authority.analysis.graph_truncated is True
    assert authority.analysis.candidate_edge_count_exact is False
    assert authority.disposition == "complete_review_projection"
    assert len(authority.requests) > 0


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_candidate_bearing_runner_requires_exactly_three_artifacts(
    tmp_path: Path, count: int
) -> None:
    paths = _workspace(tmp_path)
    components = _components(tmp_path, paths)
    supplied = components[:count] if count <= 3 else (*components, components[0])
    with pytest.raises(runner.GmailTemporalRunnerError, match="exactly three"):
        runner.run_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
            component_artifacts=tuple(supplied),
        )


def test_runner_rejects_mixed_config_duplicate_file_and_stale_source(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path / "config")
    components = _components(
        tmp_path / "config",
        paths,
        mutate=lambda value, ordinal: (
            value.__setitem__("reasoning_effort", "high") if ordinal == 2 else None
        ),
    )
    with pytest.raises(runner.GmailTemporalRunnerError, match="authority"):
        runner.run_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
            component_artifacts=components,
        )


@pytest.mark.parametrize("variant", ["mode", "symlink", "hardlink"])
def test_runner_rejects_unprotected_component_paths(
    tmp_path: Path, variant: str
) -> None:
    paths = _workspace(tmp_path)
    components = list(_components(tmp_path, paths))
    if variant == "mode":
        components[0].chmod(0o644)
    else:
        replacement = tmp_path / f"component-1-{variant}.json"
        if variant == "symlink":
            replacement.symlink_to(components[0])
        else:
            os.link(components[0], replacement)
        components[0] = replacement

    with pytest.raises(runner.GmailTemporalRunnerError, match="protected file"):
        runner.run_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
            component_artifacts=tuple(components),
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda value, ordinal: (
                value["pages"][0].__setitem__("request_fingerprint", "gtrq_stale")
                if ordinal == 2
                else None
            ),
            "fingerprints are stale",
        ),
        (
            lambda value, ordinal: (
                value["pages"][0]["verdicts"].pop() if ordinal == 2 else None
            ),
            "candidate coverage is incomplete",
        ),
        (
            lambda value, ordinal: (
                value.__setitem__("invocation_id", "external-run-1")
                if ordinal == 2
                else None
            ),
            "authority is invalid",
        ),
        (
            lambda value, ordinal: (
                value.__setitem__("run_ordinal", True) if ordinal == 1 else None
            ),
            "authority is invalid",
        ),
    ],
)
def test_runner_rejects_tampered_component_authority(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any], int], None],
    error: str,
) -> None:
    paths = _workspace(tmp_path)
    components = _components(tmp_path, paths, mutate=mutation)

    with pytest.raises(runner.GmailTemporalRunnerError, match=error):
        runner.run_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
            component_artifacts=components,
        )


def test_all_unsupported_complete_ensemble_persists_empty_projection(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)

    def reject_all(value: dict[str, Any], _ordinal: int) -> None:
        for page in value["pages"]:
            for verdict in page["verdicts"]:
                verdict["verdict"] = "unsupported"

    result = runner.run_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
        component_artifacts=_components(tmp_path, paths, mutate=reject_all),
    )

    assert result.disposition == "complete_review_projection"
    assert result.persisted is True
    assert result.artifact_count == 0
    assert result.cluster_review_count == 0

    paths = _workspace(tmp_path / "duplicate")
    components = _components(tmp_path / "duplicate", paths)
    with pytest.raises(runner.GmailTemporalRunnerError, match="distinct"):
        runner.run_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
            component_artifacts=(components[0], components[0], components[2]),
        )

    paths = _workspace(tmp_path / "stale")
    components = _components(tmp_path / "stale", paths)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET status = 'superseded' WHERE id = ?", (DOCUMENT,)
        )
    with pytest.raises(runner.GmailTemporalRunnerError, match="active Gmail"):
        runner.run_gmail_temporal_review(
            paths,
            document_id=DOCUMENT,
            gmail_message_id=MESSAGE,
            component_artifacts=components,
        )


def test_model_request_masks_gmail_secrets_without_changing_authority(
    tmp_path: Path,
) -> None:
    secret = "SECRET-OTP-729184"
    paths = _workspace(
        tmp_path,
        body=(
            "Your verification code is 729184. Project Juniper's private hiring "
            "interview is scheduled for August 14, 2027."
        ),
    )
    preparation = runner.prepare_gmail_temporal_review(
        paths,
        document_id=DOCUMENT,
        gmail_message_id=MESSAGE,
    )
    combined = "\n".join(item.payload for item in preparation.requests)
    assert "729184" not in combined
    assert "Project Juniper" in combined
    assert "Project Juniper" not in repr(preparation)
    assert "Project Juniper" not in repr(preparation.requests[0])
    assert secret not in repr(preparation)
    assert stat.S_IMODE(_components(tmp_path, paths)[0].stat().st_mode) == 0o600
