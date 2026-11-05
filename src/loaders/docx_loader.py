import re
from typing import List

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.loaders.base import LoadContext, LoaderError, is_ole_container, register, rows_to_markdown
from src.schema import Element

_HEADING_STYLE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


def heading_level(paragraph: Paragraph) -> int:
    """Returns 1-9 for heading paragraphs (Title counts as 1), 0 otherwise."""
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    if name.lower() == "title":
        return 1
    match = _HEADING_STYLE.match(name.strip())
    return int(match.group(1)) if match else 0


def table_rows(table: Table) -> List[List[str]]:
    # python-docx returns a merged cell once per grid column, so merged values repeat on purpose
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


@register(".docx")
def load_docx(ctx: LoadContext) -> List[Element]:
    """Paragraphs grouped by heading section, plus one element per table, in document order."""
    if is_ole_container(ctx.path):
        raise LoaderError("file is password protected")
    try:
        doc = Document(ctx.path)
    except Exception as e:
        raise LoaderError(f"file is corrupt or not a Word document ({type(e).__name__})")

    elements: List[Element] = []
    heading_path: List[str] = []
    buf: List[str] = []
    buf_start = None
    paragraph_no = 0
    table_no = 0
    max_chars = ctx.settings.max_text_element_chars

    def section_label(first: int, last: int) -> str:
        if heading_path:
            return f'section "{heading_path[-1]}"'
        return f"paragraphs {first}-{last}"

    def flush():
        nonlocal buf, buf_start
        if buf:
            elements.append(
                ctx.make(
                    "text",
                    "\n\n".join(buf),
                    {"paragraph_start": buf_start, "paragraph_end": paragraph_no},
                    section_label(buf_start, paragraph_no),
                    heading_path=heading_path,
                    title=heading_path[-1] if heading_path else "",
                )
            )
        buf, buf_start = [], None

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, doc)
            text = paragraph.text.strip()
            if not text:
                continue
            level = heading_level(paragraph)
            if level:
                flush()
                heading_path = heading_path[: level - 1] + [text]
            if buf and sum(len(t) for t in buf) + len(text) > max_chars:
                flush()
            paragraph_no += 1
            if buf_start is None:
                buf_start = paragraph_no
            buf.append(text)
        elif child.tag == qn("w:tbl"):
            flush()
            rows = [r for r in table_rows(Table(child, doc)) if any(c for c in r)]
            if not rows:
                continue
            table_no += 1
            elements.append(
                ctx.make(
                    "table",
                    rows_to_markdown(rows[0], rows[1:]),
                    {"table_index": table_no},
                    f"table {table_no}" + (f' in "{heading_path[-1]}"' if heading_path else ""),
                    heading_path=heading_path,
                    title=heading_path[-1] if heading_path else "",
                    structured={"header": rows[0], "rows": rows[1:]},
                )
            )
    flush()
    return elements
