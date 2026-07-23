#!/usr/bin/env python3
"""Track a content-free Gmail temporal shadow canary without model or Brain writes.

``init`` freezes the active Gmail message/thread population in an owner-only,
HMAC-pseudonymized local state file. ``observe`` compares a later active
projection with that frozen state, separates messages first seen in new threads
from messages first seen in already-known threads, and runs only
``prepare_gmail_temporal_review`` for those new messages.

The report is aggregate-only. The owner-only state envelope authenticates its
complete HMAC-digest/counter body, uses a generation-bound locked update, and
never contains Gmail identities, source paths, message text, request payloads,
or source hashes. Production duration uses only the wall clock; explicit clocks
are permanently non-release. This is a structural shadow harness, not a
semantic precision/recall scorer and not a temporal verifier executor.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from pkm_brain.db import connection
from pkm_brain.gmail_temporal_runner import (
    GmailTemporalReviewPreparation,
    GmailTemporalRunnerError,
    prepare_gmail_temporal_review,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.source_dates import source_frontmatter_with_path


REPORT_VERSION = "gmail_temporal_shadow_canary_audit_v1"
STATE_VERSION = "gmail_temporal_shadow_canary_state_v2"
STATE_ENVELOPE_VERSION = "gmail_temporal_shadow_canary_state_envelope_v1"
MAX_PRIVATE_FILE_BYTES = 16 * 1024 * 1024
MESSAGE_STRATA = {"baseline", "new_thread", "existing_thread_unseen"}
PRODUCTION_CLOCK_MODE = "production_wall_clock"
TEST_CLOCK_MODE = "test_non_release"
MAX_PRODUCTION_CLOCK_SKEW = timedelta(minutes=5)

_RUNNER_ERROR_BUCKETS = {
    "document identity is invalid": "document_identity_invalid",
    "message identity is invalid": "message_identity_invalid",
    "active Gmail source authority is unavailable": "active_source_unavailable",
    "trusted Gmail source file is unavailable": "source_file_unavailable",
    "Gmail source policy version is stale": "stale_policy_version",
    "trusted Gmail message index is invalid": "message_index_invalid",
    "trusted Gmail message policy is invalid": "message_policy_invalid",
    "trusted Gmail message identity is unavailable": "message_identity_unavailable",
    "trusted Gmail message policy is unavailable": "message_policy_unavailable",
    "trusted Gmail assertion clock is unavailable": "assertion_clock_unavailable",
    "trusted Gmail source could not be read": "source_read_failed",
    "trusted Gmail source changed during preparation": "source_changed",
    "trusted Gmail selector input is invalid": "selector_input_invalid",
    "Gmail target message policy is invalid": "target_policy_invalid",
    "temporal analysis authority is incomplete": "analysis_incomplete",
    "temporal batch authority is incomplete": "batch_incomplete",
    "temporal candidate frontier is incomplete": "frontier_incomplete",
    "temporal candidate authority is duplicated": "candidate_authority_duplicated",
    "temporal candidate page plan is incomplete": "page_plan_incomplete",
}


class ShadowCanaryAuditError(ValueError):
    """Raised for static, content-free shadow state or snapshot failures."""


@dataclass(frozen=True)
class _MessageTarget:
    message_key: str
    document_id: str
    message_id: str


@dataclass(frozen=True)
class _ThreadSnapshot:
    thread_key: str
    revision_key: str
    deleted: bool
    messages: tuple[_MessageTarget, ...]


def initialize_shadow_canary(
    home: str | Path,
    *,
    state_path: str | Path,
    hmac_key_path: str | Path,
    observed_at: str | None = None,
    non_release_test_clock: bool = False,
) -> dict[str, Any]:
    """Freeze a content-free baseline without preparing or persisting messages."""

    destination = Path(state_path)
    with _state_lock(destination, exclusive=True):
        return _initialize_shadow_canary_locked(
            home,
            state_path=destination,
            hmac_key_path=hmac_key_path,
            observed_at=observed_at,
            non_release_test_clock=non_release_test_clock,
        )


def _initialize_shadow_canary_locked(
    home: str | Path,
    *,
    state_path: str | Path,
    hmac_key_path: str | Path,
    observed_at: str | None,
    non_release_test_clock: bool,
) -> dict[str, Any]:
    """Initialize while holding the state-specific exclusive lock."""

    now, clock_mode = _operation_clock(
        observed_at=observed_at,
        non_release_test_clock=non_release_test_clock,
    )
    destination = Path(state_path)
    if destination.exists() or destination.is_symlink():
        raise ShadowCanaryAuditError("canary state already exists")
    key = _read_protected_file(Path(hmac_key_path), kind="HMAC key", minimum=32)
    snapshots = _snapshot(BrainPaths.from_value(home), key)
    thread_keys = sorted(item.thread_key for item in snapshots)
    message_strata = {
        target.message_key: "baseline" for item in snapshots for target in item.messages
    }
    state = {
        "version": STATE_VERSION,
        "generation": 0,
        "clock_mode": clock_mode,
        "created_at": now,
        "last_observed_at": now,
        "observation_count": 0,
        "thread_keys": thread_keys,
        "message_strata": dict(sorted(message_strata.items())),
        "prepared": {},
        "attempt_counts": {},
        "last_errors": {},
        "current_revisions": {item.thread_key: item.revision_key for item in snapshots},
        "current_deleted_thread_keys": sorted(
            item.thread_key for item in snapshots if item.deleted
        ),
    }
    _validate_state(state, key=key)
    _write_private_state(
        destination,
        state,
        key=key,
        expected_generation=None,
        expected_authenticator=None,
    )
    return {
        "version": REPORT_VERSION,
        "action": "init",
        "status": "ready",
        "baseline": {
            "current_thread_records": len(snapshots),
            "visible_messages": len(message_strata),
            "deleted_thread_records": sum(item.deleted for item in snapshots),
        },
        "clock": _clock_payload(state),
        "state_written": True,
        **_safety_contract(),
    }


def observe_shadow_canary(
    home: str | Path,
    *,
    state_path: str | Path,
    hmac_key_path: str | Path,
    observed_at: str | None = None,
    non_release_test_clock: bool = False,
) -> dict[str, Any]:
    """Prepare only messages not present at baseline or an earlier observation."""

    destination = Path(state_path)
    with _state_lock(destination, exclusive=True):
        return _observe_shadow_canary_locked(
            home,
            state_path=destination,
            hmac_key_path=hmac_key_path,
            observed_at=observed_at,
            non_release_test_clock=non_release_test_clock,
        )


def _observe_shadow_canary_locked(
    home: str | Path,
    *,
    state_path: str | Path,
    hmac_key_path: str | Path,
    observed_at: str | None,
    non_release_test_clock: bool,
) -> dict[str, Any]:
    """Observe while holding the state-specific exclusive lock."""

    now, requested_clock_mode = _operation_clock(
        observed_at=observed_at,
        non_release_test_clock=non_release_test_clock,
    )
    key = _read_protected_file(Path(hmac_key_path), kind="HMAC key", minimum=32)
    state = _load_state(Path(state_path), key=key)
    if state["clock_mode"] != requested_clock_mode:
        raise ShadowCanaryAuditError("canary clock mode cannot change")
    _validate_production_clock(state, now=now)
    if _parse_aware(now) < _parse_aware(str(state["last_observed_at"])):
        raise ShadowCanaryAuditError("observation clock moved backwards")
    expected_generation = int(state["generation"])
    expected_authenticator = _state_authenticator(state, key=key)

    paths = BrainPaths.from_value(home)
    snapshots = _snapshot(paths, key)
    previous_threads = set(state["thread_keys"])
    previous_messages = set(state["message_strata"])
    previous_revisions = dict(state["current_revisions"])
    current_threads = {item.thread_key for item in snapshots}
    current_revisions = {item.thread_key: item.revision_key for item in snapshots}

    first_seen: dict[str, str] = {}
    targets: dict[str, _MessageTarget] = {}
    current_message_keys: set[str] = set()
    new_thread_keys = {
        item.thread_key for item in snapshots if item.thread_key not in previous_threads
    }
    for item in snapshots:
        for target in item.messages:
            current_message_keys.add(target.message_key)
            targets[target.message_key] = target
            if target.message_key in previous_messages:
                continue
            stratum = (
                "new_thread"
                if item.thread_key in new_thread_keys
                else "existing_thread_unseen"
            )
            first_seen[target.message_key] = stratum
            state["message_strata"][target.message_key] = stratum

    state["thread_keys"] = sorted(previous_threads | current_threads)
    prepared = dict(state["prepared"])
    attempt_counts = dict(state["attempt_counts"])
    last_errors = dict(state["last_errors"])
    pending_keys = sorted(
        key_value
        for key_value, stratum in state["message_strata"].items()
        if stratum != "baseline"
        and key_value in current_message_keys
        and key_value not in prepared
    )

    attempts_by_stratum: dict[str, Counter[str]] = {
        "new_thread": Counter(),
        "existing_thread_unseen": Counter(),
    }
    error_buckets: Counter[str] = Counter()
    for message_key in pending_keys:
        target = targets[message_key]
        stratum = str(state["message_strata"][message_key])
        bucket = attempts_by_stratum[stratum]
        prior_attempts = int(attempt_counts.get(message_key) or 0)
        attempt_counts[message_key] = prior_attempts + 1
        bucket["attempted_messages"] += 1
        bucket["retried_messages"] += int(prior_attempts > 0)
        try:
            result = prepare_gmail_temporal_review(
                paths,
                document_id=target.document_id,
                gmail_message_id=target.message_id,
            )
        except GmailTemporalRunnerError as exc:
            error = _RUNNER_ERROR_BUCKETS.get(str(exc), "unclassified_runner_error")
            bucket["failed_messages"] += 1
            error_buckets[error] += 1
            last_errors[message_key] = error
            continue
        except Exception:  # noqa: BLE001 - never expose private exception detail.
            bucket["failed_messages"] += 1
            error_buckets["unexpected_preparation_error"] += 1
            last_errors[message_key] = "unexpected_preparation_error"
            continue
        prepared[message_key] = _preparation_record(result)
        last_errors.pop(message_key, None)
        bucket["prepared_messages"] += 1
        bucket["candidate_bearing_messages"] += int(result.candidate_count > 0)
        bucket["expressions"] += result.expression_count
        bucket["batches"] += result.batch_count
        bucket["candidates"] += result.candidate_count
        bucket["pages"] += result.page_count

    state["prepared"] = dict(sorted(prepared.items()))
    state["attempt_counts"] = dict(sorted(attempt_counts.items()))
    state["last_errors"] = dict(sorted(last_errors.items()))
    state["current_revisions"] = dict(sorted(current_revisions.items()))
    state["current_deleted_thread_keys"] = sorted(
        item.thread_key for item in snapshots if item.deleted
    )
    state["last_observed_at"] = now
    state["observation_count"] = int(state["observation_count"]) + 1
    state["generation"] = expected_generation + 1
    _validate_state(state, key=key)
    _write_private_state(
        Path(state_path),
        state,
        key=key,
        expected_generation=expected_generation,
        expected_authenticator=expected_authenticator,
    )

    existing_threads_with_unseen = {
        item.thread_key
        for item in snapshots
        if item.thread_key not in new_thread_keys
        and any(
            first_seen.get(target.message_key) == "existing_thread_unseen"
            for target in item.messages
        )
    }
    revision_only_updates = sum(
        item.thread_key in previous_revisions
        and previous_revisions[item.thread_key] != item.revision_key
        and item.thread_key not in existing_threads_with_unseen
        for item in snapshots
    )
    report = {
        "version": REPORT_VERSION,
        "action": "observe",
        "status": "partial" if error_buckets else "ready",
        "observation": {
            "new_thread_records": len(new_thread_keys),
            "existing_threads_with_unseen_messages": len(existing_threads_with_unseen),
            "revision_only_existing_thread_updates": revision_only_updates,
            "threads_missing_since_prior_observation": len(
                set(previous_revisions) - current_threads
            ),
            "newly_observed_messages": len(first_seen),
            "current_visible_messages": len(current_message_keys),
            "current_deleted_thread_records": sum(item.deleted for item in snapshots),
        },
        "attempts": {
            name: _counter_payload(value) for name, value in attempts_by_stratum.items()
        },
        "error_buckets": dict(sorted(error_buckets.items())),
        "cumulative": _cumulative_payload(state),
        "state_written": True,
        **_safety_contract(),
    }
    return report


def shadow_canary_status(
    *, state_path: str | Path, hmac_key_path: str | Path
) -> dict[str, Any]:
    """Return only cumulative structural status from the protected state."""

    destination = Path(state_path)
    with _state_lock(destination, exclusive=False):
        key = _read_protected_file(Path(hmac_key_path), kind="HMAC key", minimum=32)
        state = _load_state(destination, key=key)
        _validate_production_clock(
            state,
            now=_aware_iso(None),
        )
    return {
        "version": REPORT_VERSION,
        "action": "status",
        "status": "ready",
        "cumulative": _cumulative_payload(state),
        "state_written": False,
        **_safety_contract(),
    }


def _snapshot(paths: BrainPaths, key: bytes) -> tuple[_ThreadSnapshot, ...]:
    try:
        with connection(paths.sqlite_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM documents
                WHERE source_type = 'gmail_thread'
                  AND status IN ('active', 'deleted')
                ORDER BY id
                """
            ).fetchall()
    except Exception as exc:
        raise ShadowCanaryAuditError(
            "Gmail projection database is unavailable"
        ) from exc

    result: list[_ThreadSnapshot] = []
    thread_keys: set[str] = set()
    message_keys: set[str] = set()
    for row in rows:
        document = dict(row)
        frontmatter, source_path = source_frontmatter_with_path(document)
        if source_path is None or not frontmatter:
            raise ShadowCanaryAuditError("Gmail source metadata is unavailable")
        account_key = str(frontmatter.get("gmail_account_key") or "").strip()
        thread_id = str(frontmatter.get("gmail_thread_id") or "").strip()
        revision = str(frontmatter.get("gmail_source_revision") or "").strip()
        message_ids = frontmatter.get("gmail_message_ids")
        deleted = frontmatter.get("deleted") is True
        if (
            not account_key
            or not thread_id
            or not revision
            or not isinstance(message_ids, list)
            or any(not isinstance(value, str) or not value for value in message_ids)
            or len(set(message_ids)) != len(message_ids)
            or (not deleted and not message_ids)
        ):
            raise ShadowCanaryAuditError("Gmail snapshot lineage is invalid")
        thread_key = _opaque_key(key, "thread", account_key, thread_id)
        if thread_key in thread_keys:
            raise ShadowCanaryAuditError(
                "multiple active Gmail revisions share a thread"
            )
        thread_keys.add(thread_key)
        messages: list[_MessageTarget] = []
        for message_id in message_ids:
            message_key = _opaque_key(
                key, "message", account_key, thread_id, message_id
            )
            if message_key in message_keys:
                raise ShadowCanaryAuditError(
                    "active Gmail message identity is duplicated"
                )
            message_keys.add(message_key)
            messages.append(
                _MessageTarget(
                    message_key=message_key,
                    document_id=str(document.get("id") or ""),
                    message_id=message_id,
                )
            )
        result.append(
            _ThreadSnapshot(
                thread_key=thread_key,
                revision_key=_opaque_key(key, "revision", revision),
                deleted=deleted,
                messages=tuple(messages),
            )
        )
    return tuple(result)


def _preparation_record(value: GmailTemporalReviewPreparation) -> dict[str, Any]:
    return {
        "admission_basis": value.admission_basis,
        "disposition": value.disposition,
        "expression_count": value.expression_count,
        "batch_count": value.batch_count,
        "candidate_count": value.candidate_count,
        "page_count": value.page_count,
    }


def _cumulative_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    strata = state["message_strata"]
    prepared = state["prepared"]
    attempts = state["attempt_counts"]
    last_errors = state["last_errors"]
    created = _parse_aware(str(state["created_at"]))
    observed = _parse_aware(str(state["last_observed_at"]))
    by_stratum: dict[str, dict[str, int]] = {}
    for stratum in ("new_thread", "existing_thread_unseen"):
        keys = {key for key, value in strata.items() if value == stratum}
        records = [prepared[key] for key in keys if key in prepared]
        by_stratum[stratum] = {
            "observed_messages": len(keys),
            "prepared_messages": len(records),
            "pending_messages": len(keys - set(prepared)),
            "candidate_bearing_proxy_messages": sum(
                int(int(item["candidate_count"]) > 0) for item in records
            ),
            "attempts": sum(int(attempts.get(key) or 0) for key in keys),
            "messages_with_current_error": sum(key in last_errors for key in keys),
            "expressions": sum(int(item["expression_count"]) for item in records),
            "batches": sum(int(item["batch_count"]) for item in records),
            "candidates": sum(int(item["candidate_count"]) for item in records),
            "pages": sum(int(item["page_count"]) for item in records),
        }
    observed_messages = sum(item["observed_messages"] for item in by_stratum.values())
    candidate_proxy = sum(
        item["candidate_bearing_proxy_messages"] for item in by_stratum.values()
    )
    elapsed_seconds = max(0, int((observed - created).total_seconds()))
    release_clock_eligible = state["clock_mode"] == PRODUCTION_CLOCK_MODE
    seven_days_elapsed = elapsed_seconds >= 7 * 86400
    return {
        "state_generation": int(state["generation"]),
        "observation_count": int(state["observation_count"]),
        "elapsed_seconds": elapsed_seconds,
        "clock": _clock_payload(state),
        "baseline_messages": sum(
            value == "baseline" for value in state["message_strata"].values()
        ),
        "observed_messages": observed_messages,
        "candidate_bearing_proxy_messages": candidate_proxy,
        "by_stratum": by_stratum,
        "structural_gates": {
            "seven_days_observed": release_clock_eligible and seven_days_elapsed,
            "seven_days_elapsed_diagnostic": seven_days_elapsed,
            "release_clock_eligible": release_clock_eligible,
            "three_hundred_messages_observed": observed_messages >= 300,
            "existing_thread_unseen_stratum_observed": (
                by_stratum["existing_thread_unseen"]["observed_messages"] > 0
            ),
            "all_observed_messages_prepared": all(
                item["pending_messages"] == 0 for item in by_stratum.values()
            ),
        },
        "semantic_gates": {
            "material_temporal_case_count": None,
            "twenty_material_cases_evaluable": False,
            "precision_recall_evaluable": False,
            "candidate_bearing_proxy_is_not_a_material_label": True,
        },
        "release_claim": False,
    }


def _clock_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(state["clock_mode"])
    return {
        "mode": mode,
        "release_eligible": mode == PRODUCTION_CLOCK_MODE,
        "duration_gate_evaluable": mode == PRODUCTION_CLOCK_MODE,
        "test_clock_cannot_qualify_release": mode == TEST_CLOCK_MODE,
    }


def _counter_payload(value: Counter[str]) -> dict[str, int]:
    fields = (
        "attempted_messages",
        "retried_messages",
        "prepared_messages",
        "failed_messages",
        "candidate_bearing_messages",
        "expressions",
        "batches",
        "candidates",
        "pages",
    )
    return {field: int(value[field]) for field in fields}


def _safety_contract() -> dict[str, Any]:
    return {
        "aggregate_only": True,
        "external_calls": 0,
        "gmail_provider_calls": 0,
        "brain_mutations": 0,
        "temporal_persistence_calls": 0,
        "private_content_printed": False,
        "request_payloads_printed": False,
        "semantic_precision_recall_measured": False,
    }


def _opaque_key(key: bytes, domain: str, *values: str) -> str:
    material = json.dumps(
        {"domain": domain, "values": list(values)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _read_protected_file(path: Path, *, kind: str, minimum: int) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        elif path.is_symlink():
            raise ShadowCanaryAuditError(f"{kind} is not a protected file")
        descriptor = os.open(path, flags)
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or stat.S_IMODE(initial.st_mode) != 0o600
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
            or initial.st_size < minimum
            or initial.st_size > MAX_PRIVATE_FILE_BYTES
        ):
            raise ShadowCanaryAuditError(f"{kind} is not a protected file")
        raw = b""
        while len(raw) < initial.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, initial.st_size - len(raw)))
            if not chunk:
                break
            raw += chunk
        final = os.fstat(descriptor)
    except OSError as exc:
        raise ShadowCanaryAuditError(f"{kind} could not be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) != initial.st_size or (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    ) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
        raise ShadowCanaryAuditError(f"{kind} changed while reading")
    return raw


def _load_state(path: Path, *, key: bytes) -> dict[str, Any]:
    raw = _read_protected_file(path, kind="canary state", minimum=2)
    try:
        envelope = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowCanaryAuditError("canary state is invalid") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"version", "state", "state_hmac"}
        or envelope.get("version") != STATE_ENVELOPE_VERSION
        or not isinstance(envelope.get("state"), dict)
        or not _is_digest(envelope.get("state_hmac"))
    ):
        raise ShadowCanaryAuditError("canary state envelope is invalid")
    value = envelope["state"]
    expected_hmac = _state_authenticator(value, key=key)
    if not hmac.compare_digest(str(envelope["state_hmac"]), expected_hmac):
        raise ShadowCanaryAuditError("canary state authentication failed")
    _validate_state(value, key=key)
    if raw != _canonical_bytes(envelope):
        raise ShadowCanaryAuditError("canary state is not canonical")
    return dict(value)


def _validate_state(value: Mapping[str, Any], *, key: bytes) -> None:
    expected_keys = {
        "version",
        "generation",
        "clock_mode",
        "created_at",
        "last_observed_at",
        "observation_count",
        "thread_keys",
        "message_strata",
        "prepared",
        "attempt_counts",
        "last_errors",
        "current_revisions",
        "current_deleted_thread_keys",
    }
    if set(value) != expected_keys or value.get("version") != STATE_VERSION:
        raise ShadowCanaryAuditError("canary state schema is invalid")
    if len(key) < 32:
        raise ShadowCanaryAuditError("canary state HMAC authority is invalid")
    created = _parse_aware(value.get("created_at"))
    observed = _parse_aware(value.get("last_observed_at"))
    count = value.get("observation_count")
    generation = value.get("generation")
    clock_mode = value.get("clock_mode")
    if (
        observed < created
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or generation != count
        or clock_mode not in {PRODUCTION_CLOCK_MODE, TEST_CLOCK_MODE}
    ):
        raise ShadowCanaryAuditError("canary state chronology is invalid")
    thread_keys = _digest_list(value.get("thread_keys"), "thread keys")
    deleted_keys = _digest_list(
        value.get("current_deleted_thread_keys"), "deleted thread keys"
    )
    if not set(deleted_keys) <= set(thread_keys):
        raise ShadowCanaryAuditError("canary deleted-thread state is invalid")
    mappings = {
        name: value.get(name)
        for name in (
            "message_strata",
            "prepared",
            "attempt_counts",
            "last_errors",
            "current_revisions",
        )
    }
    if any(not isinstance(item, dict) for item in mappings.values()):
        raise ShadowCanaryAuditError("canary state mappings are invalid")
    if any(not _is_digest(key_value) for key_value in value["message_strata"]):
        raise ShadowCanaryAuditError("canary message keys are invalid")
    if any(item not in MESSAGE_STRATA for item in value["message_strata"].values()):
        raise ShadowCanaryAuditError("canary message strata are invalid")
    message_keys = set(value["message_strata"])
    if not set(value["prepared"]) <= message_keys:
        raise ShadowCanaryAuditError("canary prepared-message state is invalid")
    if not set(value["attempt_counts"]) <= message_keys:
        raise ShadowCanaryAuditError("canary attempt state is invalid")
    if not set(value["last_errors"]) <= message_keys:
        raise ShadowCanaryAuditError("canary error state is invalid")
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0
        for item in value["attempt_counts"].values()
    ):
        raise ShadowCanaryAuditError("canary attempt counts are invalid")
    if any(
        not isinstance(item, str) or len(item) > 96
        for item in value["last_errors"].values()
    ):
        raise ShadowCanaryAuditError("canary error buckets are invalid")
    if any(not _is_digest(key_value) for key_value in value["current_revisions"]):
        raise ShadowCanaryAuditError("canary current thread keys are invalid")
    if not set(value["current_revisions"]) <= set(thread_keys):
        raise ShadowCanaryAuditError("canary current revision state is invalid")
    if any(not _is_digest(item) for item in value["current_revisions"].values()):
        raise ShadowCanaryAuditError("canary revision keys are invalid")
    for record in value["prepared"].values():
        if not _valid_preparation_record(record):
            raise ShadowCanaryAuditError("canary preparation state is invalid")


def _valid_preparation_record(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "admission_basis",
        "disposition",
        "expression_count",
        "batch_count",
        "candidate_count",
        "page_count",
    }:
        return False
    if not isinstance(value["admission_basis"], str) or not isinstance(
        value["disposition"], str
    ):
        return False
    return all(
        isinstance(value[field], int)
        and not isinstance(value[field], bool)
        and value[field] >= 0
        for field in (
            "expression_count",
            "batch_count",
            "candidate_count",
            "page_count",
        )
    )


def _digest_list(value: Any, field_name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or value != sorted(value)
        or len(value) != len(set(value))
        or any(not _is_digest(item) for item in value)
    ):
        raise ShadowCanaryAuditError(f"canary {field_name} are invalid")
    return value


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_private_state(
    path: Path,
    value: Mapping[str, Any],
    *,
    key: bytes,
    expected_generation: int | None,
    expected_authenticator: str | None,
) -> None:
    _validate_state(value, key=key)
    if expected_generation is None:
        if expected_authenticator is not None:
            raise ShadowCanaryAuditError("canary state compare-and-swap is invalid")
        if path.exists() or path.is_symlink():
            raise ShadowCanaryAuditError("canary state already exists")
    else:
        if expected_authenticator is None:
            raise ShadowCanaryAuditError("canary state compare-and-swap is invalid")
        current = _load_state(path, key=key)
        if (
            int(current["generation"]) != expected_generation
            or not hmac.compare_digest(
                _state_authenticator(current, key=key), expected_authenticator
            )
            or int(value["generation"]) != expected_generation + 1
        ):
            raise ShadowCanaryAuditError("canary state changed concurrently")
    envelope = {
        "version": STATE_ENVELOPE_VERSION,
        "state": dict(value),
        "state_hmac": _state_authenticator(value, key=key),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_value = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_value)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(_canonical_bytes(envelope))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _state_authenticator(value: Mapping[str, Any], *, key: bytes) -> str:
    material = b"gmail-temporal-shadow-canary-state-v2\x00" + _canonical_bytes(value)
    return hmac.new(key, material, hashlib.sha256).hexdigest()


@contextmanager
def _state_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Serialize state readers/writers using an owner-only adjacent lock file."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        elif lock_path.is_symlink():
            raise ShadowCanaryAuditError("canary state lock is not protected")
        descriptor = os.open(lock_path, flags, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise ShadowCanaryAuditError("canary state lock is not protected")
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    except OSError as exc:
        raise ShadowCanaryAuditError("canary state lock is unavailable") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _aware_iso(value: str | None) -> str:
    parsed = datetime.now(timezone.utc) if value is None else _parse_aware(value)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _operation_clock(
    *, observed_at: str | None, non_release_test_clock: bool
) -> tuple[str, str]:
    if not isinstance(non_release_test_clock, bool):
        raise ShadowCanaryAuditError("canary clock mode is invalid")
    if non_release_test_clock:
        if observed_at is None:
            raise ShadowCanaryAuditError(
                "non-release test clock requires an explicit timestamp"
            )
        return _aware_iso(observed_at), TEST_CLOCK_MODE
    if observed_at is not None:
        raise ShadowCanaryAuditError(
            "explicit canary timestamps require non-release test clock mode"
        )
    return _aware_iso(None), PRODUCTION_CLOCK_MODE


def _validate_production_clock(state: Mapping[str, Any], *, now: str) -> None:
    if state["clock_mode"] != PRODUCTION_CLOCK_MODE:
        return
    current = _parse_aware(now)
    created = _parse_aware(state["created_at"])
    observed = _parse_aware(state["last_observed_at"])
    if created > current + MAX_PRODUCTION_CLOCK_SKEW or observed > (
        current + MAX_PRODUCTION_CLOCK_SKEW
    ):
        raise ShadowCanaryAuditError("production canary clock is future-dated")


def _parse_aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ShadowCanaryAuditError("canary timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ShadowCanaryAuditError("canary timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShadowCanaryAuditError("canary timestamp is invalid")
    return parsed


def _safe_failure(action: str, bucket: str) -> dict[str, Any]:
    return {
        "version": REPORT_VERSION,
        "action": action,
        "status": "failed",
        "fatal": True,
        "error_buckets": {bucket: 1},
        "state_written": False,
        **_safety_contract(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "observe"):
        child = subparsers.add_parser(action)
        child.add_argument("--home", type=Path, required=True)
        child.add_argument("--state", type=Path, required=True)
        child.add_argument("--hmac-key", type=Path, required=True)
        child.add_argument("--as-of")
        child.add_argument(
            "--non-release-test-clock",
            action="store_true",
            help=(
                "allow --as-of only for deterministic tests; this permanently "
                "makes the state ineligible for release duration gates"
            ),
        )
    status = subparsers.add_parser("status")
    status.add_argument("--state", type=Path, required=True)
    status.add_argument("--hmac-key", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize_shadow_canary(
                args.home,
                state_path=args.state,
                hmac_key_path=args.hmac_key,
                observed_at=args.as_of,
                non_release_test_clock=args.non_release_test_clock,
            )
        elif args.action == "observe":
            result = observe_shadow_canary(
                args.home,
                state_path=args.state,
                hmac_key_path=args.hmac_key,
                observed_at=args.as_of,
                non_release_test_clock=args.non_release_test_clock,
            )
        else:
            result = shadow_canary_status(
                state_path=args.state, hmac_key_path=args.hmac_key
            )
    except ShadowCanaryAuditError:
        result = _safe_failure(args.action, "canary_contract_error")
    except Exception:  # noqa: BLE001 - stdout must remain static and content-free.
        result = _safe_failure(args.action, "unexpected_canary_error")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if result.get("status") == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
