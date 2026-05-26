from __future__ import annotations

from dataclasses import dataclass
import re

from .util import text_sha256, token_count

DEFAULT_TARGET_TOKENS = 1200
DEFAULT_OVERLAP_TOKENS = 200
MAX_AGENT_SESSION_BLOCK_CHARS = 3000
MAX_AGENT_SESSION_LINE_CHARS = 1200

RETRIEVAL_BLOB_MARKERS = (
    '"active_memories"',
    '"candidate_memories"',
    '"citation_snapshots"',
    '"content_hash"',
    '"raw_context"',
    '"retrieval_event_id"',
    '"returned_chunk_ids"',
    '"selected_chunk_ids"',
    '"supporting_chunks"',
    "active_memories:",
    "candidate_memories:",
    "citation_snapshots:",
    "retrieval_event_id:",
    "supporting_chunks:",
)
TOOL_OUTPUT_MARKERS = (
    "chunk id:",
    "mcp__",
    "original token count:",
    "output:",
    "process exited with code",
    "stderr",
    "stdout",
    "tool call",
    "traceback",
    "uv run brain retrieve-context",
    "wall time:",
)


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    text: str
    heading_path: str
    start_offset: int
    end_offset: int
    token_count: int
    content_hash: str


def chunk_text(
    text: str,
    source_type: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    text = prepare_text_for_indexing(text, source_type)
    if source_type == "markdown_note":
        return chunk_markdown(text, target_tokens, overlap_tokens)
    return chunk_blocks(text, target_tokens, overlap_tokens)


def prepare_text_for_indexing(text: str, source_type: str) -> str:
    if source_type == "agent_session_log":
        return sanitize_agent_session_log(text)
    return text


def sanitize_agent_session_log(text: str) -> str:
    cleaned = strip_frontmatter(text)
    blocks = re.split(r"\n\s*\n", cleaned)
    output: list[str] = []
    for block in blocks:
        sanitized = sanitize_agent_session_block(block)
        if sanitized:
            output.append(sanitized)
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(output)).strip()


def strip_frontmatter(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text.strip()
    end = stripped.find("\n---", 3)
    if end == -1:
        return text.strip()
    return stripped[end + 4 :].strip()


def sanitize_agent_session_block(block: str) -> str:
    stripped = block.strip()
    if not stripped:
        return ""
    lowered = stripped.lower()
    if lowered.startswith("you are codex") or lowered.startswith("<permissions instructions>"):
        return ""
    if looks_like_retrieval_blob(stripped):
        return "[omitted retrieved context dump]"
    if looks_like_large_tool_output(stripped):
        return "[omitted large tool output]"
    if looks_like_large_json_blob(stripped):
        return "[omitted large JSON tool result]"

    lines: list[str] = []
    omitted_long_lines = 0
    for line in stripped.splitlines():
        line_lower = line.lower()
        normalized = line_lower.lstrip("- ")
        if "session_meta:" in line_lower:
            continue
        if "you are codex" in line_lower or "<permissions instructions>" in line_lower:
            continue
        if normalized.startswith("event_msg:") and len(line) > MAX_AGENT_SESSION_LINE_CHARS:
            omitted_long_lines += 1
            continue
        if normalized.startswith("response_item:") and len(line) > MAX_AGENT_SESSION_LINE_CHARS:
            omitted_long_lines += 1
            continue
        if any(marker in line_lower for marker in RETRIEVAL_BLOB_MARKERS):
            omitted_long_lines += 1
            continue
        if len(line) > MAX_AGENT_SESSION_LINE_CHARS and (
            looks_like_large_json_blob(line) or any(marker in line_lower for marker in TOOL_OUTPUT_MARKERS)
        ):
            omitted_long_lines += 1
            continue
        lines.append(line)
    if omitted_long_lines:
        lines.append(f"[omitted {omitted_long_lines} noisy log line(s)]")
    return "\n".join(lines).strip()


def looks_like_retrieval_blob(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in RETRIEVAL_BLOB_MARKERS):
        return True
    chunk_markers = lowered.count('"chunk_id"') + lowered.count("chunk_id:")
    document_markers = lowered.count('"document_id"') + lowered.count("document_id:")
    return chunk_markers >= 2 and document_markers >= 2 and ('"text"' in lowered or "text:" in lowered)


def looks_like_large_tool_output(text: str) -> bool:
    if len(text) <= MAX_AGENT_SESSION_BLOCK_CHARS:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in TOOL_OUTPUT_MARKERS)


def looks_like_large_json_blob(text: str) -> bool:
    stripped = text.lstrip("- \t")
    if len(stripped) <= MAX_AGENT_SESSION_BLOCK_CHARS:
        return False
    if not stripped.startswith(("{", "[")):
        return False
    return stripped.count("{") + stripped.count("[") >= 4 or '":' in stripped


def chunk_markdown(
    text: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    lines = text.splitlines(keepends=True)
    sections: list[tuple[str, int, str]] = []
    headings: list[str] = []
    current: list[str] = []
    start = 0
    offset = 0
    heading_path = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes <= 6 and stripped[hashes:hashes + 1] == " ":
                if current:
                    sections.append((heading_path, start, "".join(current)))
                title = stripped[hashes:].strip()
                headings = headings[: hashes - 1] + [title]
                heading_path = " > ".join(headings)
                current = [line]
                start = offset
                offset += len(line)
                continue
        if not current:
            start = offset
        current.append(line)
        offset += len(line)

    if current:
        sections.append((heading_path, start, "".join(current)))

    chunks: list[Chunk] = []
    for heading, section_start, section_text in sections or [("", 0, text)]:
        chunks.extend(_split_section(section_text, heading, section_start, len(chunks), target_tokens, overlap_tokens))
    return chunks


def chunk_blocks(
    text: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[str] = []
    current_start = 0
    current_end = 0
    for block_start, block_end, block in _paragraph_blocks(text):
        if not block.strip():
            continue
        if token_count(block) > target_tokens:
            if current:
                chunks.append(_make_chunk(len(chunks), "".join(current).strip(), "", current_start, current_end))
                current = []
            chunks.extend(_split_section(block, "", block_start, len(chunks), target_tokens, overlap_tokens))
            continue
        if not current:
            current_start = block_start
        if current and token_count("".join(current) + block) > target_tokens:
            chunks.append(_make_chunk(len(chunks), "".join(current).strip(), "", current_start, current_end))
            current = [block]
            current_start = block_start
        else:
            current.append(block)
        current_end = block_end
    if current:
        chunks.append(_make_chunk(len(chunks), "".join(current).strip(), "", current_start, current_end))
    return [chunk for chunk in chunks if chunk.text]


def _split_section(
    text: str,
    heading_path: str,
    start_offset: int,
    start_index: int,
    target_tokens: int,
    overlap_tokens: int,
) -> list[Chunk]:
    spans = list(re.finditer(r"\S+", text))
    if len(spans) <= target_tokens:
        return [_make_chunk(start_index, text.strip(), heading_path, start_offset, start_offset + len(text))]

    chunks: list[Chunk] = []
    cursor = 0
    step = max(1, target_tokens - min(overlap_tokens, target_tokens - 1))
    while cursor < len(spans):
        end_cursor = min(cursor + target_tokens, len(spans))
        piece_start = spans[cursor].start()
        piece_end = spans[end_cursor - 1].end()
        piece = text[piece_start:piece_end].strip()
        chunks.append(
            _make_chunk(
                start_index + len(chunks),
                piece,
                heading_path,
                start_offset + piece_start,
                start_offset + piece_end,
            )
        )
        if end_cursor == len(spans):
            break
        cursor += step
    return chunks


def _paragraph_blocks(text: str) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    offset = 0
    parts = text.split("\n\n")
    for index, part in enumerate(parts):
        separator = "\n\n" if index < len(parts) - 1 else ""
        block = part + separator
        start = offset
        end = start + len(block)
        blocks.append((start, end, block))
        offset = end
    return blocks


def _make_chunk(index: int, text: str, heading_path: str, start: int, end: int) -> Chunk:
    return Chunk(
        chunk_index=index,
        text=text,
        heading_path=heading_path,
        start_offset=start,
        end_offset=end,
        token_count=token_count(text),
        content_hash=text_sha256(text),
    )
