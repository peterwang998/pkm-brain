from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.unrouted_resolution import (
    candidate_requires_route_resolution,
    decision_needs_output_retry,
    decisions_by_candidate_index,
    normalize_batch_decision_indexes,
    resolve_unrouted_candidate_routes,
)


class FakeRouteResolver:
    name = "fake-route-resolver"
    model = "fake-route-model"

    def __init__(self, decisions: list[dict[str, object]]) -> None:
        self.decisions = decisions
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps({"decisions": self.decisions})


class RetryingRouteResolver:
    name = "retrying-route-resolver"
    model = "retrying-route-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        index = self.calls
        self.calls += 1
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_index": index,
                        "decision": "route_existing",
                        "page_hint": "concepts/valid.md",
                        "confidence": 0.95,
                        "rationale": "Valid route.",
                    }
                ]
            }
        )


class EchoLocalRouteResolver:
    name = "echo-local-route-resolver"
    model = "echo-local-route-model"

    def __init__(self) -> None:
        self.prompt_indexes: list[list[int]] = []

    def complete(self, prompt: str) -> str:
        cards = json.loads(prompt.split("Routing cards JSON:\n", 1)[1])
        indexes = [int(card["candidate_index"]) for card in cards]
        self.prompt_indexes.append(indexes)
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_index": index,
                        "decision": "route_existing",
                        "page_hint": "concepts/valid.md",
                        "confidence": 0.95,
                        "rationale": "Valid route.",
                    }
                    for index in indexes
                ]
            }
        )


class InvalidThenValidRouteResolver:
    name = "invalid-then-valid-route-resolver"
    model = "invalid-then-valid-route-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return "not json"
        cards = json.loads(prompt.split("Routing cards JSON:\n", 1)[1])
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_index": cards[0]["candidate_index"],
                        "decision": "route_existing",
                        "page_hint": "concepts/valid.md",
                        "confidence": 0.95,
                        "rationale": "The complete card supports this route.",
                    }
                ]
            }
        )


class CorrectingCompanyRouteResolver:
    name = "correcting-company-route-resolver"
    model = "correcting-company-route-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        page_hint = (
            "companies/databricks.md"
            if self.calls == 1
            else "companies/snowflake.md"
        )
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_index": 0,
                        "decision": "route_existing",
                        "page_hint": page_hint,
                        "confidence": 0.95,
                        "rationale": "The company route appears plausible.",
                    }
                ]
            }
        )


def unresolved_candidate(statement: str) -> dict[str, object]:
    return {
        "statement": statement,
        "page_hint": "concepts/extracted-facts.md",
        "section_hint": "Summary",
        "entity_key": "concepts:concepts-extracted-facts:summary",
        "metadata": {
            "routing": {
                "route_destination_valid": False,
                "route_review_reason": "fallback_page",
            }
        },
    }


def unresolved_company_candidate(
    statement: str, organization: str
) -> dict[str, object]:
    candidate = unresolved_candidate(statement)
    candidate["metadata"]["model_entity_mentions"] = [
        {
            "surface": organization,
            "entity_type": "organization",
            "mention_kind": "named",
            "is_primary": False,
        }
    ]
    return candidate


def test_route_resolver_routes_existing_and_creates_missing_canonical_page(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = FakeRouteResolver(
        [
            {
                "candidate_index": 0,
                "decision": "route_existing",
                "page_hint": "career/agent-pm-role-workload-and-strategy.md",
                "confidence": 0.94,
                "rationale": "The source and sibling topic match this role page.",
            },
            {
                "candidate_index": 1,
                "decision": "create_new_page",
                "page_hint": "open_loops/netflix-pm-interview-preparation.md",
                "confidence": 0.91,
                "rationale": "The source establishes a distinct interview-preparation topic.",
            },
        ]
    )
    route_targets = {
        "career/agent-pm-role-workload-and-strategy.md": {
            "page_hint": "career/agent-pm-role-workload-and-strategy.md",
            "canonical_entity": "Agent PM Role Workload and Strategy",
            "page_scope": "career",
        }
    }

    routed = resolve_unrouted_candidate_routes(
        paths,
        [
            unresolved_candidate("Agent PMs contribute to platform work."),
            unresolved_candidate("Peter is preparing for a Netflix PM interview."),
        ],
        route_targets,
        llm_provider=provider,
    )

    assert routed[0]["page_hint"] == "career/agent-pm-role-workload-and-strategy.md"
    assert routed[0]["metadata"]["routing"]["route_target_exists"] is True
    assert routed[1]["page_hint"] == "open_loops/netflix-pm-interview-preparation.md"
    assert routed[1]["metadata"]["routing"]["route_target_exists"] is False
    assert all(not candidate_requires_route_resolution(item) for item in routed)
    assert "Facts from one source document" in provider.prompts[0]
    assert "needs_human" in provider.prompts[0]


def test_route_resolver_keeps_low_confidence_or_invalid_choices_for_human_review(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = FakeRouteResolver(
        [
            {
                "candidate_index": 0,
                "decision": "route_existing",
                "page_hint": "concepts/missing.md",
                "confidence": 0.95,
                "rationale": "Not an existing target.",
            },
            {
                "candidate_index": 1,
                "decision": "create_new_page",
                "page_hint": "inbox/unsafe.md",
                "confidence": 0.95,
                "rationale": "Invalid namespace.",
            },
            {
                "candidate_index": 2,
                "decision": "route_existing",
                "page_hint": "concepts/valid.md",
                "confidence": 0.6,
                "rationale": "Too uncertain.",
            },
        ]
    )
    candidates = [
        unresolved_candidate("First fact."),
        unresolved_candidate("Second fact."),
        unresolved_candidate("Third fact."),
    ]

    routed = resolve_unrouted_candidate_routes(
        paths,
        candidates,
        {"concepts/valid.md": {"canonical_entity": "Valid"}},
        llm_provider=provider,
    )

    assert all(candidate_requires_route_resolution(item) for item in routed)


def test_route_resolver_normalizes_batch_local_candidate_indexes() -> None:
    decisions = {
        0: {"candidate_index": 0, "decision": "route_existing"},
        1: {"candidate_index": 1, "decision": "needs_human"},
    }

    normalized = normalize_batch_decision_indexes(decisions, [12, 13])

    assert set(normalized) == {12, 13}


def test_route_resolver_accepts_schema_shaped_luna_response() -> None:
    parsed = {
        "properties": {
            "decisions": {
                "items": [
                    {
                        "candidate_index": 0,
                        "decision": "route_existing",
                        "page_hint": "concepts/valid.md",
                        "confidence": 0.9,
                        "rationale": "Valid route.",
                    }
                ]
            }
        }
    }

    assert decisions_by_candidate_index(parsed)[0]["page_hint"] == "concepts/valid.md"


def test_route_resolver_prefers_local_indexes_for_noncontiguous_retry() -> None:
    decisions = {
        1: {"candidate_index": 1, "decision": "route_existing"},
    }

    normalized = normalize_batch_decision_indexes(decisions, [1, 4])

    assert set(normalized) == {4}


def test_route_resolver_uses_compact_indexes_for_every_model_batch(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = EchoLocalRouteResolver()

    routed = resolve_unrouted_candidate_routes(
        paths,
        [unresolved_candidate(f"Fact {index}.") for index in range(8)],
        {"concepts/valid.md": {"canonical_entity": "Valid"}},
        llm_provider=provider,
    )

    assert provider.prompt_indexes == [list(range(6)), [0, 1]]
    assert all(item["page_hint"] == "concepts/valid.md" for item in routed)


def test_route_resolver_retries_omitted_candidate_indexes(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = RetryingRouteResolver()

    routed = resolve_unrouted_candidate_routes(
        paths,
        [unresolved_candidate("First fact."), unresolved_candidate("Second fact.")],
        {"concepts/valid.md": {"canonical_entity": "Valid"}},
        llm_provider=provider,
    )

    assert provider.calls == 2
    assert [item["page_hint"] for item in routed] == [
        "concepts/valid.md",
        "concepts/valid.md",
    ]


def test_route_resolver_resends_complete_prompt_after_invalid_json(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = InvalidThenValidRouteResolver()

    routed = resolve_unrouted_candidate_routes(
        paths,
        [unresolved_candidate("The complete routing card must survive retry.")],
        {"concepts/valid.md": {"canonical_entity": "Valid"}},
        llm_provider=provider,
    )

    assert routed[0]["page_hint"] == "concepts/valid.md"
    assert len(provider.prompts) == 2
    assert all("The complete routing card must survive retry." in prompt for prompt in provider.prompts)


def test_route_resolver_retries_cross_company_route(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = CorrectingCompanyRouteResolver()

    routed = resolve_unrouted_candidate_routes(
        paths,
        [
            unresolved_company_candidate(
                "Venky is working on migrations to Snowflake.", "Snowflake"
            )
        ],
        {
            "companies/databricks.md": {"canonical_entity": "Databricks"},
            "companies/snowflake.md": {"canonical_entity": "Snowflake"},
        },
        llm_provider=provider,
    )

    assert provider.calls == 2
    assert routed[0]["page_hint"] == "companies/snowflake.md"


def test_route_resolver_collapses_new_company_subtopic_to_canonical_page(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = FakeRouteResolver(
        [
            {
                "candidate_index": 0,
                "decision": "create_new_page",
                "page_hint": "companies/greylock-talent-network.md",
                "confidence": 0.95,
                "rationale": "Greylock is a durable company topic.",
            }
        ]
    )

    routed = resolve_unrouted_candidate_routes(
        paths,
        [unresolved_company_candidate("Greylock builds a talent network.", "Greylock")],
        {},
        llm_provider=provider,
    )

    assert routed[0]["page_hint"] == "companies/greylock.md"


def test_route_resolver_preserves_existing_company_topic_page(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    provider = FakeRouteResolver(
        [
            {
                "candidate_index": 0,
                "decision": "create_new_page",
                "page_hint": "companies/snowflake-culture.md",
                "confidence": 0.95,
                "rationale": "The existing topical page fits.",
            }
        ]
    )

    routed = resolve_unrouted_candidate_routes(
        paths,
        [
            unresolved_company_candidate(
                "Snowflake has a collaborative culture.", "Snowflake"
            )
        ],
        {
            "companies/snowflake.md": {"canonical_entity": "Snowflake"},
            "companies/snowflake-culture.md": {
                "canonical_entity": "Snowflake Culture"
            },
        },
        llm_provider=provider,
    )

    assert routed[0]["page_hint"] == "companies/snowflake-culture.md"


def test_route_resolver_uses_active_autonomy_confidence_floor(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        "curation:\n  strictness: lenient\n",
        encoding="utf-8",
    )
    provider = FakeRouteResolver(
        [
            {
                "candidate_index": 0,
                "decision": "route_existing",
                "page_hint": "concepts/valid.md",
                "confidence": 0.7,
                "rationale": "Plausible under the more-autonomous profile.",
            }
        ]
    )

    routed = resolve_unrouted_candidate_routes(
        paths,
        [unresolved_candidate("A plausibly routed fact.")],
        {"concepts/valid.md": {"canonical_entity": "Valid"}},
        llm_provider=provider,
    )

    assert routed[0]["page_hint"] == "concepts/valid.md"


def test_route_resolver_retries_false_missing_card_rationale() -> None:
    assert decision_needs_output_retry(
        {
            "decision": "needs_human",
            "confidence": 0.1,
            "rationale": "The routing card for this candidate is not present.",
        }
    )
    assert decision_needs_output_retry(
        {
            "decision": "needs_human",
            "confidence": 0.1,
            "rationale": "The candidate's source fact and routing context are unavailable.",
        }
    )
    assert decision_needs_output_retry(
        {
            "decision": "needs_human",
            "confidence": 0.1,
            "rationale": "Candidate details were not provided in the prompt.",
        }
    )
    assert decision_needs_output_retry(
        {
            "decision": "route_existing",
            "confidence": None,
            "page_hint": "concepts/valid.md",
            "rationale": "Valid route.",
        }
    )
    assert not decision_needs_output_retry(
        {
            "decision": "needs_human",
            "confidence": 0.9,
            "page_hint": "",
            "rationale": "Two materially different destinations remain plausible.",
        }
    )
