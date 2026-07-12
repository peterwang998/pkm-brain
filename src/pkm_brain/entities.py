from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .db import dumps, loads, rows
from .util import new_id, now_iso


ENTITY_TYPES = {
    "person",
    "organization",
    "product",
    "project",
    "concept",
    "place",
    "event",
    "other",
}
MENTION_KINDS = {"named", "concept", "generic", "deictic"}
DEFAULT_ADMIT_KINDS = {"named"}
ENTITY_TYPE_ALIASES = {
    "company": "organization",
    "companies": "organization",
    "org": "organization",
    "people": "person",
    "location": "place",
}
ENTITY_RESOLUTION_METHODS = {
    "exact",
    "alias",
    "fuzzy",
    "embedding",
    "llm",
    "human",
    "created",
}
ENTITY_RESOLVER_SCHEMA = {
    "type": "object",
    "required": ["decision"],
    "properties": {
        "decision": {"type": "string"},
        "entity_id": {"type": "string"},
        "rationale": {"type": "string"},
    },
}
NEW_ENTITY_CHOICE = "__new_entity__"


@dataclass(frozen=True)
class EntityResolution:
    entity_id: str
    resolution_method: str
    mention_text: str
    name: str
    entity_type: str | None = None


@dataclass(frozen=True)
class EntityCandidate:
    row: Any
    match_reason: str


def normalize_entity_name(value: Any) -> str:
    text = str(value or "").replace("_", " ")
    text = re.sub(r"[\u2018\u2019]", "'", text)
    text = re.sub(r"[^0-9A-Za-z'&]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def canonical_entity_name(value: Any) -> str:
    text = surface_entity_name(value)
    if text == "Unknown Entity":
        return "Unknown Entity"
    if text == text.lower() or "_" in str(value or ""):
        return " ".join(part[:1].upper() + part[1:] for part in text.split(" "))
    return text


def surface_entity_name(value: Any) -> str:
    text = str(value or "").replace("_", " ")
    text = re.sub(r"[^0-9A-Za-z'&]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Unknown Entity"


def resolve_entity(
    conn: Any,
    mention: Any,
    type_hint: str | None = None,
    *,
    source_ids: list[str] | None = None,
    create: bool = True,
    mention_kind: str | None = None,
    admit_kinds: set[str] | list[str] | tuple[str, ...] | None = None,
    paths: Any | None = None,
    llm_provider: Any | None = None,
    provider: str | None = None,
    context: dict[str, Any] | None = None,
) -> EntityResolution | None:
    mention_text = surface_entity_name(mention)
    entity_name = canonical_entity_name(mention)
    normalized = normalize_entity_name(mention_text)
    if not normalized:
        return None
    entity_type = normalize_entity_type(type_hint)
    normalized_kind = normalize_mention_kind(mention_kind)
    if admit_kinds is not None and normalized_kind not in normalize_admit_kinds(admit_kinds):
        return None
    candidates = candidate_entity_rows(conn, normalized, entity_type)
    if len(candidates) > 1:
        choice = disambiguate_entity_choice(
            mention_text,
            entity_type=entity_type,
            candidates=candidates,
            paths=paths,
            llm_provider=llm_provider,
            provider=provider,
            context=context,
        )
        if choice and choice != NEW_ENTITY_CHOICE:
            row = next(
                (
                    candidate.row
                    for candidate in candidates
                    if str(candidate.row["id"]) == choice
                ),
                None,
            )
            if row is not None:
                append_entity_alias(conn, str(row["id"]), mention_text)
                ensure_entity_type(conn, str(row["id"]), entity_type)
                return EntityResolution(
                    entity_id=str(row["id"]),
                    resolution_method="llm",
                    mention_text=mention_text,
                    name=str(row["name"]),
                    entity_type=row_entity_type(row) or entity_type,
                )
        if choice == NEW_ENTITY_CHOICE:
            candidates = []
    exact = first_candidate(candidates, "exact")
    if exact is not None:
        row = exact.row
        append_entity_alias(conn, str(row["id"]), mention_text)
        ensure_entity_type(conn, str(row["id"]), entity_type)
        return EntityResolution(
            entity_id=str(row["id"]),
            resolution_method="exact",
            mention_text=mention_text,
            name=str(row["name"]),
            entity_type=row_entity_type(row) or entity_type,
        )
    alias = first_candidate(candidates, "alias")
    if alias is not None:
        row = alias.row
        append_entity_alias(conn, str(row["id"]), mention_text)
        ensure_entity_type(conn, str(row["id"]), entity_type)
        return EntityResolution(
            entity_id=str(row["id"]),
            resolution_method="alias",
            mention_text=mention_text,
            name=str(row["name"]),
            entity_type=row_entity_type(row) or entity_type,
        )
    if not create:
        return None
    entity_id = new_id("entity")
    now = now_iso()
    conn.execute(
        """
        INSERT INTO entities(
          id, name, entity_type, aliases, status, source_ids, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id,
            entity_name,
            entity_type,
            dumps([mention_text] if mention_text != entity_name else []),
            "active",
            dumps(source_ids or []),
            now,
        ),
    )
    return EntityResolution(
        entity_id=entity_id,
        resolution_method="created",
        mention_text=mention_text,
        name=entity_name,
        entity_type=entity_type,
    )


def candidate_entity_rows(
    conn: Any,
    normalized: str,
    entity_type: str | None = None,
) -> list[EntityCandidate]:
    output: list[EntityCandidate] = []
    seen: set[str] = set()
    for row in active_entity_rows(conn):
        if not entity_type_compatible(row, entity_type):
            continue
        row_normalized = normalize_entity_name(row["name"])
        aliases = [
            normalize_entity_name(alias)
            for alias in loads(row["aliases"], [])
            if str(alias or "").strip()
        ]
        match_reason = ""
        if row_normalized == normalized:
            match_reason = "exact"
        elif normalized in aliases:
            match_reason = "alias"
        elif entity_names_overlap(normalized, row_normalized) or any(
            entity_names_overlap(normalized, alias) for alias in aliases
        ):
            match_reason = "lexical"
        if match_reason and str(row["id"]) not in seen:
            output.append(EntityCandidate(row=row, match_reason=match_reason))
            seen.add(str(row["id"]))
    return sorted(output, key=lambda candidate: match_rank(candidate.match_reason))


def first_candidate(
    candidates: list[EntityCandidate],
    match_reason: str,
) -> EntityCandidate | None:
    return next(
        (candidate for candidate in candidates if candidate.match_reason == match_reason),
        None,
    )


def match_rank(match_reason: str) -> tuple[int, str]:
    ranks = {"exact": 0, "alias": 1, "lexical": 2}
    return ranks.get(match_reason, 9), match_reason


def entity_names_overlap(left: str, right: str) -> bool:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return False
    if len(left_tokens | right_tokens) <= 1:
        return False
    return left_tokens < right_tokens or right_tokens < left_tokens


def find_entity_by_normalized_name(
    conn: Any,
    normalized: str,
    entity_type: str | None = None,
) -> Any | None:
    for row in active_entity_rows(conn):
        if not entity_type_compatible(row, normalize_entity_type(entity_type)):
            continue
        if normalize_entity_name(row["name"]) == normalized:
            return row
    return None


def find_entity_by_normalized_alias(
    conn: Any,
    normalized: str,
    entity_type: str | None = None,
) -> Any | None:
    for row in active_entity_rows(conn):
        if not entity_type_compatible(row, normalize_entity_type(entity_type)):
            continue
        for alias in loads(row["aliases"], []):
            if normalize_entity_name(alias) == normalized:
                return row
    return None


def active_entity_rows(conn: Any) -> list[Any]:
    return rows(
        conn,
        """
        SELECT *
        FROM entities
        WHERE COALESCE(status, 'active') = 'active'
        ORDER BY created_at, id
        """,
    )


def append_entity_alias(conn: Any, entity_id: str, mention_text: str) -> None:
    row = conn.execute("SELECT name, aliases FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return
    normalized_mention = normalize_entity_name(mention_text)
    if not normalized_mention or str(row["name"]) == mention_text:
        return
    aliases = [str(item) for item in loads(row["aliases"], []) if str(item or "").strip()]
    if any(alias == mention_text for alias in aliases):
        return
    aliases.append(mention_text)
    conn.execute("UPDATE entities SET aliases = ? WHERE id = ?", (dumps(aliases), entity_id))


def ensure_entity_type(conn: Any, entity_id: str, entity_type: str | None) -> None:
    normalized = normalize_entity_type(entity_type)
    if normalized is None:
        return
    row = conn.execute("SELECT entity_type FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None or row_entity_type(row) is not None:
        return
    conn.execute(
        "UPDATE entities SET entity_type = ? WHERE id = ?",
        (normalized, entity_id),
    )


def row_entity_type(row: Any) -> str | None:
    try:
        return normalize_entity_type(row["entity_type"])
    except (KeyError, TypeError, IndexError):
        return None


def entity_type_compatible(row: Any, entity_type: str | None) -> bool:
    normalized = normalize_entity_type(entity_type)
    existing = row_entity_type(row)
    return normalized is None or existing is None or existing == normalized


def upsert_primary_fact_entity(
    conn: Any,
    *,
    fact_id: str,
    entity_id: str,
    mention_text: str | None,
    mention_span: dict[str, Any] | None = None,
    mention_kind: str | None = None,
    resolution_method: str | None = None,
    confidence: float | None = None,
) -> None:
    method = (
        resolution_method
        if resolution_method in ENTITY_RESOLUTION_METHODS
        else None
    )
    existing = conn.execute(
        """
        SELECT id
        FROM fact_entities
        WHERE fact_id = ? AND is_primary = 1
        """,
        (fact_id,),
    ).fetchone()
    values = (
        entity_id,
        mention_text,
        dumps(mention_span) if mention_span else None,
        normalize_mention_kind(mention_kind),
        method,
        confidence,
    )
    if existing is not None:
        conn.execute(
            """
            UPDATE fact_entities
            SET entity_id = ?, mention_text = ?, mention_span = ?,
                mention_kind = ?, resolution_method = ?, confidence = ?
            WHERE id = ?
            """,
            (*values, existing["id"]),
        )
        return
    conn.execute(
        """
        INSERT INTO fact_entities(
          id, fact_id, entity_id, is_primary, mention_text, mention_span,
          mention_kind, resolution_method, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("factentity"),
            fact_id,
            entity_id,
            1,
            mention_text,
            dumps(mention_span) if mention_span else None,
            normalize_mention_kind(mention_kind),
            method,
            confidence,
            now_iso(),
        ),
    )


def replace_fact_entity_links(
    conn: Any,
    *,
    fact_id: str,
    links: list[dict[str, Any]],
) -> None:
    normalized_links = normalized_fact_entity_links(links)
    conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
    if not normalized_links:
        return
    now = now_iso()
    for link in normalized_links:
        conn.execute(
            """
            INSERT INTO fact_entities(
              id, fact_id, entity_id, is_primary, mention_text, mention_span,
              mention_kind, resolution_method, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("factentity"),
                fact_id,
                str(link["entity_id"]),
                1 if link.get("is_primary") else 0,
                link.get("mention_text"),
                dumps(link.get("mention_span")) if link.get("mention_span") else None,
                normalize_mention_kind(link.get("mention_kind")),
                link.get("resolution_method")
                if link.get("resolution_method") in ENTITY_RESOLUTION_METHODS
                else None,
                optional_float(link.get("confidence")),
                now,
            ),
        )


def normalized_fact_entity_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, bool, str]] = set()
    primary_seen = False
    for index, link in enumerate(links):
        entity_id = str(link.get("entity_id") or "").strip()
        if not entity_id:
            continue
        is_primary = bool(link.get("is_primary")) and not primary_seen
        if is_primary:
            primary_seen = True
        if index == 0 and not primary_seen:
            is_primary = True
            primary_seen = True
        mention_text = str(link.get("mention_text") or "").strip()
        key = (entity_id, is_primary, normalize_entity_name(mention_text))
        if key in seen:
            continue
        seen.add(key)
        output.append({**link, "entity_id": entity_id, "is_primary": is_primary})
    if output and not any(link.get("is_primary") for link in output):
        output[0]["is_primary"] = True
    return output


def normalize_entity_type(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    normalized = ENTITY_TYPE_ALIASES.get(normalized, normalized)
    return normalized if normalized in ENTITY_TYPES else None


def normalize_mention_kind(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "name": "named",
        "proper_name": "named",
        "proper_noun": "named",
        "named_entity": "named",
        "topic": "concept",
        "technical_concept": "concept",
        "generic_role": "generic",
        "role_class": "generic",
        "common_noun": "generic",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in MENTION_KINDS else None


def normalize_admit_kinds(value: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    if value is None:
        return set(DEFAULT_ADMIT_KINDS)
    admitted = {
        kind
        for item in value
        if (kind := normalize_mention_kind(item)) is not None
    }
    return admitted or set(DEFAULT_ADMIT_KINDS)


def disambiguate_entity_choice(
    mention_text: str,
    *,
    entity_type: str | None,
    candidates: list[EntityCandidate],
    paths: Any | None,
    llm_provider: Any | None,
    provider: str | None,
    context: dict[str, Any] | None,
) -> str | None:
    if not candidates:
        return None
    if llm_provider is None and paths is None:
        return None
    from .llm import complete_json, cos_role_provider_configured

    if llm_provider is None and not cos_role_provider_configured(paths, "resolver", provider=provider):
        return None
    parsed = complete_json(
        entity_disambiguation_prompt(
            mention_text,
            entity_type=entity_type,
            candidates=candidates,
            context=context,
        ),
        schema=ENTITY_RESOLVER_SCHEMA,
        role="resolver",
        provider=provider,
        llm_provider=llm_provider,
        paths=paths,
    )
    return normalize_entity_choice(parsed, candidates)


def entity_disambiguation_prompt(
    mention_text: str,
    *,
    entity_type: str | None,
    candidates: list[EntityCandidate],
    context: dict[str, Any] | None,
) -> str:
    candidate_cards = [
        {
            "id": candidate.row["id"],
            "name": candidate.row["name"],
            "entity_type": row_entity_type(candidate.row),
            "aliases": loads(candidate.row["aliases"], []),
            "description": candidate.row["description"],
            "match_reason": candidate.match_reason,
        }
        for candidate in candidates
    ]
    return (
        "Resolve this extracted entity mention against a closed candidate list. "
        "Return decision='existing' with one entity_id from the candidates, or decision='new' "
        "if none of the candidates is the same entity. Never invent an entity_id.\n\n"
        f"Mention: {mention_text}\n"
        f"Entity type: {entity_type or 'unknown'}\n"
        f"Context JSON: {dumps(context or {})}\n"
        f"Candidates JSON: {dumps(candidate_cards)}"
    )


def normalize_entity_choice(
    parsed: dict[str, Any],
    candidates: list[EntityCandidate],
) -> str | None:
    candidate_ids = {str(candidate.row["id"]) for candidate in candidates}
    entity_id = str(parsed.get("entity_id") or "").strip()
    if entity_id in candidate_ids:
        return entity_id
    decision = str(parsed.get("decision") or "").strip().lower()
    if decision in {"new", "create", "no_match", "none"}:
        return NEW_ENTITY_CHOICE
    return None


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
