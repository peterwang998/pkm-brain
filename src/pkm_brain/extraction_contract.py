from __future__ import annotations

from typing import Any


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "statement",
                    "chunk_id",
                    "evidence_unit_ids",
                    "page_hint",
                    "section_hint",
                    "claim_class",
                    "entities",
                    "extraction_confidence",
                    "routing_confidence",
                    "truth_confidence",
                ],
                "properties": {
                    "statement": {"type": "string"},
                    "chunk_id": {"type": "string"},
                    "evidence_unit_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "page_hint": {"type": "string"},
                    "section_hint": {"type": "string"},
                    "claim_class": {
                        "type": "string",
                        "enum": [
                            "decision",
                            "commitment",
                            "preference",
                            "role_or_responsibility",
                            "project_state",
                            "factual_update",
                            "open_question",
                            "event_metadata",
                            "transcript_mechanic",
                            "pleasantry",
                            "boilerplate",
                            "non_claim",
                        ],
                    },
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "surface",
                                "type",
                                "mention_kind",
                                "is_primary",
                            ],
                            "properties": {
                                "surface": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "concept",
                                        "event",
                                        "organization",
                                        "other",
                                        "person",
                                        "place",
                                        "product",
                                        "project",
                                    ],
                                },
                                "mention_kind": {
                                    "type": "string",
                                    "enum": [
                                        "concept",
                                        "deictic",
                                        "generic",
                                        "named",
                                    ],
                                },
                                "is_primary": {"type": "boolean"},
                            },
                        },
                    },
                    "extraction_confidence": {"type": "number"},
                    "routing_confidence": {"type": "number"},
                    "truth_confidence": {"type": "number"},
                    "event_time": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["actual", "planned"],
                            },
                            "start_at": {"type": "string"},
                            "end_at": {"type": "string"},
                            "precision": {
                                "type": "string",
                                "enum": ["exact", "day", "month", "year"],
                            },
                            "expression": {"type": "string"},
                        },
                    }
                },
            },
        }
    },
}

EXTRACTION_PROMPT_VERSION = "extractor-evidence-units-v15-gmail-event-time"
# v15 adds evidence-grounded Gmail event-time stabilization on top of the v14
# speech-act and durability gates. Older successes are revisited by default so
# previously stripped or unsafe event clocks are re-evaluated; deployments may
# explicitly fence verified compatible successes during an in-place migration.
