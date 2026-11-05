from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from src.config import IngestionSettings, settings as default_settings
from src.loaders.vision import VisionDescriber
from src.schema import Element, hash_content


class LoaderError(Exception):
    """A file could not be loaded. The message is shown to the user, so keep it short and plain."""


# Formats we recognise but don't support, with a hint for the user.
UNSUPPORTED_HINTS = {
    ".ppt": "please save as .pptx",
    ".doc": "please save as .docx",
    ".rtf": "please save as .docx or .txt",
}


@dataclass
class LoadContext:
    """Everything a loader needs, plus helpers that build schema-complete elements."""

    path: str
    filename: str
    session_id: str
    doc_id: str
    version_id: str
    vision: VisionDescriber
    settings: IngestionSettings = default_settings
    source_url: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    images_used: int = 0
    images_skipped: int = 0
    _count: int = 0

    def make(
        self,
        type: str,
        content_text: str,
        location: Dict,
        label: str,
        page: Optional[int] = None,
        heading_path: Optional[List[str]] = None,
        title: str = "",
        structured: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> Element:
        """Builds an element with ids, hash and citation metadata filled in.

        `location` holds the flat citation fields (page / sheet + rows / slide / lines);
        `label` is the human-readable form used in source lists ("p.3", "slide 4").
        """
        self._count += 1
        metadata = {
            "session_id": self.session_id,
            "filename": self.filename,
            "location": label,
            **location,
        }
        if self.source_url:
            metadata["source_url"] = self.source_url
        if extra:
            metadata.update(extra)
        structured = structured or {}
        return Element(
            doc_id=self.doc_id,
            version_id=self.version_id,
            element_id=f"e{self._count:05d}",
            type=type,
            page=page,
            heading_path=list(heading_path or []),
            title=title,
            content_text=content_text,
            content_structured=structured,
            metadata=metadata,
            content_hash=hash_content(content_text, structured),
        )

    def take_image_slot(self) -> bool:
        """True while the per-document image cap allows another vision call."""
        if self.images_used < self.settings.max_images_per_doc:
            self.images_used += 1
            return True
        self.images_skipped += 1
        return False

    def describe_images(self, jobs: List[Tuple[bytes, str, str]]) -> List[Tuple[Optional[str], Optional[str]]]:
        """Describes images in parallel; returns (description, error) per job."""
        return self.vision.describe_many(jobs)

    def finish_warnings(self) -> None:
        if self.images_skipped:
            self.warnings.append(
                f"{self.images_skipped} image(s) not described (limit {self.settings.max_images_per_doc} per document)"
            )


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_ole_container(path: str) -> bool:
    """Office Open XML files (.docx/.pptx/.xlsx) are zip files; when they are wrapped in an
    OLE container instead, the file is password protected (encrypted)."""
    with open(path, "rb") as f:
        return f.read(8) == OLE_MAGIC


def cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return "" if text.lower() == "nan" else text.replace("|", "\\|")


def rows_to_markdown(header: List, rows: List[List]) -> str:
    """Renders a header and rows as a Markdown table (pipes in cells are escaped)."""
    width = 0
    for row in [header] + list(rows):
        filled = [i for i, c in enumerate(row) if cell_text(c)]
        width = max(width, filled[-1] + 1 if filled else 0)
    header, rows = list(header)[:width], [list(r)[:width] for r in rows]
    header = [cell_text(c) for c in header] + [""] * (width - len(header))
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    for row in rows:
        cells = [cell_text(c) for c in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


Loader = Callable[[LoadContext], List[Element]]
_REGISTRY: Dict[str, Loader] = {}


def register(*extensions: str):
    """Decorator: registers a loader for one or more extensions (".pdf")."""

    def wrap(fn: Loader) -> Loader:
        for ext in extensions:
            _REGISTRY[ext.lower()] = fn
        return fn

    return wrap


def get_loader(extension: str) -> Optional[Loader]:
    return _REGISTRY.get(extension.lower())


def supported_extensions() -> List[str]:
    return sorted(_REGISTRY)


def image_element(
    ctx: LoadContext,
    description: Optional[str],
    error: Optional[str],
    location: Dict,
    label: str,
    context_text: str = "",
    page: Optional[int] = None,
    heading_path: Optional[List[str]] = None,
    title: str = "",
    type: str = "image",
    extra: Optional[dict] = None,
) -> Element:
    """Builds an image element; a failed description is kept with `description_failed` so the document still finishes."""
    meta = dict(extra or {})
    if context_text:
        meta["surrounding_text"] = context_text[:1000]
    if error:
        meta["description_failed"] = True
        meta["description_error"] = error[:300]
    return ctx.make(
        type,
        description or "",
        location,
        label,
        page=page,
        heading_path=heading_path,
        title=title,
        extra=meta,
    )
