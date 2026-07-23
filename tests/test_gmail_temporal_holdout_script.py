from __future__ import annotations

import importlib.util
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT_PATH = ROOT / "scripts" / "build_gmail_temporal_holdout.py"
BASELINE_SCRIPT_PATH = (
    ROOT / "scripts" / "freeze_gmail_temporal_development_baseline.py"
)


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


holdout = _load("test_build_gmail_temporal_holdout", SCRIPT_PATH)
baseline = _load(
    "test_freeze_gmail_temporal_development_baseline_for_holdout",
    BASELINE_SCRIPT_PATH,
)


def _candidate(
    index: int,
    *,
    strata: frozenset[str] = frozenset(),
    thread: str | None = None,
    internal_at: str = "2026-07-22T12:00:00+00:00",
) -> Any:
    return holdout._Candidate(
        document_id=f"document-{index}",
        message_id=f"message-{index}",
        account_key="account",
        thread_key=thread or f"thread-{index}",
        source_revision=f"revision-{index}",
        source_sha256=f"{index:064x}",
        message_internal_at=internal_at,
        context_before_count=0,
        context_after_count=0,
        omitted_message_count=0,
        thread_truncated_message_count=0,
        target_body_truncation_status="not_indicated",
        message_ids=(f"message-{index}",),
        admission_basis="fact" if "candidate_bearing" in strata else "not_admitted",
        disposition="prepared",
        error_bucket=None,
        expression_count=1,
        mention_count=1,
        candidate_count=int("candidate_bearing" in strata),
        page_count=int("candidate_bearing" in strata),
        policy={},
        expression_forms=("explicit_date",),
        lifecycle_roles=(),
        strata=strata,
        rank=f"gthr_{index:064x}",
        fingerprint=f"candidate-{index:03d}",
    )


def test_selection_has_blind_fresh_primary_and_thread_disjoint_splits() -> None:
    candidates = [
        _candidate(
            index,
            internal_at=(
                "2026-07-22T12:00:00+00:00"
                if index < 4
                else "2026-07-10T12:00:00+00:00"
            ),
            strata=(
                frozenset({"candidate_bearing"})
                if index in {4, 6}
                else frozenset({"hard_negative"})
                if index in {5, 7}
                else frozenset()
            ),
        )
        for index in range(10)
    ]

    selection = holdout._select_cohort(
        candidates,
        sample_size=2,
        challenge_size=2,
        reserve_size=2,
        fresh_after="2026-07-14T00:00:00+00:00",
        quotas=(("candidate_bearing", 1), ("hard_negative", 1)),
    )

    assert [item.fingerprint for item in selection.primary] == [
        "candidate-000",
        "candidate-001",
    ]
    assert len(selection.challenge) == 2
    assert len(selection.reserve) == 2
    all_rows = (*selection.primary, *selection.challenge, *selection.reserve)
    assert len({(item.account_key, item.thread_key) for item in all_rows}) == 6
    assert sum("candidate_bearing" in item.strata for item in selection.challenge) >= 1
    assert sum("hard_negative" in item.strata for item in selection.challenge) >= 1
    assert selection == holdout._select_cohort(
        tuple(reversed(candidates)),
        sample_size=2,
        challenge_size=2,
        reserve_size=2,
        fresh_after="2026-07-14T00:00:00+00:00",
        quotas=(("hard_negative", 1), ("candidate_bearing", 1)),
    )


def test_baseline_thread_scope_identity_matches_freezer() -> None:
    key = b"k" * 32

    assert holdout._baseline_thread_scope_id(key, "account", "thread") == (
        baseline._thread_scope_id(key, "account", "thread")
    )


@pytest.mark.parametrize(
    ("sample_size", "challenge_size", "reserve_size"),
    [(149, 100, 75), (150, 99, 75), (150, 100, 74)],
)
def test_baseline_backed_cohort_sizes_are_pinned_at_creation(
    sample_size: int,
    challenge_size: int,
    reserve_size: int,
) -> None:
    with pytest.raises(
        holdout.GmailTemporalHoldoutError,
        match="exactly 150/100/75",
    ):
        holdout._validate_requested_cohort_sizes(
            holdout.PROSPECTIVE_EVIDENCE_CLASS,
            sample_size=sample_size,
            challenge_size=challenge_size,
            reserve_size=reserve_size,
        )

    holdout._validate_requested_cohort_sizes(
        holdout.DIAGNOSTIC_EVIDENCE_CLASS,
        sample_size=sample_size,
        challenge_size=challenge_size,
        reserve_size=reserve_size,
    )


def test_release_primary_pool_can_use_separate_historical_challenge() -> None:
    fresh = tuple(_candidate(index) for index in range(4))
    historical = (
        _candidate(
            10,
            strata=frozenset({"A", "B"}),
            internal_at="2026-07-10T12:00:00+00:00",
        ),
        _candidate(11, internal_at="2026-07-10T12:00:00+00:00"),
    )

    selection = holdout._select_cohort(
        fresh,
        challenge_candidates=historical,
        sample_size=2,
        challenge_size=1,
        reserve_size=2,
        fresh_after="2026-07-14T00:00:00+00:00",
        quotas=(("A", 1), ("B", 1)),
    )

    assert len(selection.primary) == 2
    assert len(selection.reserve) == 2
    assert [item.fingerprint for item in selection.challenge] == ["candidate-010"]


def test_primary_and_reserve_are_a_stable_activation_prefix() -> None:
    candidates = tuple(_candidate(index) for index in range(5))

    initial = holdout._select_cohort(
        candidates,
        sample_size=2,
        challenge_size=0,
        reserve_size=3,
        fresh_after="2026-07-14T00:00:00+00:00",
        quotas=(),
    )
    expanded = holdout._select_cohort(
        candidates,
        sample_size=4,
        challenge_size=0,
        reserve_size=1,
        fresh_after="2026-07-14T00:00:00+00:00",
        quotas=(),
    )

    frozen_order = [item.fingerprint for item in (*initial.primary, *initial.reserve)]
    assert [item.fingerprint for item in expanded.primary] == frozen_order[:4]
    assert [item.fingerprint for item in expanded.reserve] == frozen_order[4:]


def test_exact_selector_uses_multistratum_witness() -> None:
    rows = (
        _candidate(1, strata=frozenset({"A"})),
        _candidate(2, strata=frozenset({"B"})),
        _candidate(3, strata=frozenset({"A", "B"})),
    )

    selected = holdout._exact_challenge_selection(
        rows,
        challenge_size=1,
        quotas=(("A", 1), ("B", 1)),
    )

    assert [item.fingerprint for item in selected] == ["candidate-003"]


def test_exact_selector_uses_bounded_balanced_cost() -> None:
    one_page = replace(
        _candidate(1, strata=frozenset({"A", "B", "candidate_bearing"})),
        page_count=1,
        candidate_count=12,
    )
    two_pages = replace(
        _candidate(2, strata=frozenset({"A", "B", "candidate_bearing"})),
        page_count=2,
        candidate_count=1,
    )

    selected = holdout._exact_challenge_selection(
        (one_page, two_pages),
        challenge_size=1,
        quotas=(("A", 1), ("B", 1)),
    )

    assert [item.fingerprint for item in selected] == ["candidate-002"]


def test_exact_selector_uses_verified_canonical_secondary_tie_break() -> None:
    rows = (_candidate(1), _candidate(2), _candidate(3))
    expected = min(rows, key=holdout._canonical_secondary_weight)

    selected = holdout._exact_challenge_selection(
        rows,
        challenge_size=1,
        quotas=(),
    )
    reversed_selected = holdout._exact_challenge_selection(
        tuple(reversed(rows)),
        challenge_size=1,
        quotas=(),
    )

    assert selected == (expected,)
    assert reversed_selected == selected


def test_exact_selector_fails_closed_when_secondary_optimum_is_not_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(holdout, "_canonical_secondary_weight", lambda _item: 1)

    with pytest.raises(
        holdout.GmailTemporalHoldoutError,
        match="canonical tie-break was not unique",
    ):
        holdout._exact_challenge_selection(
            (_candidate(1), _candidate(2)),
            challenge_size=1,
            quotas=(),
        )


def test_exact_selector_enforces_one_target_per_thread() -> None:
    rows = (
        _candidate(1, strata=frozenset({"A"}), thread="shared"),
        _candidate(2, strata=frozenset({"B"}), thread="shared"),
        _candidate(3),
    )

    with pytest.raises(
        holdout.GmailTemporalHoldoutError,
        match="constraints are infeasible",
    ):
        holdout._exact_challenge_selection(
            rows,
            challenge_size=2,
            quotas=(("A", 1), ("B", 1)),
        )


def test_default_challenge_quotas_have_a_capacity_coherent_witness() -> None:
    rows: list[Any] = []
    for index in range(100):
        strata: set[str] = set()
        if index < 30:
            strata.update({"candidate_bearing", "fact_candidate"})
        elif index < 60:
            strata.update({"candidate_bearing", "temporal_rescue_candidate"})
        elif index < 70:
            strata.update(
                {
                    "candidate_bearing",
                    "hard_negative",
                    "weak_advertising_candidate",
                }
            )
        elif index < 90:
            strata.update({"hard_negative", "temporal_form_without_candidate"})
        else:
            strata.update(
                {"admitted_zero_candidate", "temporal_form_without_candidate"}
            )
        if index < 12:
            strata.update({"lifecycle_source", "lifecycle_candidate"})
        if index < 10:
            strata.add("long_tail_candidate")
        if index == 0:
            strata.add("extreme_long_tail_candidate")
        if index < 2:
            strata.add("lifecycle_role_rescheduled")
        elif index < 7:
            strata.add("lifecycle_role_cancelled")
        elif index < 12:
            strata.add("lifecycle_role_completed")
        if index < 3:
            strata.add("bulk_candidate")
        if index == 99:
            strata.add("preparation_failure")
        candidate = _candidate(index, strata=frozenset(strata))
        if index == 0:
            candidate = replace(candidate, page_count=22, candidate_count=36)
        rows.append(candidate)

    selected = holdout._exact_challenge_selection(
        rows,
        challenge_size=holdout.DEFAULT_CHALLENGE_SIZE,
        quotas=holdout.DEFAULT_QUOTAS,
    )

    assert len(selected) == holdout.DEFAULT_CHALLENGE_SIZE
    assert sum(item.page_count for item in selected) <= (
        holdout.MAX_CHALLENGE_TOTAL_PAGES
    )
    assert sum(item.candidate_count for item in selected) <= (
        holdout.MAX_CHALLENGE_TOTAL_CANDIDATES
    )
    assert max(item.page_count for item in selected) <= (
        holdout.MAX_CHALLENGE_MESSAGE_PAGES
    )
    assert all(
        sum(stratum in item.strata for item in selected) >= minimum
        for stratum, minimum in holdout.DEFAULT_QUOTAS
    )


def test_source_label_queue_hides_pipeline_decisions() -> None:
    sample = {
        "sample_id": "sample",
        "thread_id": "thread",
        "message_internal_at": "2026-07-22T12:00:00+00:00",
        "source_prior_message_count": 1,
        "source_later_message_count": 0,
        "source_omitted_before_count": 0,
        "thread_truncated_message_count": 1,
        "target_body_truncation_status": "unknown_thread_has_truncation",
        "text": "Meeting next Tuesday.",
        "sanitized_text_sha256": "a" * 64,
        "selection_partition": "challenge",
        "preparation": {"candidate_count": 1},
        "policy": {"provider_important": True},
        "selection_strata": ["candidate_bearing"],
        "expressions": [{"surface": "next Tuesday"}],
        "mentions": [{"surface": "Meeting"}],
        "leads": [{"expression_id": "expression"}],
    }

    materialized = SimpleNamespace(
        candidate=_candidate(20),
        text=sample["text"],
        context=(
            holdout._ContextSource(
                relative_position=-1,
                message_id="prior",
                message_internal_at="2026-07-21T12:00:00+00:00",
                text="p" * 3_100,
            ),
        ),
    )

    row = holdout._source_label_queue_row(sample, materialized, key=b"k" * 32)

    assert row["target"]["text"] == sample["text"]
    assert row["target"]["body_truncation_status"] == ("unknown_thread_has_truncation")
    assert row["thread_context"]["prior_included"] == 1
    assert row["thread_context"]["prior_omitted"] == 0
    assert row["thread_context"]["messages"][0]["emitted_char_count"] == 3_000
    assert row["thread_context"]["messages"][0]["text_truncated_after"] is True
    assert row["label_status"] == "unlabeled"
    for hidden in (
        "selection_partition",
        "preparation",
        "policy",
        "selection_strata",
        "expressions",
        "mentions",
        "leads",
    ):
        assert hidden not in row


def test_context_uses_exact_two_prior_and_two_later_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = replace(
        _candidate(3),
        message_id="m3",
        message_ids=("m0", "m1", "m2", "m3", "m4", "m5"),
    )

    def load_message(
        _paths: object,
        *,
        document_id: str,
        gmail_message_id: str,
    ) -> Any:
        assert document_id == candidate.document_id
        return SimpleNamespace(
            text=f"text-{gmail_message_id}",
            locator=SimpleNamespace(
                gmail_account_key=candidate.account_key,
                gmail_thread_id=candidate.thread_key,
                gmail_source_revision=candidate.source_revision,
                message_internal_at="2026-07-22T12:00:00+00:00",
            ),
        )

    monkeypatch.setattr(holdout, "_load_trusted_message", load_message)

    context = holdout._context_for_candidate(object(), candidate)

    assert [(item.relative_position, item.message_id) for item in context] == [
        (-2, "m1"),
        (-1, "m2"),
        (1, "m4"),
        (2, "m5"),
    ]


def test_publish_is_private_and_no_replace(tmp_path: Path) -> None:
    parent = tmp_path / "shared-parent"
    parent.mkdir()
    os.chmod(parent, 0o755)
    output = parent / "holdout"

    holdout._publish_frozen(output, {"artifact.json": b"{}\n"})

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "artifact.json").stat().st_mode) == 0o600
    with pytest.raises(
        holdout.GmailTemporalHoldoutError,
        match="already exists",
    ):
        holdout._publish_frozen(output, {"artifact.json": b"changed\n"})
    assert (output / "artifact.json").read_bytes() == b"{}\n"


def test_freeze_authority_records_first_attempt_and_blocks_path_or_key_reroll(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    output = tmp_path / "first-output"
    authority_key = b"a" * 32
    holdout_key = b"h" * 32
    configuration = {
        "fresh_after": "2026-07-23T00:00:00+00:00",
        "sample_size": 150,
        "challenge_size": 100,
        "reserve_size": 75,
        "quotas": [],
        "development_baseline_manifest_sha256": "b" * 64,
    }

    attempt = holdout._register_freeze_attempt(
        authority_root,
        authority_key=authority_key,
        holdout_key=holdout_key,
        output_root=output,
        milestone="production-release-1",
        evidence_class=holdout.PROSPECTIVE_EVIDENCE_CLASS,
        configuration=configuration,
    )

    attempt_path = authority_root / "attempts" / f"{attempt.attempt_id}.json"
    record, raw = holdout._load_freeze_signed_record(
        attempt_path,
        key=authority_key,
        domain=holdout.FREEZE_ATTEMPT_DOMAIN,
    )
    assert record["milestone"] == "production-release-1"
    assert record["evidence_class"] == holdout.PROSPECTIVE_EVIDENCE_CLASS
    assert record["registered_before_discovery_selection_and_publication"] is True
    assert attempt.attempt_sha256 == holdout._sha256_bytes(raw)
    assert stat.S_IMODE(authority_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(attempt_path.stat().st_mode) == 0o600

    for alternate_output, alternate_holdout_key in (
        (tmp_path / "alternate-output", holdout_key),
        (tmp_path / "third-output", b"z" * 32),
    ):
        with pytest.raises(
            holdout.GmailTemporalHoldoutError,
            match="already attempted",
        ):
            holdout._register_freeze_attempt(
                authority_root,
                authority_key=authority_key,
                holdout_key=alternate_holdout_key,
                output_root=alternate_output,
                milestone="production-release-1",
                evidence_class=holdout.PROSPECTIVE_EVIDENCE_CLASS,
                configuration=configuration,
            )


def test_freeze_authority_uses_independent_stable_key_and_authenticated_outcome(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    authority_key = b"a" * 32
    attempt = holdout._register_freeze_attempt(
        authority_root,
        authority_key=authority_key,
        holdout_key=b"h" * 32,
        output_root=tmp_path / "output",
        milestone="diagnostic-1",
        evidence_class=holdout.DIAGNOSTIC_EVIDENCE_CLASS,
        configuration={"sample_size": 1},
    )
    holdout._complete_freeze_attempt(
        attempt,
        authority_key=authority_key,
        holdout_manifest_raw=b'{"manifest":"frozen"}\n',
    )
    outcome_path = authority_root / "outcomes" / f"{attempt.attempt_id}.json"
    outcome, _raw = holdout._load_freeze_signed_record(
        outcome_path,
        key=authority_key,
        domain=holdout.FREEZE_OUTCOME_DOMAIN,
    )
    assert outcome["status"] == "published"
    assert outcome["attempt_sha256"] == attempt.attempt_sha256

    with pytest.raises(
        holdout.GmailTemporalHoldoutError,
        match="independent",
    ):
        holdout._register_freeze_attempt(
            tmp_path / "second-authority",
            authority_key=b"x" * 32,
            holdout_key=b"x" * 32,
            output_root=tmp_path / "second-output",
            milestone="diagnostic-2",
            evidence_class=holdout.DIAGNOSTIC_EVIDENCE_CLASS,
            configuration={"sample_size": 1},
        )


def test_hmac_key_must_be_owner_only_regular_file(tmp_path: Path) -> None:
    key = tmp_path / "holdout.key"
    key.write_bytes(b"k" * 32)
    os.chmod(key, 0o600)
    assert holdout._private_hmac_key(key) == b"k" * 32

    os.chmod(key, 0o644)
    with pytest.raises(
        holdout.GmailTemporalHoldoutError,
        match="owner-only",
    ):
        holdout._private_hmac_key(key)
