from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_gmail_temporal_event_identity.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "gmail_temporal_event_identity_diagnostic",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diagnostic = _load_script()


def test_public_synthetic_structural_addressability_metrics_are_exact() -> None:
    report = diagnostic.diagnose()

    assert report["scope"] == (
        "structural_event_identity_addressability_not_semantic_recall"
    )
    assert report["semantic_recall_measured"] is False
    assert report["semantic_precision_measured"] is False
    assert report["private_gmail_used"] is False
    assert report["external_model_calls"] == 0
    assert report["aggregate"] == {
        "plans": 5,
        "messages": 6,
        "eligible_event_bearing_messages": 4,
        "review_artifacts": 9,
        "eligible_event_identity_units": 7,
        "one_unit_plans": 1,
        "zero_call_plans": 2,
        "source_self_assertions": 5,
        "resolution_clusters": 6,
        "canonical_cross_unit_clusters": 1,
        "provisional_source_self_views": 5,
        "external_consensus_clusters": 1,
        "excluded_non_event_artifacts": 2,
        "lifecycle_event_views": 5,
        "fixture_oracle_verdict_sets": 9,
        "external_model_calls": 0,
    }
    assert all(report["safety_invariants"].values())


def test_scenarios_distinguish_self_external_uncertain_and_excluded_units() -> None:
    report = diagnostic.diagnose()
    scenarios = {item["scenario"]: item for item in report["scenarios"]}

    singleton = scenarios["zero_call_singleton_schedule"]
    assert singleton["eligible_event_identity_units"] == 1
    assert singleton["identity_pairs"] == 0
    assert singleton["fixture_oracle_verdict_sets"] == 0
    assert singleton["source_self_assertions"] == 1

    external = scenarios["external_consensus_reschedule"]
    assert external["eligible_event_identity_units"] == 2
    assert external["fixture_oracle_verdict_sets"] == 3
    assert external["external_consensus_clusters"] == 1
    assert external["external_consensus_assertions"] == 2

    distinct = scenarios["distinct_event_self_views"]
    assert distinct["resolution_clusters"] == 2
    assert distinct["canonical_cross_unit_clusters"] == 0
    assert distinct["source_self_clusters"] == 2
    assert distinct["external_consensus_clusters"] == 0

    uncertain = scenarios["uncertain_pair_self_views"]
    assert uncertain["resolution_clusters"] == 2
    assert uncertain["canonical_cross_unit_clusters"] == 0
    assert uncertain["source_self_clusters"] == 2
    assert uncertain["external_consensus_clusters"] == 0

    excluded = scenarios["non_event_deadline_exclusion"]
    assert excluded["review_artifacts"] == 2
    assert excluded["excluded_non_event_artifacts"] == 2
    assert excluded["eligible_event_identity_units"] == 0
    assert excluded["resolution_clusters"] == 0
    assert excluded["canonical_cross_unit_clusters"] == 0


def test_cli_emits_the_same_machine_readable_scope() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert report["scope"] == (
        "structural_event_identity_addressability_not_semantic_recall"
    )
    assert report["aggregate"]["external_model_calls"] == 0
    assert all(report["safety_invariants"].values())
