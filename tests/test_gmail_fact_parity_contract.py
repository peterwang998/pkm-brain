from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
CONTRACT_PATH = ROOT / "scripts" / "gmail_fact_parity_contract.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_gmail_fact_parity_contract_module", CONTRACT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _load()


def _candidate(
    *, statement: str = "Alpha launch is scheduled for Friday."
) -> dict[str, Any]:
    return {
        "statement": statement,
        "page_hint": "projects/alpha.md",
        "source_ids": ["document:gmail-alpha"],
        "source_spans": [{"chunk_id": "chunk_gmail_alpha", "start": 10, "end": 72}],
        "evidence_quote": "Alpha launch is scheduled for Friday.",
        "metadata": {"gmail_message_id": "message-alpha"},
    }


def _action(
    candidate: dict[str, Any],
    *,
    status: str,
    action_id: str = "cosact_alpha",
    critic_decision: str | None = None,
    action_features: dict[str, Any] | None = None,
    policy_decision: str | None = None,
    target_fact_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "action_type": "fact_upsert",
        "status": status,
        "critic_decision": critic_decision,
        "policy_decision": policy_decision,
        "action_features": action_features or {},
        "target_fact_ids": target_fact_ids or [],
        "evidence_json": {"payload": {"fact": deepcopy(candidate)}},
    }


def _fact(
    candidate: dict[str, Any],
    *,
    fact_id: str = "fact_alpha",
    status: str = "active",
) -> dict[str, Any]:
    return {**deepcopy(candidate), "id": fact_id, "status": status}


def test_contract_is_canonical_hash_bound_and_returns_an_isolated_copy() -> None:
    value = contract.canonical_contract()

    assert value["version"] == contract.CONTRACT_VERSION
    assert contract.CONTRACT_SHA256 == contract.canonical_sha256(value)
    assert (
        contract.CONTRACT_SHA256
        == "d83dd30e0d8ec972dc9a543699a4089d365e16523051b64c1db75ba9a7d4ddfa"
    )
    assert value["accepted_statuses"]["action"] == list(
        contract.ACCEPTED_ACTION_STATUSES
    )
    assert value["accepted_statuses"]["critic"] == [
        "not_run",
        "agree",
        "disagree",
        "evidence_incomplete",
        "unavailable",
    ]

    value["scope"] = "mutated"
    assert contract.canonical_contract()["scope"] != "mutated"


def test_candidate_identity_is_order_stable_and_ignores_non_identity_fields() -> None:
    first = _candidate()
    second = deepcopy(first)
    second["truth_confidence"] = 0.51
    second["page_hint"] = "projects/repaired-route.md"
    second["source_ids"] = list(reversed(second["source_ids"]))

    assert contract.candidate_identity(first) == contract.candidate_identity(second)
    assert contract.candidate_sha256(first) == contract.candidate_sha256(second)


def test_candidate_requires_durable_source_evidence() -> None:
    candidate = {"statement": "A source-free assertion."}

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="durable source evidence anchors",
    ):
        contract.candidate_identity(candidate)


def test_proposed_action_is_deferred_before_review() -> None:
    candidate = _candidate()

    record = contract.derive_stage_record(
        candidate, [_action(candidate, status="proposed")], []
    )

    assert record["disposition"] == "deferred"
    assert record["stages"] == {
        "candidate": True,
        "review": False,
        "persisted": False,
    }


def test_no_action_is_deferred_before_review() -> None:
    record = contract.derive_stage_record(_candidate(), [], [])

    assert record["action_id"] is None
    assert record["action_status"] is None
    assert record["disposition"] == "deferred"
    assert record["stages"] == {
        "candidate": True,
        "review": False,
        "persisted": False,
    }


@pytest.mark.parametrize(
    ("features", "policy_decision"),
    [
        ({"simple_decision": "residue"}, None),
        ({"resolver_precheck": "residue"}, None),
        ({"residue_kind": "gmail_temporal_review"}, None),
        ({}, "simple_residue"),
        ({}, "earned_residue"),
    ],
)
def test_explicit_needs_human_residue_enters_review(
    features: dict[str, Any], policy_decision: str | None
) -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status="needs_human",
        action_features=features,
        policy_decision=policy_decision,
    )

    record = contract.derive_stage_record(candidate, [action], [])

    assert record["disposition"] == "residue"
    assert record["stages"] == {
        "candidate": True,
        "review": True,
        "persisted": False,
    }


@pytest.mark.parametrize("status", ["needs_human", "failed"])
def test_non_rejected_holds_are_deferred_after_review(status: str) -> None:
    candidate = _candidate()
    action = _action(candidate, status=status, critic_decision="agree")

    record = contract.derive_stage_record(candidate, [action], [])

    assert record["disposition"] == "deferred"
    assert record["stages"]["review"] is True
    assert record["stages"]["persisted"] is False


@pytest.mark.parametrize(
    "critic_decision", ["disagree", "evidence_incomplete", "unavailable"]
)
def test_critic_block_is_rejected_before_review(critic_decision: str) -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status="needs_human",
        critic_decision=critic_decision,
        action_features={"residue_kind": "critic_disagreement"},
    )

    record = contract.derive_stage_record(candidate, [action], [])

    assert record["disposition"] == "rejected"
    assert record["stages"] == {
        "candidate": True,
        "review": False,
        "persisted": False,
    }


@pytest.mark.parametrize("status", ["rejected", "dismissed"])
def test_terminal_rejection_never_enters_review(status: str) -> None:
    candidate = _candidate()
    record = contract.derive_stage_record(
        candidate,
        [_action(candidate, status=status, critic_decision="agree")],
        [],
    )

    assert record["disposition"] == "rejected"
    assert record["stages"]["review"] is False


def test_reverted_action_retains_review_history_but_not_persistence() -> None:
    candidate = _candidate()
    record = contract.derive_stage_record(
        candidate,
        [_action(candidate, status="reverted", target_fact_ids=["fact_alpha"])],
        [_fact(candidate)],
    )

    assert record["disposition"] == "rejected"
    assert record["stages"] == {
        "candidate": True,
        "review": True,
        "persisted": False,
    }
    assert record["persisted_fact_ids"] == []


@pytest.mark.parametrize("status", ["applied", "auto_applied"])
def test_applied_action_requires_and_verifies_unique_current_fact(status: str) -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status=status,
        critic_decision="agree",
        target_fact_ids=["fact_alpha"],
    )

    record = contract.derive_stage_record(candidate, [action], [_fact(candidate)])

    assert record["disposition"] == "applied"
    assert record["stages"] == {
        "candidate": True,
        "review": True,
        "persisted": True,
    }
    assert record["persisted_fact_ids"] == ["fact_alpha"]
    assert (
        contract.derive_stage_membership(candidate, [action], [_fact(candidate)])
        == record["stages"]
    )


def test_critic_repair_may_add_evidence_without_breaking_identity() -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status="applied",
        critic_decision="agree",
        target_fact_ids=["fact_alpha"],
    )
    action_fact = action["evidence_json"]["payload"]["fact"]
    action_fact["source_spans"].append(
        {"chunk_id": "chunk_gmail_alpha", "start": 75, "end": 104}
    )
    action_fact["source_ids"].append("document:gmail-alpha-repair")
    persisted = _fact(action_fact)

    record = contract.derive_stage_record(candidate, [action], [persisted])

    assert record["stages"]["persisted"] is True


def test_applied_action_fails_closed_without_complete_target_evidence() -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status="applied",
        target_fact_ids=["fact_alpha", "fact_counterpart"],
    )

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="target fact evidence is incomplete",
    ):
        contract.derive_stage_record(candidate, [action], [_fact(candidate)])


def test_applied_action_fails_closed_when_matching_fact_is_not_current() -> None:
    candidate = _candidate()
    action = _action(candidate, status="applied", target_fact_ids=["fact_alpha"])

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="exactly one matching current persisted fact",
    ):
        contract.derive_stage_record(
            candidate,
            [action],
            [_fact(candidate, status="revision_closed")],
        )


def test_applied_action_fails_closed_on_ambiguous_current_facts() -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status="auto_applied",
        target_fact_ids=["fact_alpha", "fact_alpha_copy"],
    )

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="exactly one matching current persisted fact",
    ):
        contract.derive_stage_record(
            candidate,
            [action],
            [_fact(candidate), _fact(candidate, fact_id="fact_alpha_copy")],
        )


def test_multiple_matching_actions_fail_closed() -> None:
    candidate = _candidate()

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="more than one fact_upsert action",
    ):
        contract.derive_stage_record(
            candidate,
            [
                _action(candidate, status="rejected", action_id="cosact_a"),
                _action(candidate, status="needs_human", action_id="cosact_b"),
            ],
            [],
        )


def test_same_statement_from_different_evidence_does_not_map() -> None:
    candidate = _candidate()
    unrelated = _candidate()
    unrelated["source_ids"] = ["document:gmail-other"]
    unrelated["source_spans"] = [
        {"chunk_id": "chunk_gmail_other", "start": 1, "end": 20}
    ]
    unrelated["metadata"] = {"gmail_message_id": "message-other"}

    record = contract.derive_stage_record(
        candidate,
        [_action(unrelated, status="applied", target_fact_ids=["fact_other"])],
        [_fact(unrelated, fact_id="fact_other")],
    )

    assert record["action_id"] is None
    assert record["disposition"] == "deferred"


def test_conflicting_explicit_candidate_keys_fail_closed() -> None:
    candidate = _candidate()
    candidate["candidate_key"] = "candidate-a"
    action = _action(
        candidate,
        status="needs_human",
        action_features={"candidate_key": "candidate-b"},
    )

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="conflicting explicit candidate keys",
    ):
        contract.derive_stage_record(candidate, [action], [])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "invented", "action status is not accepted"),
        ("critic_decision", "maybe", "critic_decision is not accepted"),
    ],
)
def test_unrecognized_action_state_fails_closed(
    field: str, value: str, message: str
) -> None:
    candidate = _candidate()
    action = _action(candidate, status="needs_human")
    action[field] = value

    with pytest.raises(contract.GmailFactParityContractError, match=message):
        contract.derive_stage_record(candidate, [action], [])


def test_applied_action_with_blocking_critic_is_inconsistent() -> None:
    candidate = _candidate()
    action = _action(
        candidate,
        status="applied",
        critic_decision="disagree",
        target_fact_ids=["fact_alpha"],
    )

    with pytest.raises(
        contract.GmailFactParityContractError,
        match="applied action cannot have a blocking critic decision",
    ):
        contract.derive_stage_record(candidate, [action], [_fact(candidate)])


@pytest.mark.parametrize(
    "stages",
    [
        {"candidate": False, "review": True, "persisted": False},
        {"candidate": True, "review": False, "persisted": True},
    ],
)
def test_monotonicity_is_enforced(stages: dict[str, bool]) -> None:
    with pytest.raises(contract.GmailFactParityContractError, match="requires"):
        contract.validate_stage_membership(stages)


def test_stage_membership_requires_exact_boolean_shape() -> None:
    with pytest.raises(contract.GmailFactParityContractError, match="exact stage keys"):
        contract.validate_stage_membership({"candidate": True, "review": False})
    with pytest.raises(contract.GmailFactParityContractError, match="must be boolean"):
        contract.validate_stage_membership(
            {"candidate": True, "review": 1, "persisted": False}
        )


def test_non_fact_actions_do_not_participate_in_candidate_mapping() -> None:
    record = contract.derive_stage_record(
        _candidate(),
        [
            {
                "id": "cosact_page",
                "action_type": "synthesize_page",
                "status": "made_up_status_is_irrelevant",
            }
        ],
        [],
    )

    assert record["action_id"] is None
    assert record["disposition"] == "deferred"
