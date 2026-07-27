from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field


MCPEventKind = Annotated[
    Literal["actual", "planned"],
    Field(
        description=(
            "Event-time filter. Use actual for occurrences and planned for schedules; "
            "requires event_as_of."
        )
    ),
]

MCPTemporalMode = Annotated[
    Literal["current", "valid", "known", "bitemporal", "timeline"],
    Field(
        description=(
            "Temporal view: current; valid with valid_as_of; known with known_as_of; "
            "bitemporal with both clocks; or timeline."
        )
    ),
]

MCPSearchMailLimit = Annotated[
    int,
    Field(
        ge=1,
        le=5,
        description=(
            "Maximum local Gmail results. Intentionally bounded to 1-5; open a "
            "matching thread with get_mail_thread for full context."
        ),
    ),
]
