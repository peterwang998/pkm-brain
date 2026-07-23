#!/usr/bin/env python3
"""Audit the authoritative Gmail temporal preparation path without model calls.

The audit discovers active Gmail messages from a Brain home and invokes only
``prepare_gmail_temporal_review``.  Its result is deliberately aggregate-only:
no source path, document or message identity, source hash, request fingerprint,
request payload, exception detail, or message text is returned or printed.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from pkm_brain.db import connection
from pkm_brain.gmail_temporal_runner import (
    GmailTemporalReviewPreparation,
    GmailTemporalRunnerError,
    prepare_gmail_temporal_review,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.source_dates import (
    source_frontmatter_with_path,
    trusted_gmail_message_policies,
)


AUDIT_VERSION = "gmail_temporal_runner_audit_v1"

# Runner errors are intentionally translated by exact match.  Unknown messages
# are never reflected into the report because an exception may one day contain
# private source material.
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

_VOLUME_FIELDS = ("expressions", "batches", "candidates", "pages")
_COUNT_FIELDS = (
    "active_documents",
    "documents_with_messages",
    "deleted_documents_without_messages",
    "documents_with_discovery_errors",
    "discovered_messages",
    "prepared_messages",
    "failed_messages",
    "candidate_bearing_messages",
    "prepared_messages_without_policy_strata",
    "expressions",
    "batches",
    "candidates",
    "pages",
    "requests",
    "error_count",
)


@dataclass(frozen=True)
class _Target:
    document: Mapping[str, Any]
    document_id: str
    message_id: str


@dataclass
class _Stratum:
    counts: Counter[str] = field(default_factory=Counter)
    admission: Counter[str] = field(default_factory=Counter)
    dispositions: Counter[str] = field(default_factory=Counter)

    def observe(self, preparation: GmailTemporalReviewPreparation) -> None:
        self.counts["messages"] += 1
        self.counts["candidate_bearing_messages"] += int(
            preparation.candidate_count > 0
        )
        self.counts["expressions"] += preparation.expression_count
        self.counts["batches"] += preparation.batch_count
        self.counts["candidates"] += preparation.candidate_count
        self.counts["pages"] += preparation.page_count
        self.admission[preparation.admission_basis] += 1
        self.dispositions[preparation.disposition] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(sorted(self.counts.items())),
            "admission": dict(sorted(self.admission.items())),
            "dispositions": dict(sorted(self.dispositions.items())),
        }


def _discover_targets(
    paths: BrainPaths,
) -> tuple[list[_Target], Counter[str], Counter[str]]:
    """Return private targets plus aggregate discovery counts and errors."""

    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    with connection(paths.sqlite_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM documents
            WHERE source_type = 'gmail_thread' AND status = 'active'
            ORDER BY id
            """
        ).fetchall()
    counts["active_documents"] = len(rows)
    targets: list[_Target] = []
    for row in rows:
        document = dict(row)
        frontmatter, source_path = source_frontmatter_with_path(document)
        if source_path is None or not frontmatter:
            counts["documents_with_discovery_errors"] += 1
            errors["source_metadata_unavailable"] += 1
            continue
        message_ids = frontmatter.get("gmail_message_ids")
        if (
            not isinstance(message_ids, list)
            or any(not isinstance(value, str) or not value for value in message_ids)
            or len(set(message_ids)) != len(message_ids)
        ):
            counts["documents_with_discovery_errors"] += 1
            errors["message_index_unavailable"] += 1
            continue
        if not message_ids:
            if frontmatter.get("deleted") is True:
                counts["deleted_documents_without_messages"] += 1
            else:
                counts["documents_with_discovery_errors"] += 1
                errors["message_index_unavailable"] += 1
            continue
        counts["documents_with_messages"] += 1
        document_id = str(document.get("id") or "")
        targets.extend(
            _Target(
                document=document,
                document_id=document_id,
                message_id=message_id,
            )
            for message_id in message_ids
        )
    counts["discovered_messages"] = len(targets)
    return targets, counts, errors


def _runner_error_bucket(exc: GmailTemporalRunnerError) -> str:
    return _RUNNER_ERROR_BUCKETS.get(str(exc), "unclassified_runner_error")


def _trusted_target_policy(target: _Target) -> Mapping[str, Any] | None:
    """Reload the validated policy after preparation to avoid stale strata."""

    frontmatter, source_path = source_frontmatter_with_path(dict(target.document))
    policies = trusted_gmail_message_policies(
        dict(target.document),
        frontmatter,
        source_path,
    )
    if policies is None:
        return None
    matches = [
        policy for policy in policies if policy.get("message_id") == target.message_id
    ]
    return matches[0] if len(matches) == 1 else None


def _policy_stratum_keys(policy: Mapping[str, Any]) -> dict[str, str]:
    delivery = str(policy.get("delivery_kind") or "unknown")
    advertising_bases = policy.get("advertising_bases")
    advertising = (
        "+".join(str(value) for value in advertising_bases)
        if isinstance(advertising_bases, list) and advertising_bases
        else "none"
    )
    relevance_signals: list[str] = []
    if policy.get("provider_important") is True:
        relevance_signals.append("provider_important")
    if policy.get("provider_starred") is True:
        relevance_signals.append("provider_starred")
    if policy.get("human_signal_basis") != "none":
        relevance_signals.append("human_signal")
    if policy.get("operator_message_after") is True:
        relevance_signals.append("operator_message_after")
    return {
        "delivery": delivery,
        "advertising": advertising,
        "relevance": "+".join(relevance_signals) or "none",
        "fact_basis": str(policy.get("fact_admission_basis") or "none"),
    }


def _nearest_rank(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return ordered[index]


def _volume_summary(rows: Iterable[Mapping[str, int]]) -> dict[str, Any]:
    material = list(rows)
    result: dict[str, Any] = {"message_count": len(material)}
    for field_name in _VOLUME_FIELDS:
        values = [int(row[field_name]) for row in material]
        result[field_name] = {
            "p50": _nearest_rank(values, 50),
            "p90": _nearest_rank(values, 90),
            "p95": _nearest_rank(values, 95),
            "p99": _nearest_rank(values, 99),
            "max": max(values) if values else None,
        }
    return result


def _safe_fatal_result(bucket: str) -> dict[str, Any]:
    return {
        "version": AUDIT_VERSION,
        "counts": {name: 0 for name in _COUNT_FIELDS},
        "admission": {},
        "dispositions": {},
        "error_buckets": {bucket: 1},
        "strata": {
            "delivery": {},
            "advertising": {},
            "relevance": {},
            "fact_basis": {},
        },
        "error_strata": {
            "delivery": {},
            "advertising": {},
            "relevance": {},
            "fact_basis": {},
        },
        "volume_percentiles": {
            "all_prepared_messages": _volume_summary(()),
            "candidate_bearing_messages": _volume_summary(()),
        },
        "fatal": True,
        "aggregate_only": True,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "request_payloads_printed": False,
    }


def audit_gmail_temporal_runner(home: str | Path) -> dict[str, Any]:
    """Prepare every discoverable active Gmail message and aggregate outcomes."""

    paths = BrainPaths.from_value(home)
    try:
        targets, counts, errors = _discover_targets(paths)
    except Exception:  # noqa: BLE001 - details are intentionally suppressed.
        return _safe_fatal_result("database_or_schema_unavailable")

    admissions: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    strata: dict[str, dict[str, _Stratum]] = {
        "delivery": {},
        "advertising": {},
        "relevance": {},
        "fact_basis": {},
    }
    error_strata: dict[str, dict[str, Counter[str]]] = {
        "delivery": {},
        "advertising": {},
        "relevance": {},
        "fact_basis": {},
    }
    volume_rows: list[dict[str, int]] = []
    candidate_volume_rows: list[dict[str, int]] = []

    for target in targets:
        try:
            error_policy = _trusted_target_policy(target)
        except Exception:  # noqa: BLE001 - never expose private exception detail.
            error_policy = None
        error_policy_keys = (
            _policy_stratum_keys(error_policy) if error_policy is not None else {}
        )
        try:
            preparation = prepare_gmail_temporal_review(
                paths,
                document_id=target.document_id,
                gmail_message_id=target.message_id,
            )
        except GmailTemporalRunnerError as exc:
            error_bucket = _runner_error_bucket(exc)
            counts["failed_messages"] += 1
            errors[error_bucket] += 1
            for dimension, stratum_key in error_policy_keys.items():
                error_strata[dimension].setdefault(stratum_key, Counter())[
                    error_bucket
                ] += 1
            continue
        except Exception:  # noqa: BLE001 - never expose private exception detail.
            error_bucket = "unexpected_preparation_error"
            counts["failed_messages"] += 1
            errors[error_bucket] += 1
            for dimension, stratum_key in error_policy_keys.items():
                error_strata[dimension].setdefault(stratum_key, Counter())[
                    error_bucket
                ] += 1
            continue

        counts["prepared_messages"] += 1
        counts["candidate_bearing_messages"] += int(preparation.candidate_count > 0)
        counts["expressions"] += preparation.expression_count
        counts["batches"] += preparation.batch_count
        counts["candidates"] += preparation.candidate_count
        counts["pages"] += preparation.page_count
        counts["requests"] += len(preparation.requests)
        admissions[preparation.admission_basis] += 1
        dispositions[preparation.disposition] += 1
        volume = {
            "expressions": preparation.expression_count,
            "batches": preparation.batch_count,
            "candidates": preparation.candidate_count,
            "pages": preparation.page_count,
        }
        volume_rows.append(volume)
        if preparation.candidate_count > 0:
            candidate_volume_rows.append(volume)

        try:
            policy = _trusted_target_policy(target)
        except Exception:  # noqa: BLE001 - never expose private exception detail.
            policy = None
        if policy is None:
            counts["prepared_messages_without_policy_strata"] += 1
            errors["post_prepare_policy_unavailable"] += 1
            continue
        for dimension, stratum_key in _policy_stratum_keys(policy).items():
            bucket = strata[dimension].setdefault(stratum_key, _Stratum())
            bucket.observe(preparation)

    counts["error_count"] = sum(errors.values())
    for field_name in _COUNT_FIELDS:
        counts[field_name] += 0
    return {
        "version": AUDIT_VERSION,
        "counts": dict(sorted(counts.items())),
        "admission": dict(sorted(admissions.items())),
        "dispositions": dict(sorted(dispositions.items())),
        "error_buckets": dict(sorted(errors.items())),
        "strata": {
            dimension: {
                name: value.as_dict()
                for name, value in sorted(dimension_values.items())
            }
            for dimension, dimension_values in strata.items()
        },
        "error_strata": {
            dimension: {
                name: dict(sorted(value.items()))
                for name, value in sorted(dimension_values.items())
            }
            for dimension, dimension_values in error_strata.items()
        },
        "volume_percentiles": {
            "all_prepared_messages": _volume_summary(volume_rows),
            "candidate_bearing_messages": _volume_summary(candidate_volume_rows),
        },
        "fatal": False,
        "aggregate_only": True,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "request_payloads_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit_gmail_temporal_runner(args.home)
    except Exception:  # noqa: BLE001 - stdout must remain static and content-free.
        result = _safe_fatal_result("unexpected_audit_error")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
