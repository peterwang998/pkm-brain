#!/usr/bin/env python3
"""Canonical post-admission stage semantics for Gmail fact parity runs.

The parity runner needs evidence-derived stage membership, not booleans supplied
by the caller.  This module is deliberately self-contained and side-effect
free so the same contract can be loaded beside both the frozen original Brain
and Brain V2 implementations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


CONTRACT_VERSION = "gmail_fact_parity_post_admission_stage_v2"
STAGES = ("candidate", "review", "persisted")
DISPOSITIONS = ("residue", "deferred", "rejected", "applied")

# These are the complete action-ledger states this version understands.  A new
# production state must result in a contract-version change, not an implicit
# best-effort classification.
ACCEPTED_ACTION_STATUSES = (
    "proposed",
    "needs_human",
    "applied",
    "auto_applied",
    "rejected",
    "dismissed",
    "failed",
    "reverted",
)
APPLIED_ACTION_STATUSES = ("applied", "auto_applied")
ACCEPTED_CRITIC_DECISIONS = (
    None,
    "agree",
    "disagree",
    "evidence_incomplete",
    "unavailable",
)
CRITIC_BLOCKING_DECISIONS = ("disagree", "evidence_incomplete", "unavailable")

# Rows with any accepted status may be supplied as target-state evidence.  Only
# current, queryable rows establish the persisted stage.
ACCEPTED_FACT_STATUSES = (
    "active",
    "archived",
    "conflicted",
    "contested",
    "needs_confirmation",
    "revision_closed",
    "superseded",
)
CURRENT_FACT_STATUSES = ("active", "conflicted", "contested", "needs_confirmation")

_CONTRACT = {
    "version": CONTRACT_VERSION,
    "scope": "one_source_admitted_emitted_fact_candidate",
    "identity": {
        "statement": "whitespace_normalized_case_preserving",
        "evidence": "source_span_source_id_or_trusted_gmail_message_anchor",
        "mapping": "candidate_evidence_must_be_a_subset_of_action_and_fact_evidence",
        "cardinality": "zero_or_one_matching_fact_upsert_action_and_exactly_one_current_fact_for_applied_actions",
        "run_ownership": "candidate_action_and_candidate_persisted_fact_relations_are_one_to_one; reuse_fails_closed",
        "ambiguity": "fail_closed",
    },
    "accepted_statuses": {
        "action": list(ACCEPTED_ACTION_STATUSES),
        "critic": [
            "not_run",
            *[value for value in ACCEPTED_CRITIC_DECISIONS if value is not None],
        ],
        "fact_evidence": list(ACCEPTED_FACT_STATUSES),
        "current_fact": list(CURRENT_FACT_STATUSES),
    },
    "stages": {
        "candidate": "the source-admitted extractor emitted the validated candidate",
        "review": "a mapped action survived policy and critic blocking; explicit review residue counts",
        "persisted": "an applied mapped action targets exactly one uniquely matching current fact row",
        "monotonicity": [
            "persisted_implies_review",
            "review_implies_candidate",
        ],
    },
    "dispositions": {
        "residue": "needs_human with an explicit deterministic residue signal",
        "deferred": "no action, undecided action, non-residue human hold, or operational failure",
        "rejected": "policy dismissal, critic block, explicit rejection, or later reversion",
        "applied": "applied or auto_applied with uniquely verified current persistence",
    },
}


class GmailFactParityContractError(ValueError):
    """Raised when stage evidence is malformed, inconsistent, or ambiguous."""


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used by all contract hashes."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise GmailFactParityContractError("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    """Hash a value using the contract's canonical UTF-8 JSON encoding."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


CONTRACT_SHA256 = canonical_sha256(_CONTRACT)


def canonical_contract() -> dict[str, Any]:
    """Return an isolated copy of the versioned, hash-bound contract document."""

    return json.loads(canonical_json(_CONTRACT))


def validate_stage_membership(value: Any) -> dict[str, bool]:
    """Validate exact stage keys and candidate -> review -> persisted monotonicity."""

    if not isinstance(value, Mapping) or set(value) != set(STAGES):
        raise GmailFactParityContractError(
            "stage membership must contain exact stage keys"
        )
    stages: dict[str, bool] = {}
    for stage in STAGES:
        membership = value[stage]
        if not isinstance(membership, bool):
            raise GmailFactParityContractError(f"{stage} membership must be boolean")
        stages[stage] = membership
    if stages["review"] and not stages["candidate"]:
        raise GmailFactParityContractError(
            "review membership requires candidate membership"
        )
    if stages["persisted"] and not stages["review"]:
        raise GmailFactParityContractError(
            "persisted membership requires review membership"
        )
    return stages


def candidate_identity(candidate: Any) -> dict[str, Any]:
    """Return the durable semantic/evidence identity used for action mapping."""

    fact = _mapping(candidate, "candidate")
    statement = _normalized_statement(fact.get("statement"), "candidate.statement")
    anchors = _evidence_anchors(fact, "candidate")
    return {
        "statement": statement,
        "evidence_anchors": [json.loads(value) for value in anchors],
        **(
            {"explicit_candidate_key": explicit_key}
            if (explicit_key := _explicit_candidate_key(fact, "candidate")) is not None
            else {}
        ),
    }


def candidate_sha256(candidate: Any) -> str:
    """Return the stable identity hash for one emitted candidate."""

    return canonical_sha256(candidate_identity(candidate))


def derive_stage_membership(
    candidate: Any,
    actions: Sequence[Any] = (),
    persisted_facts: Sequence[Any] = (),
) -> dict[str, bool]:
    """Derive only the canonical candidate/review/persisted booleans."""

    return derive_stage_record(candidate, actions, persisted_facts)["stages"]


def derive_stage_record(
    candidate: Any,
    actions: Sequence[Any] = (),
    persisted_facts: Sequence[Any] = (),
) -> dict[str, Any]:
    """Derive a canonical, auditable stage record from production-shaped evidence.

    ``actions`` should contain the run-scoped action rows and ``persisted_facts``
    the rows named by their ``target_fact_ids``.  Other action types are ignored;
    malformed ``fact_upsert`` rows fail closed because they could conceal the
    candidate's true disposition.
    """

    identity = candidate_identity(candidate)
    candidate_fact = _mapping(candidate, "candidate")
    fact_actions = _fact_upsert_actions(actions)
    matching_actions = [
        action
        for action in fact_actions
        if _action_matches_candidate(candidate_fact, action)
    ]
    if len(matching_actions) > 1:
        raise GmailFactParityContractError(
            "candidate maps to more than one fact_upsert action"
        )

    facts_by_id = _persisted_facts_by_id(persisted_facts)
    if not matching_actions:
        stages = validate_stage_membership(
            {"candidate": True, "review": False, "persisted": False}
        )
        return _stage_record(
            identity=identity,
            disposition="deferred",
            stages=stages,
            action=None,
            persisted_fact_ids=[],
        )

    action = matching_actions[0]
    status = str(action["status"])
    critic_decision = action.get("critic_decision")
    if critic_decision not in ACCEPTED_CRITIC_DECISIONS:
        raise GmailFactParityContractError("action critic_decision is not accepted")
    critic_blocked = critic_decision in CRITIC_BLOCKING_DECISIONS

    if status in APPLIED_ACTION_STATUSES:
        if critic_blocked:
            raise GmailFactParityContractError(
                "applied action cannot have a blocking critic decision"
            )
        persisted_fact_ids = _verified_persisted_fact_ids(
            candidate_fact,
            action,
            facts_by_id,
        )
        stages = validate_stage_membership(
            {"candidate": True, "review": True, "persisted": True}
        )
        return _stage_record(
            identity=identity,
            disposition="applied",
            stages=stages,
            action=action,
            persisted_fact_ids=persisted_fact_ids,
        )

    if critic_blocked:
        disposition = "rejected"
        review = False
    elif status == "needs_human" and _is_explicit_residue(action):
        disposition = "residue"
        review = True
    elif status in {"rejected", "dismissed"}:
        disposition = "rejected"
        review = False
    elif status == "reverted":
        disposition = "rejected"
        review = True
    elif status in {"needs_human", "failed"}:
        disposition = "deferred"
        review = True
    elif status == "proposed":
        disposition = "deferred"
        review = False
    else:  # pragma: no cover - exact status validation above owns this invariant.
        raise GmailFactParityContractError("action status has no stage disposition")

    stages = validate_stage_membership(
        {"candidate": True, "review": review, "persisted": False}
    )
    return _stage_record(
        identity=identity,
        disposition=disposition,
        stages=stages,
        action=action,
        persisted_fact_ids=[],
    )


def _stage_record(
    *,
    identity: Mapping[str, Any],
    disposition: str,
    stages: Mapping[str, bool],
    action: Mapping[str, Any] | None,
    persisted_fact_ids: list[str],
) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise GmailFactParityContractError("unknown stage disposition")
    action_id = str(action.get("id") or "").strip() if action is not None else None
    action_status = str(action["status"]) if action is not None else None
    return {
        "version": CONTRACT_VERSION,
        "contract_sha256": CONTRACT_SHA256,
        "candidate_sha256": canonical_sha256(identity),
        "action_id": action_id,
        "action_status": action_status,
        "disposition": disposition,
        "stages": dict(stages),
        "persisted_fact_ids": list(persisted_fact_ids),
    }


def _fact_upsert_actions(actions: Sequence[Any]) -> list[Mapping[str, Any]]:
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise GmailFactParityContractError("actions must be a sequence")
    parsed: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_action in enumerate(actions):
        action = _mapping(raw_action, f"actions[{index}]")
        if str(action.get("action_type") or "") != "fact_upsert":
            continue
        action_id = _nonempty_string(action.get("id"), f"actions[{index}].id")
        if action_id in seen_ids:
            raise GmailFactParityContractError("fact_upsert action ids must be unique")
        seen_ids.add(action_id)
        status = _nonempty_string(action.get("status"), f"actions[{index}].status")
        if status not in ACCEPTED_ACTION_STATUSES:
            raise GmailFactParityContractError("action status is not accepted")
        _action_fact(action, f"actions[{index}]")
        parsed.append(action)
    return parsed


def _action_fact(action: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    evidence = _mapping(action.get("evidence_json"), f"{name}.evidence_json")
    payload = _mapping(evidence.get("payload"), f"{name}.evidence_json.payload")
    return _mapping(payload.get("fact"), f"{name}.evidence_json.payload.fact")


def _action_matches_candidate(
    candidate: Mapping[str, Any], action: Mapping[str, Any]
) -> bool:
    action_fact = _action_fact(action, "action")
    candidate_key = _explicit_candidate_key(candidate, "candidate")
    action_key = _action_explicit_candidate_key(action, action_fact)
    if (
        candidate_key is not None
        and action_key is not None
        and candidate_key != action_key
    ):
        return False
    return _fact_contains_candidate_identity(candidate, action_fact)


def _action_explicit_candidate_key(
    action: Mapping[str, Any], action_fact: Mapping[str, Any]
) -> str | None:
    values: list[str] = []
    features = action.get("action_features")
    if features is not None:
        feature_mapping = _mapping(features, "action.action_features")
        if value := str(feature_mapping.get("candidate_key") or "").strip():
            values.append(value)
    if value := _explicit_candidate_key(action_fact, "action fact"):
        values.append(value)
    if len(set(values)) > 1:
        raise GmailFactParityContractError(
            "action has conflicting explicit candidate keys"
        )
    return values[0] if values else None


def _fact_contains_candidate_identity(
    candidate: Mapping[str, Any], other: Mapping[str, Any]
) -> bool:
    if _normalized_statement(
        candidate.get("statement"), "candidate.statement"
    ) != _normalized_statement(other.get("statement"), "matched fact.statement"):
        return False
    candidate_anchors = set(_evidence_anchors(candidate, "candidate"))
    other_anchors = set(_evidence_anchors(other, "matched fact"))
    return candidate_anchors.issubset(other_anchors)


def _persisted_facts_by_id(facts: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
        raise GmailFactParityContractError("persisted_facts must be a sequence")
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_fact in enumerate(facts):
        fact = _mapping(raw_fact, f"persisted_facts[{index}]")
        fact_id = _nonempty_string(fact.get("id"), f"persisted_facts[{index}].id")
        if fact_id in by_id:
            raise GmailFactParityContractError("persisted fact ids must be unique")
        status = _nonempty_string(
            fact.get("status"), f"persisted_facts[{index}].status"
        )
        if status not in ACCEPTED_FACT_STATUSES:
            raise GmailFactParityContractError("persisted fact status is not accepted")
        by_id[fact_id] = fact
    return by_id


def _verified_persisted_fact_ids(
    candidate: Mapping[str, Any],
    action: Mapping[str, Any],
    facts_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    raw_target_ids = action.get("target_fact_ids")
    if not isinstance(raw_target_ids, list) or not raw_target_ids:
        raise GmailFactParityContractError("applied action must have target_fact_ids")
    target_ids = [
        _nonempty_string(value, "action.target_fact_ids[]") for value in raw_target_ids
    ]
    if len(set(target_ids)) != len(target_ids):
        raise GmailFactParityContractError("action target_fact_ids must be unique")
    missing = sorted(set(target_ids) - set(facts_by_id))
    if missing:
        raise GmailFactParityContractError(
            "applied action target fact evidence is incomplete"
        )
    matching = [
        fact_id
        for fact_id in target_ids
        if str(facts_by_id[fact_id]["status"]) in CURRENT_FACT_STATUSES
        and _fact_contains_candidate_identity(candidate, facts_by_id[fact_id])
    ]
    if len(matching) != 1:
        raise GmailFactParityContractError(
            "applied action must map to exactly one matching current persisted fact"
        )
    return matching


def _is_explicit_residue(action: Mapping[str, Any]) -> bool:
    features = action.get("action_features")
    if features is None:
        feature_mapping: Mapping[str, Any] = {}
    else:
        feature_mapping = _mapping(features, "action.action_features")
    if str(feature_mapping.get("simple_decision") or "") == "residue":
        return True
    if str(feature_mapping.get("resolver_precheck") or "") == "residue":
        return True
    if str(feature_mapping.get("residue_kind") or "").strip():
        return True
    return str(action.get("policy_decision") or "") in {
        "simple_residue",
        "earned_residue",
    }


def _explicit_candidate_key(fact: Mapping[str, Any], name: str) -> str | None:
    values: list[str] = []
    if value := str(fact.get("candidate_key") or "").strip():
        values.append(value)
    metadata = fact.get("metadata")
    if metadata is not None:
        metadata_mapping = _mapping(metadata, f"{name}.metadata")
        for key in ("candidate_key", "fact_parity_candidate_key"):
            if value := str(metadata_mapping.get(key) or "").strip():
                values.append(value)
    if len(set(values)) > 1:
        raise GmailFactParityContractError(
            f"{name} has conflicting explicit candidate keys"
        )
    return values[0] if values else None


def _evidence_anchors(fact: Mapping[str, Any], name: str) -> tuple[str, ...]:
    anchors: set[str] = set()
    spans = fact.get("source_spans")
    if spans is not None:
        if not isinstance(spans, list):
            raise GmailFactParityContractError(f"{name}.source_spans must be a list")
        for index, raw_span in enumerate(spans):
            span = _mapping(raw_span, f"{name}.source_spans[{index}]")
            chunk_id = _nonempty_string(
                span.get("chunk_id"), f"{name}.source_spans[{index}].chunk_id"
            )
            start = _nonnegative_int(
                span.get("start"), f"{name}.source_spans[{index}].start"
            )
            end = _nonnegative_int(span.get("end"), f"{name}.source_spans[{index}].end")
            if end <= start:
                raise GmailFactParityContractError("source span end must exceed start")
            anchors.add(
                canonical_json(
                    {
                        "kind": "source_span",
                        "chunk_id": chunk_id,
                        "start": start,
                        "end": end,
                    }
                )
            )

    source_ids = fact.get("source_ids")
    if source_ids is not None:
        if not isinstance(source_ids, list):
            raise GmailFactParityContractError(f"{name}.source_ids must be a list")
        for index, source_id in enumerate(source_ids):
            anchors.add(
                canonical_json(
                    {
                        "kind": "source_id",
                        "value": _nonempty_string(
                            source_id, f"{name}.source_ids[{index}]"
                        ),
                    }
                )
            )

    metadata = fact.get("metadata")
    if metadata is not None:
        metadata_mapping = _mapping(metadata, f"{name}.metadata")
        message_values: list[Any] = []
        if metadata_mapping.get("gmail_message_id") is not None:
            message_values.append(metadata_mapping["gmail_message_id"])
        raw_message_ids = metadata_mapping.get("gmail_message_ids")
        if raw_message_ids is not None:
            if not isinstance(raw_message_ids, list):
                raise GmailFactParityContractError(
                    f"{name}.metadata.gmail_message_ids must be a list"
                )
            message_values.extend(raw_message_ids)
        for index, message_id in enumerate(message_values):
            anchors.add(
                canonical_json(
                    {
                        "kind": "gmail_message_id",
                        "value": _nonempty_string(
                            message_id,
                            f"{name}.metadata.gmail_message_ids[{index}]",
                        ),
                    }
                )
            )

    if not anchors:
        raise GmailFactParityContractError(
            f"{name} must contain durable source evidence anchors"
        )
    return tuple(sorted(anchors))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GmailFactParityContractError(f"{name} must be an object")
    return value


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailFactParityContractError(f"{name} must be a non-empty string")
    return value.strip()


def _normalized_statement(value: Any, name: str) -> str:
    return " ".join(_nonempty_string(value, name).split())


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GmailFactParityContractError(f"{name} must be a non-negative integer")
    return value
