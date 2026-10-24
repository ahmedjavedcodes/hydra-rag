"""Elements -> chunks. Text is split by size with overlap on paragraph/sentence/word boundaries
(loaders already split by heading), tables keep their header row in every chunk, and image and
chart descriptions stay whole."""

import re
from dataclasses import dataclass
from typing import Dict, List

from src.config import RagSettings, rag_settings
from src.schema import hash_content

_SEPARATORS = ["\n\n", "\n", r"(?<=[.!?])\s+", " "]


@dataclass
class Chunk:
    id: str  # doc_id:element_id:chunk_n
    text: str  # the passage shown in sources
    embed_text: str  # what gets embedded: heading/title context + passage
    metadata: Dict  # flat, Pinecone-safe citation fields
    content_hash: str


def _atoms(text: str, size: int, level: int = 0) -> List[str]:
    """Splits text into pieces no longer than `size`, preferring the coarsest separator."""
    if len(text) <= size:
        return [text]
    if level >= len(_SEPARATORS):
        return [text[i : i + size] for i in range(0, len(text), size)]
    parts = [p for p in re.split(_SEPARATORS[level], text) if p.strip()]
    if len(parts) == 1:
        return _atoms(text, size, level + 1)
    out = []
    for part in parts:
        out.extend(_atoms(part.strip(), size, level + 1))
    return out


def split_text(text: str, size: int, overlap: int) -> List[str]:
    """Packs pieces into chunks of at most `size` chars; each chunk starts with up to `overlap`
    chars of the previous chunk's tail (whole pieces only)."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, current = [], []
    for atom in _atoms(text, size):
        if current and len(" ".join(current + [atom])) > size:
            chunks.append(" ".join(current))
            tail = []
            for piece in reversed(current):
                if len(" ".join([piece] + tail)) > overlap:
                    break
                tail.insert(0, piece)
            if not tail and overlap:
                # Last piece is longer than the overlap: carry its final words instead
                words = current[-1].split()
                while words and len(" ".join(words)) > overlap:
                    words.pop(0)
                tail = [" ".join(words)] if words else []
            current = tail if len(" ".join(tail + [atom])) <= size else []
        current.append(atom)
    if current:
        chunks.append(" ".join(current))
    return chunks


def split_table(text: str, structured: Dict, max_size: int) -> List[Dict]:
    """Splits a table into row groups that each repeat the header.

    Returns dicts with `text` and, when the loader recorded spreadsheet row numbers,
    `row_start`/`row_end` for that group.
    """
    if len(text) <= max_size:
        return [{"text": text}]
    lines = text.splitlines()
    header_lines = 2 if lines and lines[0].startswith("|") else 1  # Markdown header + separator, or CSV header
    header, rows = lines[:header_lines], lines[header_lines:]
    numbers = structured.get("row_numbers") or []
    groups, current, start = [], [], 0
    for i, row in enumerate(rows):
        if current and len("\n".join(header + current + [row])) > max_size:
            groups.append((start, i - 1, current))
            current, start = [], i
        current.append(row)
    if current:
        groups.append((start, len(rows) - 1, current))

    out = []
    for first, last, group in groups:
        item = {"text": "\n".join(header + group)}
        if len(numbers) == len(rows):
            item["row_start"], item["row_end"] = numbers[first], numbers[last]
        out.append(item)
    return out


def _location_label(meta: Dict) -> str:
    """Rebuilds the human label when a table chunk covers fewer rows than its element."""
    label = f"rows {meta['row_start']}-{meta['row_end']}"
    return f"{meta['sheet']}, {label}" if meta.get("sheet") else label


def citation_metadata(element: Dict) -> Dict:
    """Flat, Pinecone-safe metadata (strings, numbers, booleans, lists of strings; no nulls)."""
    meta = element["metadata"]
    out = {
        "doc_id": element["doc_id"],
        "version_id": element["version_id"],
        "element_id": element["element_id"],
        "type": element["type"],
        "title": element.get("title") or "",
        "heading_path": [str(h) for h in element.get("heading_path") or []],
        "filename": meta.get("filename") or "",
        "location": meta.get("location") or "",
    }
    for key in ("source_url", "page", "sheet", "row_start", "row_end", "slide", "notes", "hidden"):
        value = meta.get(key, element.get(key) if key == "page" else None)
        if value is not None and value != "":
            out[key] = value
    return out


def chunk_element(element: Dict, settings: RagSettings = rag_settings) -> List[Chunk]:
    """Chunks one element (as stored in elements/<doc_id>.jsonl). Empty elements give no chunks."""
    text = (element.get("content_text") or "").strip()
    if not text:
        return []
    base = citation_metadata(element)
    if element["type"] == "table":
        pieces = split_table(text, element.get("content_structured") or {}, settings.table_chunk_size)
    elif element["type"] in ("image", "chart"):
        pieces = [{"text": text}]
    else:
        pieces = [{"text": t} for t in split_text(text, settings.chunk_size, settings.chunk_overlap)]

    context = " > ".join(base["heading_path"]) or base["title"]
    chunks = []
    for n, piece in enumerate(pieces):
        meta = dict(base, chunk_index=n)
        if "row_start" in piece:
            meta["row_start"], meta["row_end"] = piece["row_start"], piece["row_end"]
            meta["location"] = _location_label(meta)
        passage = piece["text"]
        embed_text = f"{context}\n{passage}" if context and context not in passage[: len(context) + 5] else passage
        content_hash = hash_content(embed_text)
        meta["content_hash"] = content_hash
        chunks.append(
            Chunk(
                id=f"{element['doc_id']}:{element['element_id']}:{n}",
                text=passage,
                embed_text=embed_text,
                metadata=meta,
                content_hash=content_hash,
            )
        )
    return chunks


def chunk_elements(elements: List[Dict], settings: RagSettings = rag_settings) -> List[Chunk]:
    chunks = []
    for element in elements:
        chunks.extend(chunk_element(element, settings))
    return chunks
