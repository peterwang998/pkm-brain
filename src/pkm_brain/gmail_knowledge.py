from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .capture import (
    AgentLogCapture,
    AgentSessionCapture,
    CaptureResult,
    capture_state_hash,
)
from .db import connection
from .gmail_archive import (
    ArchiveOpenedMessage,
    ArchiveThreadResult,
    ArchiveThreadSnapshot,
    GmailArchiveStore,
)
from .gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GMAIL_MESSAGE_POLICY_VERSION,
    gmail_projection_session_id,
    require_gmail_projection_version,
)
from .google_normalization import (
    GMAIL_MESSAGE_BODY_CAP,
    GMAIL_THREAD_BODY_CAP,
    strip_quoted_history,
)
from .indexes import delete_vectors
from .paths import BrainPaths
from .source_dates import (
    GMAIL_SOURCE_REVISION,
    read_frontmatter,
    source_frontmatter_with_path,
    strict_int,
    trusted_gmail_message_policies,
    trusted_gmail_message_timestamps,
)
from .util import file_sha256, slugify


GMAIL_KNOWLEDGE_SOURCE_TYPE = "gmail_thread"
GMAIL_KNOWLEDGE_AGENT = "gmail"
GMAIL_KNOWLEDGE_OUTPUT_GROUP = "documents"
GMAIL_KNOWLEDGE_MIN_HUMAN_FACT_BODY_CHARS = 40
GMAIL_KNOWLEDGE_MIN_TEMPORAL_FACT_BODY_CHARS = 120
GMAIL_KNOWLEDGE_DEFAULT_BATCH_SIZE = 500
GMAIL_KNOWLEDGE_MAX_OPEN_BODY_CHARS = 4_000_000
GMAIL_KNOWLEDGE_MAX_MESSAGES = 500
GMAIL_KNOWLEDGE_TRANSACTIONAL_IMPORTANCE_CONFIDENCE = 0.95

_NO_REPLY_PATTERN = re.compile(
    r"(?:^|[<\s])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|alerts?|mailer-daemon)@",
    re.IGNORECASE,
)
_TRANSACTIONAL_SUBJECT_PATTERN = re.compile(
    r"\b(receipt|invoice|order|shipp(?:ed|ing)|delivery|verification|verify|"
    r"security alert|password|statement|payment|payout|transfer|reservation|"
    r"confirmation|confirmed|scheduled|rescheduled|cancelled|canceled|"
    r"appointment|interview|meeting|reminder|notification|daily digest|"
    r"one[- ]time code|otp|renewal|expires?|due)\b",
    re.IGNORECASE,
)
_BULK_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
_PROMOTIONAL_LABELS = {"CATEGORY_PROMOTIONS"}
_ADVERTISING_PATTERN = re.compile(
    r"\b(?:advertisement|sponsored|shop now|buy now|promo(?:tional)? code|"
    r"limited[ -]?time offer|special offer|clearance|sale ends?|"
    r"save \d{1,2}%|\d{1,2}% off|exclusive deal|reserve your (?:seat|spot)|"
    r"register now)\b",
    re.IGNORECASE,
)
_TEMPORAL_TOKEN = (
    r"(?:today|tomorrow|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"in \d+ (?:hours?|days?|weeks?))"
)
_EVENT_TEMPORAL_PATTERN = re.compile(
    rf"(?:\b(?:appointment|interview|meeting|call|consultation|session|reservation|"
    rf"booking|flight|departure|arrival|deadline|check[ -]?in)\b.{{0,120}}"
    rf"\b{_TEMPORAL_TOKEN}\b|"
    rf"\b{_TEMPORAL_TOKEN}\b.{{0,120}}\b(?:appointment|interview|meeting|call|"
    rf"consultation|session|reservation|booking|flight|departure|arrival|deadline|"
    rf"check[ -]?in)\b)",
    re.IGNORECASE | re.DOTALL,
)
_EVENT_SUBJECT_PATTERN = re.compile(
    r"\b(?:appointment|interview|meeting|call|consultation|session|reservation|"
    r"booking|flight|departure|arrival|deadline|scheduled|rescheduled|"
    r"cancelled|canceled|invitation)\b",
    re.IGNORECASE,
)
_OBLIGATION_TEMPORAL_PATTERN = re.compile(
    rf"(?:\b(?:renewal|payment|invoice|bill|due|expires?)\b.{{0,120}}\b{_TEMPORAL_TOKEN}\b|"
    rf"\b{_TEMPORAL_TOKEN}\b.{{0,120}}\b(?:renewal|payment|invoice|bill|due|expires?)\b)",
    re.IGNORECASE | re.DOTALL,
)
_OBLIGATION_SUBJECT_PATTERN = re.compile(
    r"\b(?:renewal|payment|invoice|bill|due|expires?)\b", re.IGNORECASE
)
_ACTION_REQUIRED_PATTERN = re.compile(
    r"\b(?:action required|please (?:confirm|complete|submit|pay|renew|respond|reply)|"
    r"must (?:confirm|complete|submit|pay|renew|respond|reply)|"
    r"complete by|submit by|pay by|renew by|rsvp|check[ -]?in by)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GmailImportanceAssessment:
    fact_importance: str
    actionability: str
    importance_basis: str
    importance_confidence: float
    allow_transactional_facts: bool


@dataclass(frozen=True)
class GmailMessagePolicy:
    message_id: str
    delivery_kind: str
    advertising_bases: tuple[str, ...]
    fact_admission_basis: str
    provider_important: bool
    provider_starred: bool
    human_signal_basis: str
    operator_message_after: bool


@dataclass(frozen=True)
class NormalizedGmailThread:
    account_key: str
    thread_id: str
    source_revision: str
    projection_version: int
    classifier_version: int
    provider_labels_available: bool
    human_signal_basis: str
    delivery_kind: str
    delivery_kind_basis: str
    classification: str
    classification_basis: str
    fact_importance: str
    actionability: str
    importance_basis: str
    importance_confidence: float
    fact_admission_basis: str
    fact_eligible: bool
    message_policies: tuple[GmailMessagePolicy, ...]
    created_at: str | None
    updated_at: str | None
    total_message_count: int
    message_count: int
    omitted_message_count: int
    body_chars: int
    quoted_chars_removed: int
    attachment_count: int
    truncated_message_count: int
    deleted: bool
    markdown: str


@dataclass(frozen=True)
class GmailRevisionReconciliation:
    active_documents: int
    superseded_documents: int
    deleted_documents: int
    retrieval_chunks_removed: int
    vectors_removed: int
    reactivated_documents: int = 0
    held_documents: int = 0
    retrieval_chunks_restored: int = 0
    vectors_restored: int = 0
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_documents": self.active_documents,
            "superseded_documents": self.superseded_documents,
            "deleted_documents": self.deleted_documents,
            "retrieval_chunks_removed": self.retrieval_chunks_removed,
            "vectors_removed": self.vectors_removed,
            "reactivated_documents": self.reactivated_documents,
            "held_documents": self.held_documents,
            "retrieval_chunks_restored": self.retrieval_chunks_restored,
            "vectors_restored": self.vectors_restored,
            "errors": list(self.errors),
        }


class GmailKnowledgeCapture:
    """Project encrypted local Gmail evidence into immutable Brain documents.

    This adapter never calls Gmail. It reads one already-approved encrypted archive,
    normalizes only visible correspondence, and writes immutable plaintext revisions
    into an explicitly selected Brain home.
    """

    def __init__(
        self,
        target_paths: BrainPaths,
        store: GmailArchiveStore,
        *,
        account_key: str,
        operator_email: str = "",
        batch_size: int | None = GMAIL_KNOWLEDGE_DEFAULT_BATCH_SIZE,
        projection_version: int = GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    ) -> None:
        self.target_paths = target_paths
        self.store = store
        self.account_key = account_key
        self.operator_email = operator_email.strip().casefold()
        self.batch_size = batch_size
        self.projection_version = require_gmail_projection_version(projection_version)

    def discover(self) -> list[ArchiveThreadSnapshot]:
        return list(self.store.list_thread_snapshots(self.account_key))

    def capture(
        self,
        snapshots: Sequence[ArchiveThreadSnapshot],
        *,
        dry_run: bool = False,
        export_outbox: bool = False,
    ) -> CaptureResult:
        existing, repair_required, raw_artifacts_repaired = (
            self._captured_revision_state(repair_raw=not dry_run)
        )
        accepted_snapshots: list[ArchiveThreadSnapshot] = []
        lineage_errors: list[str] = []
        for snapshot in snapshots:
            try:
                _require_snapshot_lineage(snapshot, self.account_key)
            except ValueError as exc:
                lineage_errors.append(_safe_gmail_thread_error(snapshot, exc))
            else:
                accepted_snapshots.append(snapshot)
        changed = [
            snapshot
            for snapshot in accepted_snapshots
            if self._capture_id(snapshot) not in existing
        ]
        changed.sort(
            key=lambda item: (
                str(item.archive_updated_at or ""),
                item.thread_id,
            ),
            reverse=True,
        )
        selected = (
            changed[: self.batch_size] if self.batch_size is not None else changed
        )
        deferred = max(0, len(changed) - len(selected))
        output = CaptureResult(
            discovered=len(snapshots),
            skipped=len(accepted_snapshots) - len(changed) + deferred,
            errors=lineage_errors,
        )
        if raw_artifacts_repaired:
            output.warnings.append(
                "Repaired "
                f"{raw_artifacts_repaired} corrupted Gmail raw evidence artifact(s) "
                "from their validated inbox projections."
            )
        if export_outbox:
            output.warnings.append(
                "Gmail Knowledge revisions stay local and were not exported to the sync outbox."
            )
        if deferred:
            output.warnings.append(
                f"Deferred {deferred} changed Gmail thread revision(s) to a later bounded run."
            )
        writer = AgentLogCapture(self.target_paths)
        for snapshot in selected:
            try:
                capture_id = self._capture_id(snapshot)
                if dry_run:
                    session = self._session(snapshot, markdown="")
                    if capture_id in repair_required:
                        output.captured += 1
                        output.artifacts.append(str(self._projection_path(session)))
                        continue
                else:
                    normalized = self._normalize_snapshot(snapshot)
                    if normalized is None:
                        output.skipped += 1
                        output.warnings.append(
                            f"{_safe_gmail_thread_ref(snapshot)} changed during capture; "
                            "it will retry."
                        )
                        continue
                    session = self._session(snapshot, markdown=normalized.markdown)
                    if capture_id in repair_required:
                        self._mark_capture_for_repair(capture_id)
                captured = writer.capture_sessions(
                    [session],
                    dry_run=dry_run,
                    export_outbox=False,
                )
                output.captured += captured.captured
                output.errors.extend(captured.errors)
                output.warnings.extend(captured.warnings)
                output.artifacts.extend(captured.artifacts)
            except (
                Exception
            ) as exc:  # isolate one malformed or concurrently changed thread
                output.errors.append(_safe_gmail_thread_error(snapshot, exc))
        return output

    def _normalize_snapshot(
        self, snapshot: ArchiveThreadSnapshot
    ) -> NormalizedGmailThread | None:
        if snapshot.visible_message_count:
            opened = self.store.open_thread(
                self.account_key,
                snapshot.thread_id,
                max_messages=GMAIL_KNOWLEDGE_MAX_MESSAGES,
                max_body_chars=GMAIL_KNOWLEDGE_MAX_OPEN_BODY_CHARS,
                include_spam_trash=False,
            )
        else:
            opened = ArchiveThreadResult(
                thread_id=snapshot.thread_id,
                total_messages=0,
                messages=(),
                truncated=False,
                account_key=self.account_key,
            )
        current = self.store.get_thread_snapshot(self.account_key, snapshot.thread_id)
        if current is None or current.source_revision != snapshot.source_revision:
            return None
        return normalize_gmail_thread(
            snapshot,
            opened,
            operator_email=self.operator_email,
            projection_version=self.projection_version,
        )

    def _session(
        self, snapshot: ArchiveThreadSnapshot, *, markdown: str
    ) -> AgentSessionCapture:
        _require_snapshot_lineage(snapshot, self.account_key)
        session_id = gmail_revision_session_id(
            snapshot, projection_version=self.projection_version
        )
        return AgentSessionCapture(
            agent=GMAIL_KNOWLEDGE_AGENT,
            session_id=session_id,
            title=f"Gmail thread {snapshot.thread_id}",
            source_path=self.store.db_path,
            source_hash=(
                f"gmail-projection-v{self.projection_version}:"
                f"{snapshot.source_revision}"
            ),
            source_mtime=_timestamp(snapshot.archive_updated_at),
            source_size=snapshot.raw_size,
            markdown=markdown,
            source_kind=GMAIL_KNOWLEDGE_SOURCE_TYPE,
            output_group=GMAIL_KNOWLEDGE_OUTPUT_GROUP,
        )

    def _capture_id(self, snapshot: ArchiveThreadSnapshot) -> str:
        session_id = gmail_revision_session_id(
            snapshot, projection_version=self.projection_version
        )
        return f"{GMAIL_KNOWLEDGE_AGENT}:{session_id}"

    def _projection_path(self, session: AgentSessionCapture) -> Path:
        return (
            self.target_paths.inbox
            / session.output_group
            / session.agent
            / f"{slugify(session.session_id)}.md"
        )

    def _captured_revision_state(
        self,
        *,
        repair_raw: bool,
    ) -> tuple[set[str], set[str], int]:
        valid: set[str] = set()
        repair_required: set[str] = set()
        raw_artifacts_repaired = 0
        with connection(self.target_paths.sqlite_path) as conn:
            ingested_documents = {
                str(row["source_path"]): dict(row)
                for row in conn.execute(
                    """
                    SELECT id, source_type, source_path, raw_path, content_hash
                    FROM documents
                    WHERE source_type = ?
                    ORDER BY ingested_at, id
                    """,
                    (GMAIL_KNOWLEDGE_SOURCE_TYPE,),
                )
            }
            captured_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT capture.id, capture.session_id, capture.source_hash,
                           capture.captured_path
                    FROM capture_sources AS capture
                    WHERE capture.agent = ?
                      AND capture.status = 'captured'
                      AND capture.session_id LIKE ?
                    """,
                    (
                        GMAIL_KNOWLEDGE_AGENT,
                        f"gmail-thread-p{self.projection_version}-%",
                    ),
                )
            ]
        if repair_raw:
            for captured_path_value, document in ingested_documents.items():
                expected_hash = str(document.get("content_hash") or "").strip()
                if self._ingested_raw_artifact_is_valid(document, expected_hash):
                    continue
                captured_path = Path(captured_path_value)
                frontmatter = read_frontmatter(captured_path)
                if trusted_gmail_message_timestamps(
                    document,
                    frontmatter,
                    captured_path,
                ) is not None and self._repair_ingested_raw_artifact(
                    captured_path,
                    document,
                    expected_hash,
                ):
                    raw_artifacts_repaired += 1
        for row in captured_rows:
            row["ingested_document"] = ingested_documents.get(
                str(row.get("captured_path") or "")
            )
            capture_id = str(row["id"])
            projection_valid, raw_repaired = self._captured_projection_is_valid(
                row,
                repair_raw=repair_raw,
            )
            raw_artifacts_repaired += int(raw_repaired)
            if projection_valid:
                valid.add(capture_id)
            else:
                repair_required.add(capture_id)
        return valid, repair_required, raw_artifacts_repaired

    def _captured_projection_is_valid(
        self,
        row: dict[str, Any],
        *,
        repair_raw: bool,
    ) -> tuple[bool, bool]:
        captured_path = Path(str(row.get("captured_path") or ""))
        session_id = str(row.get("session_id") or "").strip()
        if not session_id or not captured_path.is_file() or captured_path.is_symlink():
            return False, False
        expected_path = (
            self.target_paths.inbox
            / GMAIL_KNOWLEDGE_OUTPUT_GROUP
            / GMAIL_KNOWLEDGE_AGENT
            / f"{slugify(session_id)}.md"
        )
        if captured_path != expected_path:
            return False, False
        try:
            markdown = captured_path.read_text(encoding="utf-8")
            content_hash = file_sha256(captured_path)
        except (OSError, UnicodeError):
            return False, False
        if not markdown.endswith("\n") or "\n---\n\n# Email thread:" not in markdown:
            return False, False
        ingested_document = row.get("ingested_document")
        ingested_hash = (
            str(ingested_document.get("content_hash") or "").strip()
            if isinstance(ingested_document, dict)
            else ""
        )
        if ingested_hash and ingested_hash != content_hash:
            return False, False
        frontmatter = read_frontmatter(captured_path)
        account_key = str(frontmatter.get("gmail_account_key") or "").strip()
        thread_id = str(frontmatter.get("gmail_thread_id") or "").strip()
        source_revision = str(frontmatter.get("gmail_source_revision") or "").strip()
        projection_version = strict_int(frontmatter.get("gmail_projection_version"))
        classifier_version = strict_int(frontmatter.get("gmail_classifier_version"))
        if (
            str(frontmatter.get("source_type") or "") != GMAIL_KNOWLEDGE_SOURCE_TYPE
            or not account_key
            or not thread_id
            or not GMAIL_SOURCE_REVISION.fullmatch(source_revision)
            or projection_version != self.projection_version
            or classifier_version != GMAIL_KNOWLEDGE_CLASSIFIER_VERSION
        ):
            return False, False
        expected_session_id = gmail_projection_session_id(
            account_key=account_key,
            thread_id=thread_id,
            source_revision=source_revision,
            projection_version=projection_version,
        )
        if session_id != expected_session_id:
            return False, False
        if str(row.get("id") or "") != f"{GMAIL_KNOWLEDGE_AGENT}:{session_id}":
            return False, False
        expected_state_hash = capture_state_hash(
            f"gmail-projection-v{projection_version}:{source_revision}"
        )
        if str(row.get("source_hash") or "") != expected_state_hash:
            return False, False
        trusted_timestamps = trusted_gmail_message_timestamps(
            {
                "source_type": GMAIL_KNOWLEDGE_SOURCE_TYPE,
                "source_path": str(captured_path),
                "content_hash": content_hash,
            },
            frontmatter,
            captured_path,
        )
        if trusted_timestamps is None:
            return False, False
        if self.projection_version == GMAIL_KNOWLEDGE_PROJECTION_VERSION and (
            trusted_gmail_message_policies(
                {
                    "source_type": GMAIL_KNOWLEDGE_SOURCE_TYPE,
                    "source_path": str(captured_path),
                    "content_hash": content_hash,
                },
                frontmatter,
                captured_path,
            )
            is None
        ):
            return False, False
        if not isinstance(ingested_document, dict):
            return True, False
        if self._ingested_raw_artifact_is_valid(ingested_document, content_hash):
            return True, False
        if repair_raw and self._repair_ingested_raw_artifact(
            captured_path,
            ingested_document,
            content_hash,
        ):
            return True, True
        return False, False

    @staticmethod
    def _ingested_raw_artifact_is_valid(
        document: dict[str, Any],
        expected_hash: str,
    ) -> bool:
        raw_path = Path(str(document.get("raw_path") or ""))
        if not raw_path.is_file() or raw_path.is_symlink():
            return False
        try:
            return file_sha256(raw_path) == expected_hash
        except OSError:
            return False

    def _repair_ingested_raw_artifact(
        self,
        captured_path: Path,
        document: dict[str, Any],
        expected_hash: str,
    ) -> bool:
        raw_path = Path(str(document.get("raw_path") or ""))
        try:
            raw_root = self.target_paths.raw.resolve()
            raw_path.absolute().relative_to(raw_root)
            raw_path.parent.resolve().relative_to(raw_root)
            if file_sha256(captured_path) != expected_hash:
                return False
            raw_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(raw_path.parent, 0o700)
            descriptor, temporary_value = tempfile.mkstemp(
                prefix=f".{raw_path.name}.gmail-repair-",
                dir=raw_path.parent,
            )
            temporary = Path(temporary_value)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    with captured_path.open("rb") as source:
                        shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, raw_path)
            finally:
                temporary.unlink(missing_ok=True)
            return file_sha256(raw_path) == expected_hash
        except (OSError, ValueError):
            return False

    def _mark_capture_for_repair(self, capture_id: str) -> None:
        with connection(self.target_paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE capture_sources
                SET status = 'error',
                    error = 'Gmail projection artifact failed integrity validation'
                WHERE id = ? AND agent = ?
                """,
                (capture_id, GMAIL_KNOWLEDGE_AGENT),
            )


def gmail_revision_session_id(
    snapshot: ArchiveThreadSnapshot,
    *,
    projection_version: int = GMAIL_KNOWLEDGE_PROJECTION_VERSION,
) -> str:
    """Return a collision-resistant identity for one immutable projection.

    The opaque digest uses the full account, thread, provider revision, and render
    version. This avoids both truncated-revision collisions and filename collisions
    after ``AgentLogCapture`` slugifies otherwise similar account/thread keys.
    """

    return gmail_projection_session_id(
        account_key=snapshot.account_key,
        thread_id=snapshot.thread_id,
        source_revision=snapshot.source_revision,
        projection_version=projection_version,
    )


def _safe_gmail_thread_ref(snapshot: ArchiveThreadSnapshot) -> str:
    digest = hashlib.sha256()
    digest.update(b"pkm-brain/gmail-error-reference/v1\0")
    digest.update(str(snapshot.account_key or "").encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(str(snapshot.thread_id or "").encode("utf-8", errors="replace"))
    return f"gmail-thread-{digest.hexdigest()[:12]}"


def _safe_gmail_thread_error(
    snapshot: ArchiveThreadSnapshot,
    error: Exception,
) -> str:
    # Lower-layer exception text may contain message IDs, account lineage, or
    # sender-controlled content in addition to the known thread ID. Preserve
    # only an opaque correlation reference and the local exception class.
    return f"{_safe_gmail_thread_ref(snapshot)}: {type(error).__name__}"


def normalize_gmail_thread(
    snapshot: ArchiveThreadSnapshot,
    thread: ArchiveThreadResult,
    *,
    operator_email: str = "",
    projection_version: int = GMAIL_KNOWLEDGE_PROJECTION_VERSION,
) -> NormalizedGmailThread:
    projection_version = require_gmail_projection_version(projection_version)
    account_key = _require_thread_lineage(snapshot, thread)
    messages = list(thread.messages)
    delivery_kind, delivery_basis = classify_gmail_thread(
        messages, operator_email=operator_email
    )
    provider_labels_available = any(message.label_ids for message in messages)
    human_signal_basis = gmail_human_signal_basis(
        messages, operator_email=operator_email
    )
    normalized_bodies: list[str] = []
    quoted_removed = 0
    truncated_indexes = {
        index for index, message in enumerate(messages) if message.body_truncated
    }
    for index, message in enumerate(messages):
        body, removed = strip_quoted_history(message.body_text)
        quoted_removed += removed
        if len(body) > GMAIL_MESSAGE_BODY_CAP:
            body = (
                body[:GMAIL_MESSAGE_BODY_CAP].rstrip() + "\n\n[Message body truncated]"
            )
            truncated_indexes.add(index)
        normalized_bodies.append(body)
    normalized_bodies, thread_truncated_indexes = _cap_newest_bodies(
        normalized_bodies, GMAIL_THREAD_BODY_CAP
    )
    truncated_indexes.update(thread_truncated_indexes)
    retained_messages = [
        replace(message, body_text=body)
        for message, body in zip(messages, normalized_bodies)
    ]
    importance = assess_gmail_importance(
        retained_messages,
        delivery_kind=delivery_kind,
        operator_email=operator_email,
    )
    body_chars = sum(len(body) for body in normalized_bodies)
    deleted = snapshot.visible_message_count == 0
    omitted_message_count = max(
        int(thread.omitted_message_count),
        int(thread.total_messages) - len(messages),
        int(snapshot.visible_message_count) - len(messages),
        0,
    )
    message_delivery_kinds = [
        _message_delivery_kind(message, operator_email=operator_email)
        for message in retained_messages
    ]
    has_human_message = "human" in message_delivery_kinds
    human_candidate_messages = [
        message
        for message, kind in zip(retained_messages, message_delivery_kinds)
        if kind == "human"
        or (
            kind == "unknown"
            and has_human_message
            and not _message_is_advertising(message, operator_email=operator_email)
        )
    ]
    human_candidate = has_human_message
    transactional_temporal_candidate = importance.allow_transactional_facts
    temporal_candidate_messages = [
        message
        for message in retained_messages
        if transactional_temporal_candidate
        and not _message_is_advertising(message, operator_email=operator_email)
        and _message_is_transactional_temporal_candidate(
            message, operator_email=operator_email
        )
    ]
    human_candidate_body_chars = sum(
        len(message.body_text) for message in human_candidate_messages
    )
    temporal_candidate_body_chars = sum(
        len(message.body_text) for message in temporal_candidate_messages
    )
    human_body_sufficient = (
        human_candidate_body_chars >= GMAIL_KNOWLEDGE_MIN_HUMAN_FACT_BODY_CHARS
    )
    temporal_body_sufficient = (
        temporal_candidate_body_chars >= GMAIL_KNOWLEDGE_MIN_TEMPORAL_FACT_BODY_CHARS
    )
    human_lane_eligible = human_candidate and human_body_sufficient
    temporal_lane_eligible = (
        transactional_temporal_candidate and temporal_body_sufficient
    )
    fact_eligible = bool(
        not deleted and (human_lane_eligible or temporal_lane_eligible)
    )
    human_admitted_ids = (
        {message.message_id for message in human_candidate_messages}
        if human_lane_eligible and not deleted
        else set()
    )
    temporal_admitted_ids = (
        {message.message_id for message in temporal_candidate_messages}
        if temporal_lane_eligible and not deleted
        else set()
    )
    message_human_signal_bases = [
        gmail_human_signal_basis((message,), operator_email=operator_email)
        for message in retained_messages
    ]
    message_policies = tuple(
        GmailMessagePolicy(
            message_id=message.message_id,
            delivery_kind=delivery_kind,
            advertising_bases=_message_advertising_bases(
                message, operator_email=operator_email
            ),
            fact_admission_basis=(
                "durable_human_candidate"
                if message.message_id in human_admitted_ids
                else "high_confidence_important_transactional_temporal"
                if message.message_id in temporal_admitted_ids
                else "none"
            ),
            provider_important="IMPORTANT"
            in {str(value).upper() for value in message.label_ids},
            provider_starred="STARRED"
            in {str(value).upper() for value in message.label_ids},
            human_signal_basis=message_human_signal_bases[index],
            operator_message_after=any(
                {
                    "provider_sent",
                    "operator_authored",
                }
                & set(basis.split("+"))
                for basis in message_human_signal_bases[index + 1 :]
            ),
        )
        for index, (message, delivery_kind) in enumerate(
            zip(retained_messages, message_delivery_kinds)
        )
    )
    admitted_message_ids = [
        message.message_id
        for message in retained_messages
        if message.message_id in human_admitted_ids | temporal_admitted_ids
    ]
    admitted_body_chars = sum(
        len(message.body_text)
        for message in retained_messages
        if message.message_id in human_admitted_ids | temporal_admitted_ids
    )
    fact_admission_basis = (
        "durable_human_candidate"
        if fact_eligible and human_lane_eligible
        else "high_confidence_important_transactional_temporal"
        if fact_eligible and temporal_lane_eligible
        else "deleted_or_hidden"
        if deleted
        else "advertising_excluded"
        if importance.fact_importance == "advertising"
        else "insufficient_retained_body"
        if (
            (human_candidate and not human_body_sufficient)
            or (transactional_temporal_candidate and not temporal_body_sufficient)
        )
        else "delivery_not_fact_eligible"
    )
    title = _thread_title(messages)
    created_at = snapshot.created_at or _first_message_at(messages)
    updated_at = snapshot.updated_at or _last_message_at(messages)
    attachment_count = sum(len(message.attachments) for message in messages)
    captured_at = snapshot.archive_updated_at or updated_at or created_at or ""
    body_parts = [f"# Email thread: {title}"]
    body_cursor = len(body_parts[0])
    message_timestamps: list[dict[str, Any]] = []
    if deleted:
        body_parts.append(
            "This Gmail thread is no longer available in the approved visible archive."
        )
    else:
        for index, (message, body) in enumerate(
            zip(messages, normalized_bodies), start=1
        ):
            message_at = message.internal_date or message.date_header or "unknown-time"
            message_lines = [
                f"## Message {index} — {message_at} — {message.message_id}",
                "",
                f"From: {_addresses(message.from_addresses)}",
                f"To: {_addresses(message.to_addresses)}",
            ]
            if message.cc_addresses:
                message_lines.append(f"Cc: {_addresses(message.cc_addresses)}")
            if message.subject:
                message_lines.append(f"Subject: {message.subject}")
            direction, direction_basis = _message_direction(
                message, operator_email=operator_email
            )
            message_lines.append(f"Direction: {direction} ({direction_basis})")
            if message.attachments:
                message_lines.append(
                    "Attachments: "
                    + "; ".join(
                        _attachment_descriptor(item) for item in message.attachments
                    )
                )
            if not body and index - 1 in truncated_indexes:
                rendered_body = "[Message body omitted by retention cap]"
            else:
                rendered_body = body or "[No retained text body]"
                if (
                    body
                    and index - 1 in truncated_indexes
                    and "[Message body truncated]" not in body
                ):
                    rendered_body = body.rstrip() + "\n\n[Message body truncated]"
            message_lines.extend(["", rendered_body])
            rendered_message = "\n".join(message_lines).rstrip()
            start_offset = body_cursor + 2
            end_offset = start_offset + len(rendered_message)
            message_timestamps.append(
                {
                    "message_id": message.message_id,
                    # Only Gmail's provider internalDate is an assertion clock.
                    # Date headers remain display text because a sender controls them.
                    "internal_date": message.internal_date or "",
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                }
            )
            body_parts.append(rendered_message)
            body_cursor = end_offset
    body_markdown = "\n\n".join(body_parts).rstrip()
    frontmatter = [
        "---",
        f"title: {_yaml_string(title)}",
        f"source_type: {GMAIL_KNOWLEDGE_SOURCE_TYPE}",
        "source_trust: untrusted_external",
        f"created_at: {_yaml_string(created_at or '')}",
        f"source_updated_at: {_yaml_string(updated_at or '')}",
        f"captured_at: {_yaml_string(captured_at)}",
        f"archive_updated_at: {_yaml_string(snapshot.archive_updated_at or '')}",
        f"gmail_account_key: {_yaml_string(account_key)}",
        f"gmail_thread_id: {_yaml_string(snapshot.thread_id)}",
        f"gmail_source_revision: {_yaml_string(snapshot.source_revision)}",
        f"gmail_projection_version: {projection_version}",
        f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}",
        "gmail_provider_labels_available: "
        f"{'true' if provider_labels_available else 'false'}",
        f"gmail_human_signal_basis: {_yaml_string(human_signal_basis)}",
        f"gmail_message_ids: {_yaml_list([message.message_id for message in messages])}",
        "gmail_message_timestamps_version: 1",
        f"gmail_message_timestamps: {_yaml_json(message_timestamps)}",
        f"gmail_message_policy_version: {GMAIL_MESSAGE_POLICY_VERSION}",
        f"gmail_message_policies: {_yaml_json([asdict(item) for item in message_policies])}",
        f"delivery_kind: {delivery_kind}",
        f"delivery_kind_basis: {delivery_basis}",
        # Compatibility aliases for existing reports and extraction metadata.
        f"classification: {delivery_kind}",
        f"classification_basis: {delivery_basis}",
        f"fact_importance: {importance.fact_importance}",
        f"actionability: {importance.actionability}",
        f"importance_basis: {importance.importance_basis}",
        f"importance_confidence: {importance.importance_confidence:.2f}",
        f"fact_admission_basis: {fact_admission_basis}",
        f"fact_eligible: {'true' if fact_eligible else 'false'}",
        f"gmail_fact_admitted_message_ids: {_yaml_list(admitted_message_ids)}",
        f"gmail_fact_admitted_body_chars: {admitted_body_chars}",
        f"deleted: {'true' if deleted else 'false'}",
        f"archive_message_count: {snapshot.total_message_count}",
        f"visible_message_count: {snapshot.visible_message_count}",
        f"retained_message_count: {len(messages)}",
        f"omitted_message_count: {omitted_message_count}",
        f"truncated_message_count: {len(truncated_indexes)}",
        f"retained_body_chars: {body_chars}",
        f"quoted_chars_removed: {quoted_removed}",
        f"attachment_count: {attachment_count}",
        "---",
    ]
    markdown = "\n".join(frontmatter) + "\n\n" + body_markdown + "\n"
    return NormalizedGmailThread(
        account_key=account_key,
        thread_id=snapshot.thread_id,
        source_revision=snapshot.source_revision,
        projection_version=projection_version,
        classifier_version=GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
        provider_labels_available=provider_labels_available,
        human_signal_basis=human_signal_basis,
        delivery_kind=delivery_kind,
        delivery_kind_basis=delivery_basis,
        classification=delivery_kind,
        classification_basis=delivery_basis,
        fact_importance=importance.fact_importance,
        actionability=importance.actionability,
        importance_basis=importance.importance_basis,
        importance_confidence=importance.importance_confidence,
        fact_admission_basis=fact_admission_basis,
        fact_eligible=fact_eligible,
        message_policies=message_policies,
        created_at=created_at,
        updated_at=updated_at,
        total_message_count=snapshot.total_message_count,
        message_count=len(messages),
        omitted_message_count=omitted_message_count,
        body_chars=body_chars,
        quoted_chars_removed=quoted_removed,
        attachment_count=attachment_count,
        truncated_message_count=len(truncated_indexes),
        deleted=deleted,
        markdown=markdown,
    )


def classify_gmail_thread(
    messages: Sequence[ArchiveOpenedMessage],
    *,
    operator_email: str = "",
) -> tuple[str, str]:
    """Classify delivery mechanics without deciding fact importance.

    Classification happens per message.  In particular, one message carrying the
    Gmail ``SENT`` label no longer turns an otherwise automated or list-delivered
    thread into human correspondence.
    """

    labels = {
        str(label).upper()
        for message in messages
        for label in message.label_ids
        if str(label).strip()
    }
    basis = "provider_labels_and_headers" if labels else "headers_only"
    kinds = {
        _message_delivery_kind(message, operator_email=operator_email)
        for message in messages
    }
    if not kinds:
        return "unknown", basis
    if len(kinds) == 1:
        return kinds.pop(), basis
    return "mixed", basis


def assess_gmail_importance(
    messages: Sequence[ArchiveOpenedMessage],
    *,
    delivery_kind: str | None = None,
    operator_email: str = "",
) -> GmailImportanceAssessment:
    """Assess fact importance and actionability independently of delivery kind."""

    effective_delivery = (
        delivery_kind
        or classify_gmail_thread(messages, operator_email=operator_email)[0]
    )
    classified_messages = [
        (
            message,
            _message_delivery_kind(message, operator_email=operator_email),
        )
        for message in messages
    ]
    human_messages = [
        message for message, kind in classified_messages if kind == "human"
    ]
    temporal_messages = [
        message
        for message, _ in classified_messages
        if not _message_is_advertising(message, operator_email=operator_email)
        if _message_is_transactional_temporal_candidate(
            message, operator_email=operator_email
        )
    ]
    advertising = any(
        _message_is_advertising(message, operator_email=operator_email)
        for message in messages
    )
    temporal = bool(temporal_messages)
    if temporal and effective_delivery in {"transactional", "mixed"}:
        temporal_content = "\n".join(
            value
            for message in temporal_messages
            for value in (message.subject or "", message.body_text)
            if value
        )
        confidence = GMAIL_KNOWLEDGE_TRANSACTIONAL_IMPORTANCE_CONFIDENCE
        return GmailImportanceAssessment(
            fact_importance="important_temporal",
            actionability=(
                "action_required"
                if _ACTION_REQUIRED_PATTERN.search(temporal_content)
                else "time_sensitive"
            ),
            importance_basis="explicit_transactional_schedule_or_deadline",
            importance_confidence=confidence,
            allow_transactional_facts=(
                confidence >= GMAIL_KNOWLEDGE_TRANSACTIONAL_IMPORTANCE_CONFIDENCE
            ),
        )
    if advertising and not human_messages:
        return GmailImportanceAssessment(
            fact_importance="advertising",
            actionability="promotional",
            importance_basis="provider_promotion_or_advertising_signal",
            importance_confidence=0.99,
            allow_transactional_facts=False,
        )
    if human_messages:
        human_content = "\n".join(
            value
            for message in human_messages
            for value in (message.subject or "", message.body_text)
            if value
        )
        return GmailImportanceAssessment(
            fact_importance="durable_candidate",
            actionability=(
                "action_required"
                if _ACTION_REQUIRED_PATTERN.search(human_content)
                else "informational"
            ),
            importance_basis="human_correspondence_candidate",
            importance_confidence=0.90,
            allow_transactional_facts=False,
        )
    return GmailImportanceAssessment(
        fact_importance="routine",
        actionability="informational",
        importance_basis=f"{effective_delivery}_without_explicit_important_time",
        importance_confidence=0.95,
        allow_transactional_facts=False,
    )


def _message_is_advertising(
    message: ArchiveOpenedMessage, *, operator_email: str = ""
) -> bool:
    return bool(_message_advertising_bases(message, operator_email=operator_email))


def _message_advertising_bases(
    message: ArchiveOpenedMessage, *, operator_email: str = ""
) -> tuple[str, ...]:
    if _message_delivery_kind(message, operator_email=operator_email) == "human":
        return ()
    labels = {str(label).upper() for label in message.label_ids if str(label).strip()}
    content = "\n".join((message.subject or "", message.body_text))
    bases: list[str] = []
    if labels & _PROMOTIONAL_LABELS:
        bases.append("provider_category_promotions")
    if _ADVERTISING_PATTERN.search(content):
        bases.append("content_pattern")
    return tuple(sorted(bases))


def _message_is_transactional_temporal_candidate(
    message: ArchiveOpenedMessage, *, operator_email: str = ""
) -> bool:
    if (
        _message_delivery_kind(message, operator_email=operator_email)
        != "transactional"
    ):
        return False
    subject = message.subject or ""
    content = "\n".join((subject, message.body_text))
    event = bool(_EVENT_SUBJECT_PATTERN.search(subject)) and bool(
        _EVENT_TEMPORAL_PATTERN.search(content)
    )
    obligation = (
        bool(_OBLIGATION_SUBJECT_PATTERN.search(subject))
        and bool(_OBLIGATION_TEMPORAL_PATTERN.search(content))
        and bool(_ACTION_REQUIRED_PATTERN.search(content))
    )
    return event or obligation


def gmail_human_signal_basis(
    messages: Sequence[ArchiveOpenedMessage], *, operator_email: str = ""
) -> str:
    """Return only positive, auditable evidence of human participation."""

    normalized_operator = operator_email.strip().casefold()
    labels = {
        str(label).upper()
        for message in messages
        for label in message.label_ids
        if str(label).strip()
    }
    bases: list[str] = []
    if "CATEGORY_PERSONAL" in labels:
        bases.append("provider_category_personal")
    if "SENT" in labels:
        bases.append("provider_sent")
    operator_authored = bool(normalized_operator) and any(
        normalized_operator
        in {str(address).strip().casefold() for address in message.from_addresses}
        for message in messages
    )
    if operator_authored:
        bases.append("operator_authored")
        if any(
            normalized_operator
            not in {
                str(address).strip().casefold() for address in message.from_addresses
            }
            for message in messages
        ):
            bases.append("reciprocal_thread")
    return "+".join(bases) if bases else "none"


def _message_delivery_kind(
    message: ArchiveOpenedMessage, *, operator_email: str = ""
) -> str:
    labels = {str(label).upper() for label in message.label_ids if str(label).strip()}
    precedence = str(message.precedence or "").strip().casefold()
    if (
        labels & _BULK_LABELS
        or message.list_id
        or message.list_unsubscribe
        or precedence in {"bulk", "list", "junk"}
    ):
        return "bulk"
    normalized_operator = operator_email.strip().casefold()
    operator_authored = bool(normalized_operator) and normalized_operator in {
        str(address).strip().casefold() for address in message.from_addresses
    }
    if "SENT" in labels or "CATEGORY_PERSONAL" in labels or operator_authored:
        return "human"
    auto_submitted = str(message.auto_submitted or "").strip().casefold()
    if (
        "CATEGORY_UPDATES" in labels
        or _NO_REPLY_PATTERN.search(" ".join(message.from_addresses))
        or (auto_submitted and auto_submitted != "no")
        or _TRANSACTIONAL_SUBJECT_PATTERN.search(message.subject or "")
    ):
        return "transactional"
    # Missing automation markers are not positive evidence of human authorship.
    # Legacy archive rows may lack provider labels, so they remain retrievable as
    # unknown instead of entering fact extraction by default.
    return "unknown"


def reconcile_gmail_document_revisions(
    paths: BrainPaths,
) -> GmailRevisionReconciliation:
    """Keep only the newest immutable thread revision in retrieval/extraction.

    Older documents and chunks remain in SQLite so already-admitted facts retain
    their exact source evidence, but their chunks are removed from FTS/vector search.
    """

    reactivated_ids: list[str] = []
    reactivated_chunk_ids: list[str] = []
    retrieval_chunks_restored = 0
    held_documents = 0
    with connection(paths.sqlite_path) as conn:
        documents = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, source_type, source_path, raw_path, content_hash, status
                FROM documents
                WHERE source_type = ?
                ORDER BY ingested_at, id
                """,
                (GMAIL_KNOWLEDGE_SOURCE_TYPE,),
            )
        ]
        grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        desired_status: dict[str, str] = {}
        for document in documents:
            frontmatter, frontmatter_path = source_frontmatter_with_path(document)
            account_key = str(frontmatter.get("gmail_account_key") or "").strip()
            thread_id = str(frontmatter.get("gmail_thread_id") or "").strip()
            if not _valid_gmail_revision_lineage(
                document,
                frontmatter,
                frontmatter_path,
            ):
                desired_status[str(document["id"])] = "superseded"
                continue
            grouped.setdefault((account_key, thread_id), []).append(
                (document, frontmatter)
            )

        for revisions in grouped.values():
            current_document, current_frontmatter = max(
                revisions, key=_gmail_revision_rank
            )
            for document, _frontmatter in revisions:
                desired_status[str(document["id"])] = "superseded"
            desired_status[str(current_document["id"])] = (
                "deleted" if bool(current_frontmatter.get("deleted")) else "active"
            )

        candidate_reactivated_ids = [
            str(document["id"])
            for document in documents
            if desired_status.get(str(document["id"])) == "active"
            and str(document.get("status") or "") != "active"
        ]
        reactivated_chunks_by_document: dict[str, list[dict[str, Any]]] = {
            document_id: [] for document_id in candidate_reactivated_ids
        }
        for offset in range(0, len(candidate_reactivated_ids), 500):
            batch = candidate_reactivated_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"""
                SELECT c.id, c.document_id, c.text, c.heading_path,
                       d.title, d.project, d.tags
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.document_id IN ({placeholders})
                ORDER BY c.document_id, c.chunk_index
                """,
                batch,
            ):
                reactivated_chunks_by_document[str(row["document_id"])].append(
                    dict(row)
                )
        for document_id, chunk_rows in reactivated_chunks_by_document.items():
            if chunk_rows:
                reactivated_ids.append(document_id)
            else:
                desired_status[document_id] = "superseded"
                held_documents += 1

        retired_ids = [
            str(document["id"])
            for document in documents
            if desired_status.get(str(document["id"]), str(document["status"]))
            != "active"
        ]
        retired_chunk_ids: list[str] = []
        for offset in range(0, len(retired_ids), 500):
            batch = retired_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            retired_chunk_ids.extend(
                str(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM chunks WHERE document_id IN ({placeholders})",
                    batch,
                )
            )
        indexed_retired_chunk_ids: set[str] = set()
        has_retrieval_fts = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retrieval_fts'"
            ).fetchone()
            is not None
        )
        for offset in range(0, len(retired_chunk_ids), 500):
            batch = retired_chunk_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            indexed_retired_chunk_ids.update(
                str(row["chunk_id"])
                for row in conn.execute(
                    f"SELECT chunk_id FROM chunk_fts WHERE chunk_id IN ({placeholders})",
                    batch,
                )
            )
            if has_retrieval_fts:
                indexed_retired_chunk_ids.update(
                    str(row["target_id"])
                    for row in conn.execute(
                        f"""
                        SELECT target_id
                        FROM retrieval_fts
                        WHERE kind = 'chunk' AND target_id IN ({placeholders})
                        """,
                        batch,
                    )
                )
        if indexed_retired_chunk_ids:
            from .service import delete_chunk_retrieval_fts

            indexed_ids = sorted(indexed_retired_chunk_ids)
            for offset in range(0, len(indexed_ids), 500):
                delete_chunk_retrieval_fts(conn, indexed_ids[offset : offset + 500])
        if reactivated_ids:
            from .service import (
                delete_chunk_retrieval_fts,
                insert_chunk_retrieval_fts,
            )

            reactivated_rows = [
                row
                for document_id in reactivated_ids
                for row in reactivated_chunks_by_document[document_id]
            ]
            reactivated_chunk_ids = [str(row["id"]) for row in reactivated_rows]
            for offset in range(0, len(reactivated_chunk_ids), 500):
                delete_chunk_retrieval_fts(
                    conn, reactivated_chunk_ids[offset : offset + 500]
                )
            for row in reactivated_rows:
                insert_chunk_retrieval_fts(
                    conn,
                    chunk_id=str(row["id"]),
                    title=row["title"],
                    text=row["text"],
                    heading_path=row["heading_path"],
                    project=row["project"] or "",
                    tags=row["tags"] or "[]",
                )
            retrieval_chunks_restored = len(reactivated_chunk_ids)
        for document_id, status in desired_status.items():
            conn.execute(
                "UPDATE documents SET status = ? WHERE id = ?",
                (status, document_id),
            )

    from .indexes import vector_chunk_ids

    reconciliation_errors: list[str] = []
    vector_inventory_ok = True
    try:
        current_vector_ids = vector_chunk_ids(paths.lancedb_path)
    except Exception as exc:
        current_vector_ids = set()
        vector_inventory_ok = False
        reconciliation_errors.append(
            f"Gmail vector inventory failed: {type(exc).__name__}"
        )
    retired_vector_ids = sorted(current_vector_ids.intersection(retired_chunk_ids))
    vectors_removed = 0
    try:
        for offset in range(0, len(retired_vector_ids), 500):
            vectors_removed += delete_vectors(
                paths.lancedb_path, retired_vector_ids[offset : offset + 500]
            )
    except Exception as exc:
        vector_inventory_ok = False
        reconciliation_errors.append(
            f"Gmail retired-vector purge failed: {type(exc).__name__}"
        )
    vectors_restored = 0
    if reactivated_ids and vector_inventory_ok:
        from .service import BrainService

        try:
            vector_repair = BrainService(paths).rebuild_vector_index(missing_only=True)
        except Exception:
            vector_inventory_ok = False
            reconciliation_errors.append("Gmail reactivated-vector repair failed")
        else:
            if vector_repair.get("status") == "ok":
                vectors_restored = int(vector_repair.get("vectors_written") or 0)
            else:
                vector_inventory_ok = False
                reconciliation_errors.append(
                    "Gmail reactivated-vector repair did not complete"
                )
    if reactivated_ids and not vector_inventory_ok:
        with connection(paths.sqlite_path) as conn:
            from .service import delete_chunk_retrieval_fts

            for offset in range(0, len(reactivated_chunk_ids), 500):
                delete_chunk_retrieval_fts(
                    conn, reactivated_chunk_ids[offset : offset + 500]
                )
            for document_id in reactivated_ids:
                conn.execute(
                    "UPDATE documents SET status='superseded' WHERE id=?",
                    (document_id,),
                )
                desired_status[document_id] = "superseded"
        try:
            for offset in range(0, len(reactivated_chunk_ids), 500):
                vectors_removed += delete_vectors(
                    paths.lancedb_path,
                    reactivated_chunk_ids[offset : offset + 500],
                )
        except Exception:
            pass
        held_documents += len(reactivated_ids)
        reactivated_ids = []
        retrieval_chunks_restored = 0
        vectors_restored = 0
    purged_chunk_ids = indexed_retired_chunk_ids.union(retired_vector_ids)
    return GmailRevisionReconciliation(
        active_documents=sum(status == "active" for status in desired_status.values()),
        superseded_documents=sum(
            status == "superseded" for status in desired_status.values()
        ),
        deleted_documents=sum(
            status == "deleted" for status in desired_status.values()
        ),
        retrieval_chunks_removed=len(purged_chunk_ids),
        vectors_removed=int(vectors_removed),
        reactivated_documents=len(reactivated_ids),
        held_documents=held_documents,
        retrieval_chunks_restored=retrieval_chunks_restored,
        vectors_restored=vectors_restored,
        errors=tuple(reconciliation_errors),
    )


def _require_snapshot_lineage(
    snapshot: ArchiveThreadSnapshot,
    expected_account_key: str,
) -> None:
    account_key = str(snapshot.account_key or "").strip()
    expected = str(expected_account_key or "").strip()
    if not account_key:
        raise ValueError("Gmail snapshot is missing its archive account lineage")
    if account_key != expected:
        raise ValueError(
            "Gmail snapshot account does not match the approved archive account"
        )


def _valid_gmail_revision_lineage(
    document: dict[str, Any],
    frontmatter: dict[str, Any],
    frontmatter_path: Path | None,
) -> bool:
    """Fail closed for malformed current projections before retrieval activation."""

    if str(frontmatter.get("source_type") or "") != GMAIL_KNOWLEDGE_SOURCE_TYPE:
        return False
    account_key = str(frontmatter.get("gmail_account_key") or "")
    thread_id = str(frontmatter.get("gmail_thread_id") or "")
    source_revision = str(frontmatter.get("gmail_source_revision") or "")
    if not all(
        _bounded_gmail_lineage_value(value, limit=limit)
        for value, limit in (
            (account_key, 512),
            (thread_id, 512),
            (source_revision, 512),
        )
    ):
        return False
    if _timestamp(str(frontmatter.get("archive_updated_at") or "")) is None:
        return False
    if _timestamp(str(frontmatter.get("captured_at") or "")) is None:
        return False
    raw_projection_version = frontmatter.get("gmail_projection_version")
    if raw_projection_version is None:
        # Preserve immutable evidence rendered before explicit projection versions;
        # its lineage still has bounded identifiers and valid archive clocks.
        return True
    projection_version = strict_int(raw_projection_version)
    if projection_version is None or projection_version < 1:
        return False
    if projection_version < GMAIL_KNOWLEDGE_PROJECTION_VERSION:
        return True
    if projection_version != GMAIL_KNOWLEDGE_PROJECTION_VERSION:
        return False
    if not GMAIL_SOURCE_REVISION.fullmatch(source_revision):
        return False
    if (
        strict_int(frontmatter.get("gmail_classifier_version"))
        != GMAIL_KNOWLEDGE_CLASSIFIER_VERSION
    ):
        return False
    if not isinstance(frontmatter.get("deleted"), bool) or not isinstance(
        frontmatter.get("fact_eligible"), bool
    ):
        return False
    trusted_timestamps = trusted_gmail_message_timestamps(
        document,
        frontmatter,
        frontmatter_path,
    )
    return trusted_timestamps is not None and (
        trusted_gmail_message_policies(
            document,
            frontmatter,
            frontmatter_path,
        )
        is not None
    )


def _bounded_gmail_lineage_value(value: str, *, limit: int) -> bool:
    return bool(
        value
        and value == value.strip()
        and len(value) <= limit
        and not any(character in value for character in ("\x00", "\r", "\n"))
    )


def _frontmatter_projection_version(frontmatter: dict[str, Any]) -> int:
    value = frontmatter.get("gmail_projection_version")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 1:
        return value
    return 0


def _timestamp_rank(value: Any) -> tuple[int, float, str]:
    raw = str(value or "").strip()
    parsed = _timestamp(raw)
    return (1, parsed, raw) if parsed is not None else (0, float("-inf"), raw)


def _gmail_revision_rank(
    item: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[Any, ...]:
    document, frontmatter = item
    return (
        *_timestamp_rank(frontmatter.get("archive_updated_at")),
        _frontmatter_projection_version(frontmatter),
        *_timestamp_rank(frontmatter.get("captured_at")),
        str(frontmatter.get("gmail_source_revision") or ""),
        str(document.get("id") or ""),
    )


def _require_thread_lineage(
    snapshot: ArchiveThreadSnapshot,
    thread: ArchiveThreadResult,
) -> str:
    account_key = str(snapshot.account_key or "").strip()
    if not account_key:
        raise ValueError("Gmail snapshot is missing its archive account lineage")
    if thread.account_key != account_key:
        raise ValueError("Opened Gmail thread account does not match its snapshot")
    if thread.thread_id != snapshot.thread_id:
        raise ValueError("Opened Gmail thread identity does not match its snapshot")
    for message in thread.messages:
        if message.account_key != account_key:
            raise ValueError("Opened Gmail message account does not match its thread")
        if message.thread_id != snapshot.thread_id:
            raise ValueError("Opened Gmail message thread does not match its snapshot")
    return account_key


def _cap_newest_bodies(bodies: list[str], cap: int) -> tuple[list[str], set[int]]:
    remaining = max(0, cap)
    output = ["" for _ in bodies]
    truncated: set[int] = set()
    for index in range(len(bodies) - 1, -1, -1):
        body = bodies[index]
        if len(body) <= remaining:
            output[index] = body
            remaining -= len(body)
            continue
        output[index] = body[:remaining].rstrip() if remaining else ""
        if body:
            truncated.add(index)
        remaining = 0
    return output, truncated


def _thread_title(messages: Sequence[ArchiveOpenedMessage]) -> str:
    for message in reversed(messages):
        if message.subject and message.subject.strip():
            return " ".join(message.subject.split())[:500]
    return "Email thread"


def _first_message_at(messages: Sequence[ArchiveOpenedMessage]) -> str | None:
    return next(
        (
            message.internal_date or message.date_header
            for message in messages
            if message.internal_date or message.date_header
        ),
        None,
    )


def _last_message_at(messages: Sequence[ArchiveOpenedMessage]) -> str | None:
    return next(
        (
            message.internal_date or message.date_header
            for message in reversed(messages)
            if message.internal_date or message.date_header
        ),
        None,
    )


def _message_direction(
    message: ArchiveOpenedMessage, *, operator_email: str
) -> tuple[str, str]:
    labels = {str(value).upper() for value in message.label_ids}
    if "SENT" in labels:
        return "outgoing", "gmail_sent_label"
    if operator_email and operator_email in {
        value.casefold() for value in message.from_addresses
    }:
        return "outgoing", "from_header_fallback"
    if operator_email and operator_email in {
        value.casefold() for value in (*message.to_addresses, *message.cc_addresses)
    }:
        return "incoming", "recipient_header_fallback"
    return "unknown", "insufficient_provider_metadata"


def _attachment_descriptor(value: Any) -> str:
    name = str(value.filename or "unnamed attachment").replace("\n", " ")[:500]
    kind = str(value.content_type or "application/octet-stream")[:255]
    size = f", {int(value.size)} bytes" if value.size is not None else ""
    return f"{name} ({kind}{size})"


def _addresses(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(unknown)"


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _yaml_list(values: Sequence[str]) -> str:
    return json.dumps([str(value) for value in values], ensure_ascii=True)


def _yaml_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def secure_gmail_knowledge_workspace(paths: BrainPaths) -> None:
    """Make the Brain home owner-private before normalized mail is written."""

    for directory in paths.directories():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    for database in (
        paths.sqlite_path,
        Path(f"{paths.sqlite_path}-wal"),
        Path(f"{paths.sqlite_path}-shm"),
    ):
        if database.exists():
            os.chmod(database, 0o600)
