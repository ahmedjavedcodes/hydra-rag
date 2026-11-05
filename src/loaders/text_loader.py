import re
from typing import List, Tuple

from src.loaders.base import LoadContext, LoaderError, register
from src.schema import Element

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def read_text_file(path: str) -> str:
    """Reads a text file, trying UTF-8 first and falling back to Latin-1."""
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:4096]:
        raise LoaderError("file is not plain text")
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LoaderError("could not decode text")


def group_paragraphs(lines: List[str], start_line: int, max_chars: int) -> List[Tuple[str, int, int]]:
    """Groups lines into chunks of whole paragraphs up to `max_chars`.

    Returns (text, first_line, last_line) with 1-based line numbers.
    """
    paragraphs = []  # (text, first, last)
    buf, first = [], None
    for offset, line in enumerate(lines):
        number = start_line + offset
        if line.strip():
            if first is None:
                first = number
            buf.append(line.rstrip())
        elif buf:
            paragraphs.append(("\n".join(buf), first, number - 1))
            buf, first = [], None
    if buf:
        paragraphs.append(("\n".join(buf), first, start_line + len(lines) - 1))

    groups = []
    cur, cur_first, cur_last = [], None, None
    for text, p_first, p_last in paragraphs:
        size = sum(len(t) for t in cur) + len(text)
        if cur and size > max_chars:
            groups.append(("\n\n".join(cur), cur_first, cur_last))
            cur, cur_first = [], None
        if cur_first is None:
            cur_first = p_first
        cur.append(text)
        cur_last = p_last
    if cur:
        groups.append(("\n\n".join(cur), cur_first, cur_last))
    return groups


def _lines_element(ctx: LoadContext, text: str, first: int, last: int, heading_path: List[str]) -> Element:
    return ctx.make(
        "text",
        text,
        {"line_start": first, "line_end": last},
        f"lines {first}-{last}",
        heading_path=heading_path,
        title=heading_path[-1] if heading_path else "",
    )


@register(".txt")
def load_txt(ctx: LoadContext) -> List[Element]:
    lines = read_text_file(ctx.path).splitlines()
    return [
        _lines_element(ctx, text, first, last, [])
        for text, first, last in group_paragraphs(lines, 1, ctx.settings.max_text_element_chars)
    ]


@register(".md", ".markdown")
def load_markdown(ctx: LoadContext) -> List[Element]:
    """Splits Markdown by headings so each element knows its heading path."""
    lines = read_text_file(ctx.path).splitlines()
    elements: List[Element] = []
    heading_path: List[str] = []
    section: List[str] = []
    section_start = 1
    in_code = False

    def flush():
        for text, first, last in group_paragraphs(section, section_start, ctx.settings.max_text_element_chars):
            elements.append(_lines_element(ctx, text, first, last, heading_path))

    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else _MD_HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            heading_path = heading_path[: level - 1] + [match.group(2)]
            section = [line]
            section_start = number
        else:
            section.append(line)
    flush()
    return elements
