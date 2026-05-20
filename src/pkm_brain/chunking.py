from __future__ import annotations

from dataclasses import dataclass
import re

from .util import text_sha256, token_count

DEFAULT_TARGET_TOKENS = 1200
DEFAULT_OVERLAP_TOKENS = 200


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
    if source_type == "markdown_note":
        return chunk_markdown(text, target_tokens, overlap_tokens)
    return chunk_blocks(text, target_tokens, overlap_tokens)


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
