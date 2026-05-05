from __future__ import annotations

from dataclasses import dataclass

from .util import text_sha256, token_count


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    text: str
    heading_path: str
    start_offset: int
    end_offset: int
    token_count: int
    content_hash: str


def chunk_text(text: str, source_type: str, target_tokens: int = 650) -> list[Chunk]:
    if source_type == "markdown_note":
        return chunk_markdown(text, target_tokens)
    return chunk_blocks(text, target_tokens)


def chunk_markdown(text: str, target_tokens: int = 650) -> list[Chunk]:
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
        chunks.extend(_split_section(section_text, heading, section_start, len(chunks), target_tokens))
    return chunks


def chunk_blocks(text: str, target_tokens: int = 650) -> list[Chunk]:
    paragraphs = text.split("\n\n")
    chunks: list[Chunk] = []
    current: list[str] = []
    current_start = 0
    offset = 0
    for para in paragraphs:
        block = para + "\n\n"
        if not current:
            current_start = offset
        if current and token_count("".join(current) + block) > target_tokens:
            chunks.append(_make_chunk(len(chunks), "".join(current).strip(), "", current_start, offset))
            current = [block]
            current_start = offset
        else:
            current.append(block)
        offset += len(block)
    if current:
        chunks.append(_make_chunk(len(chunks), "".join(current).strip(), "", current_start, len(text)))
    return [chunk for chunk in chunks if chunk.text]


def _split_section(
    text: str,
    heading_path: str,
    start_offset: int,
    start_index: int,
    target_tokens: int,
) -> list[Chunk]:
    words = text.split()
    if len(words) <= target_tokens:
        return [_make_chunk(start_index, text.strip(), heading_path, start_offset, start_offset + len(text))]

    chunks: list[Chunk] = []
    cursor = 0
    while cursor < len(words):
        piece_words = words[cursor: cursor + target_tokens]
        piece = " ".join(piece_words)
        chunks.append(
            _make_chunk(
                start_index + len(chunks),
                piece,
                heading_path,
                start_offset,
                start_offset + len(text),
            )
        )
        cursor += target_tokens
    return chunks


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
