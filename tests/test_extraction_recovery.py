import json
from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.extraction import (
    evidence_units_for_text,
    extraction_watermark_status,
    partial_extraction_watermark_is_terminal,
    validate_extracted_facts_with_report,
)
from pkm_brain.extraction_contract import EXTRACTION_SCHEMA
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def indexed_source(tmp_path: Path, text: str) -> tuple[BrainService, dict[str, str]]:
    service = BrainService(BrainPaths.from_value(tmp_path / "brain"))
    service.init_workspace()
    (service.paths.inbox / "source.md").write_text(
        f"# Source\n\n{text}", encoding="utf-8"
    )
    service.ingest()
    with connection(service.paths.sqlite_path) as conn:
        row = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()
    return service, {"id": str(row["id"]), "text": str(row["text"])}


def unit_ids_containing(text: str, needle: str) -> list[str]:
    return [
        str(unit["unit_id"])
        for unit in evidence_units_for_text(text)
        if needle in str(unit["text"])
    ]


def test_complete_extraction_schema_publishes_fixed_enums() -> None:
    fact = EXTRACTION_SCHEMA["properties"]["facts"]["items"]

    assert {"page_hint", "section_hint", "entities"} <= set(fact["required"])
    assert "factual_update" in fact["properties"]["claim_class"]["enum"]
    entity = fact["properties"]["entities"]["items"]
    assert entity["required"] == [
        "surface",
        "type",
        "mention_kind",
        "is_primary",
    ]


def test_unknown_claim_class_and_string_entities_do_not_discard_base_fact(
    tmp_path: Path,
) -> None:
    service, chunk = indexed_source(
        tmp_path,
        "Sunday work is generally not expected unless an employee chooses it.",
    )

    report = validate_extracted_facts_with_report(
        service.paths,
        [
            {
                "statement": (
                    "Sunday work is generally not expected unless an employee "
                    "chooses it."
                ),
                "chunk_id": chunk["id"],
                "evidence_unit_ids": unit_ids_containing(
                    chunk["text"], "Sunday work"
                ),
                "claim_class": "workplace_norm",
                "entities": ["employees", "Sunday work"],
            }
        ],
    )

    assert report["accepted_count"] == 1
    assert report["rejected_count"] == 0
    assert report["contract_recovery_warning_count"] == 2
    candidate = report["candidates"][0]
    assert candidate["claim_class"] == "factual_update"
    assert "entity_mentions" not in candidate
    assert {
        warning["enrichment"]
        for warning in candidate["metadata"]["contract_recovery_warnings"]
    } == {"claim_class", "entities"}


def test_malformed_entity_annotation_keeps_other_valid_mentions(tmp_path: Path) -> None:
    service, chunk = indexed_source(
        tmp_path,
        "Peter owns the product launch plan.",
    )

    report = validate_extracted_facts_with_report(
        service.paths,
        [
            {
                "statement": "Peter owns the product launch plan.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": unit_ids_containing(chunk["text"], "Peter owns"),
                "claim_class": "responsibility",
                "entities": [
                    {
                        "surface": "Peter",
                        "type": "person",
                        "mention_kind": "named",
                        "is_primary": True,
                    },
                    "product launch plan",
                ],
            }
        ],
    )

    candidate = report["candidates"][0]
    assert candidate["claim_class"] == "role_or_responsibility"
    assert [mention["surface"] for mention in candidate["entity_mentions"]] == [
        "Peter"
    ]
    assert report["contract_recovery_warning_count"] == 2


def test_structured_event_does_not_mask_failed_llm_extraction_watermark() -> None:
    validation = {
        "schema_errors": [],
        "rejected_count": 12,
        "structured_event_candidate_count": 1,
        "llm_candidate_count": 0,
        "rejections": [{"reasons": ["unknown claim_class: product_claim"]}],
    }
    structured = [{"extraction_method": "structured_metadata"}]

    assert extraction_watermark_status(validation, structured) == "invalid"
    assert (
        partial_extraction_watermark_is_terminal(
            {
                "status": "invalid",
                "metadata": json.dumps(
                    {"candidate_count": 1, "validation": validation}
                ),
            }
        )
        is False
    )
