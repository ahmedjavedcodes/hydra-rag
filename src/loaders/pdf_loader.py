import csv
import io
from typing import List

import pymupdf as fitz

from src.loaders.base import LoadContext, LoaderError, image_element, register
from src.schema import Element

# Below this many characters a page is treated as scanned (no text layer).
SCANNED_PAGE_MIN_CHARS = 20


def matrix_to_csv(data_matrix) -> str:
    """Converts a 2D matrix (list of lists) into a clean CSV string."""
    if not data_matrix:
        return ""
    output = io.StringIO()
    writer = csv.writer(output)
    for row in data_matrix:
        cleaned_row = [str(cell).replace("\n", " ").strip() if cell is not None else "" for cell in row]
        writer.writerow(cleaned_row)
    return output.getvalue().strip()


def get_surrounding_text(text_blocks, bbox, v_margin=100, h_margin=50) -> str:
    """Finds text blocks near a visual element's bounding box (captions/context)."""
    context = []
    element_rect = fitz.Rect(bbox)

    for block in text_blocks:
        if block[6] == 0:  # Text block
            block_rect = fitz.Rect(block[:4])
            v_close = (
                abs(element_rect.y0 - block_rect.y1) < v_margin or abs(block_rect.y0 - element_rect.y1) < v_margin
            )
            h_overlap = block_rect.x1 >= element_rect.x0 - h_margin and block_rect.x0 <= element_rect.x1 + h_margin
            if v_close and h_overlap:
                context.append(block[4].strip())

    return " ".join(context)


def toc_heading_paths(doc) -> dict:
    """Maps page number -> heading path from the PDF outline (empty when the PDF has none)."""
    toc = doc.get_toc(simple=True)  # [level, title, page]
    paths, current = {}, []
    entries = sorted(((page, i, level, title) for i, (level, title, page) in enumerate(toc)), key=lambda e: (e[0], e[1]))
    idx = 0
    for page_no in range(1, doc.page_count + 1):
        while idx < len(entries) and entries[idx][0] <= page_no:
            _, _, level, title = entries[idx]
            current = current[: level - 1] + [title.strip()]
            idx += 1
        paths[page_no] = list(current)
    return paths


def _open(path: str):
    try:
        # Opened from bytes so a failed open never leaves the file locked (Windows)
        with open(path, "rb") as f:
            doc = fitz.open(stream=f.read(), filetype="pdf")
    except Exception as e:
        raise LoaderError(f"file is corrupt or not a PDF ({type(e).__name__})")
    if doc.needs_pass:
        doc.close()
        raise LoaderError("file is password protected")
    return doc


@register(".pdf")
def load_pdf(ctx: LoadContext) -> List[Element]:
    """Prose text per page (outside tables), tables as CSV, images and scanned pages described by the vision model."""
    doc = _open(ctx.path)
    elements: List[Element] = []
    # Vision work is collected first and run in parallel at the end: (job, element kwargs)
    pending = []
    scanned_skipped = 0

    try:
        headings = toc_heading_paths(doc)
        for page in doc:
            page_no = page.number + 1
            loc = {"page": page_no}
            label = f"p.{page_no}"
            heading_path = headings.get(page_no, [])
            title = heading_path[-1] if heading_path else ""
            text_blocks = page.get_text("blocks")
            try:
                tables = list(page.find_tables())
            except Exception:
                tables = []
            table_rects = [fitz.Rect(tab.bbox) for tab in tables]

            # 1. Prose text only (text inside tables is left to the table elements)
            prose = []
            for block in text_blocks:
                if block[6] == 0:
                    block_rect = fitz.Rect(block[:4])
                    if not any(block_rect.intersects(t) for t in table_rects) and block[4].strip():
                        prose.append(block[4].strip())
            page_text = "\n\n".join(prose)

            images = page.get_images(full=True)
            if len(page_text) < SCANNED_PAGE_MIN_CHARS and not tables:
                # 2a. Scanned page: render it and let the vision model transcribe it
                if ctx.take_image_slot():
                    png = page.get_pixmap(dpi=150).tobytes("png")
                    pending.append(
                        (
                            (png, "", "page"),
                            dict(
                                type="text",
                                location=loc,
                                label=label,
                                page=page_no,
                                heading_path=heading_path,
                                title=title,
                                extra={"scanned_page": True},
                            ),
                        )
                    )
                else:
                    scanned_skipped += 1
                continue

            if page_text:
                elements.append(ctx.make("text", page_text, loc, label, page=page_no, heading_path=heading_path, title=title))

            # 2. Tables (CSV + structured rows)
            for t_index, tab in enumerate(tables, start=1):
                matrix = tab.extract()
                csv_text = matrix_to_csv(matrix)
                if not csv_text:
                    continue
                elements.append(
                    ctx.make(
                        "table",
                        csv_text,
                        loc,
                        f"p.{page_no}, table {t_index}",
                        page=page_no,
                        heading_path=heading_path,
                        title=title,
                        structured={"rows": matrix},
                        extra={"surrounding_text": get_surrounding_text(text_blocks, tab.bbox)[:1000]},
                    )
                )

            # 3. Embedded images
            for img_info in images:
                xref = img_info[0]
                width, height = img_info[2], img_info[3]
                if min(width, height) < ctx.settings.min_image_px:
                    continue  # icons, bullets, decorations
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                if not ctx.take_image_slot():
                    continue
                try:
                    raw = doc.extract_image(xref)["image"]
                except Exception:
                    continue
                context = get_surrounding_text(text_blocks, rects[0])
                pending.append(
                    (
                        (raw, context, "image"),
                        dict(location=loc, label=label, page=page_no, heading_path=heading_path, title=title, context_text=context),
                    )
                )

            # 4. Vector charts (drawings large enough to be figures, outside tables)
            seen = []
            for drawing in page.get_drawings():
                rect = fitz.Rect(drawing["rect"]) & page.rect
                if rect.width > 150 and rect.height > 100:
                    if any(rect.intersects(t) for t in table_rects) or any(rect.intersects(s) for s in seen):
                        continue
                    seen.append(rect)
                    if not ctx.take_image_slot():
                        continue
                    png = page.get_pixmap(clip=rect, dpi=150).tobytes("png")
                    context = get_surrounding_text(text_blocks, rect)
                    pending.append(
                        (
                            (png, context, "image"),
                            dict(
                                type="chart",
                                location=loc,
                                label=label,
                                page=page_no,
                                heading_path=heading_path,
                                title=title,
                                context_text=context,
                            ),
                        )
                    )
    finally:
        doc.close()

    results = ctx.describe_images([job for job, _ in pending])
    for (_, kwargs), (description, error) in zip(pending, results):
        elements.append(image_element(ctx, description, error, **kwargs))

    if scanned_skipped:
        ctx.warnings.append(
            f"{scanned_skipped} scanned page(s) not processed (limit {ctx.settings.max_images_per_doc} per document)"
        )
    # Keep reading order: sort by page, then by creation order
    elements.sort(key=lambda e: (e.page or 0, e.element_id))
    return elements
