import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import List, Optional

ELEMENT_TYPES = ("text", "table", "image", "chart")


@dataclass
class Element:
    """One citable unit of content extracted from a document (the output of every loader)."""

    doc_id: str
    version_id: str
    element_id: str
    type: str
    page: Optional[int]
    heading_path: List[str]
    title: str
    content_text: str
    content_structured: dict
    metadata: dict
    content_hash: str

    def to_dict(self) -> dict:
        return asdict(self)


def hash_content(text: str, structured: Optional[dict] = None) -> str:
    """Stable hash of an element's content, used to skip re-embedding unchanged content."""
    payload = text + json.dumps(structured or {}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_element(element: Element) -> List[str]:
    """Returns a list of schema problems (empty when the element is valid)."""
    problems = []
    if element.type not in ELEMENT_TYPES:
        problems.append(f"unknown type {element.type!r}")
    for name in ("doc_id", "version_id", "element_id", "content_hash"):
        if not getattr(element, name):
            problems.append(f"missing {name}")
    meta = element.metadata
    if not meta.get("session_id"):
        problems.append("metadata.session_id missing")
    if not (meta.get("filename") or meta.get("source_url")):
        problems.append("metadata needs filename or source_url")
    if not meta.get("location"):
        problems.append("metadata.location missing")
    if not isinstance(element.heading_path, list):
        problems.append("heading_path must be a list")
    return problems


@dataclass
class FileResult:
    """Outcome of ingesting one file or URL."""

    source: str
    status: str  # "ok", "failed" or "unsupported"
    reason: Optional[str] = None
    doc_id: Optional[str] = None
    version_id: Optional[str] = None
    element_count: int = 0
    duplicate: bool = False
    stored_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchResult:
    """Outcome of one ingestion request. `error` is set when the whole request was rejected or aborted."""

    session_id: str
    files: List[FileResult] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
