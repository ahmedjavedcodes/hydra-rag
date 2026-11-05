"""Ingestion entry points: files or a URL in, citable elements out (stored per session).

    from src.pipeline.ingest import ingest_files, ingest_url
    result = ingest_files(["report.pdf"])            # new session
    ingest_url("example.com/article", result.session_id)
"""

import hashlib
import os
import shutil
from typing import List, Optional, Union

from src.config import IngestionSettings, settings as default_settings
from src.loaders import UNSUPPORTED_HINTS, LoadContext, LoaderError, get_loader, supported_extensions
from src.loaders import web_loader
from src.loaders.vision import VisionDescriber, default_vision
from src.pipeline.store import FAILED, PROCESSING, READY, SessionStore
from src.schema import BatchResult, FileResult
from utilities.ingestion import category_for, store_file, unique_destination
from utilities.scrapper import sanitize_and_generate_dynamic_folder
from utilities.session import new_session_id, validate_session_id


class StorageError(Exception):
    """Writing to disk failed; the rest of the batch is aborted."""


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:32]


def _unsupported(source: str, ext: str) -> FileResult:
    hint = UNSUPPORTED_HINTS.get(ext)
    reason = hint or f"supported types: {', '.join(supported_extensions())}"
    return FileResult(source=source, status="unsupported", reason=reason)


def _too_large(settings: IngestionSettings) -> str:
    return f"file too large (limit {settings.max_file_mb} MB)"


def _process_stored(
    store: SessionStore,
    stored_path: str,
    display_name: str,
    doc_id: str,
    vision: VisionDescriber,
    settings: IngestionSettings,
    source_url: Optional[str] = None,
) -> FileResult:
    """Runs the loader on an already stored, already reserved document and records the outcome."""
    record = store.get(doc_id)
    result = FileResult(
        source=source_url or display_name, status="ok", doc_id=doc_id, version_id=record["version_id"], stored_path=stored_path
    )
    ext = os.path.splitext(display_name)[1].lower() or os.path.splitext(stored_path)[1].lower()
    store.update(doc_id, state=PROCESSING, stored_path=stored_path)
    ctx = LoadContext(
        path=stored_path,
        filename=display_name,
        session_id=store.session_id,
        doc_id=doc_id,
        version_id=record["version_id"],
        vision=vision,
        settings=settings,
        source_url=source_url,
    )
    try:
        elements = get_loader(ext)(ctx)
        ctx.finish_warnings()
        if not any(e.content_text.strip() for e in elements):
            if any(e.metadata.get("description_failed") for e in elements):
                raise LoaderError("image description failed (vision model unavailable), please try again later")
            raise LoaderError("no readable content")
    except Exception as e:
        reason = str(e) if isinstance(e, LoaderError) else f"could not read file ({type(e).__name__})"
        if not isinstance(e, LoaderError):
            print(f"   ✗ Unexpected loader error for {display_name}: {e!r}")
        store.update(doc_id, state=FAILED, reason=reason, warnings=ctx.warnings)
        try:
            os.remove(stored_path)
        except OSError:
            pass
        result.status, result.reason, result.stored_path, result.warnings = "failed", reason, None, ctx.warnings
        return result

    store.write_elements(doc_id, elements)
    failed_images = sum(1 for e in elements if e.metadata.get("description_failed"))
    if failed_images:
        ctx.warnings.append(f"{failed_images} image description(s) failed")
    store.update(doc_id, state=READY, element_count=len(elements), warnings=ctx.warnings)
    result.element_count = len(elements)
    result.warnings = ctx.warnings
    return result


def _ingest_local_file(
    store: SessionStore, path: str, vision: VisionDescriber, settings: IngestionSettings
) -> FileResult:
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    if not os.path.isfile(path):
        return FileResult(source=filename, status="failed", reason="file not found")
    if get_loader(ext) is None:
        return _unsupported(filename, ext)
    size = os.path.getsize(path)
    if size > settings.max_file_bytes:
        return FileResult(source=filename, status="failed", reason=_too_large(settings))
    if size == 0:
        return FileResult(source=filename, status="failed", reason="no readable content")

    doc_id = file_hash(path)
    existing = store.reserve(doc_id, filename.lower(), {"filename": filename, "source_url": None})
    if existing:
        return _duplicate(filename, existing)
    try:
        stored_path = store_file(path, store.root)
    except OSError as e:
        store.update(doc_id, state=FAILED, reason="could not store file")
        raise StorageError(f"could not store file ({e.strerror or type(e).__name__})")
    return _process_stored(store, stored_path, filename, doc_id, vision, settings)


def _duplicate(source: str, existing: dict) -> FileResult:
    warnings = [] if existing["state"] == READY else [f"already {existing['state']}"]
    return FileResult(
        source=source,
        status="ok",
        doc_id=existing["doc_id"],
        version_id=existing["version_id"],
        element_count=existing.get("element_count", 0),
        duplicate=True,
        stored_path=existing.get("stored_path"),
        warnings=warnings,
    )


def ingest_files(
    inputs: Union[str, List[str]],
    session_id: Optional[str] = None,
    vision: Optional[VisionDescriber] = None,
    settings: IngestionSettings = default_settings,
) -> BatchResult:
    """Ingests local files into a session. Omit `session_id` to start a new session.

    Returns one FileResult per input; a bad file never stops the others. Raises ValueError
    for an invalid session id (before anything is written).
    """
    if isinstance(inputs, str):
        inputs = [inputs]
    session_id = validate_session_id(session_id) if session_id else new_session_id()
    batch = BatchResult(session_id=session_id)

    if len(inputs) > settings.max_files_per_upload:
        batch.error = f"too many files: {len(inputs)} (limit {settings.max_files_per_upload} per upload)"
        batch.files = [
            FileResult(source=os.path.basename(str(p)), status="failed", reason=batch.error) for p in inputs
        ]
        return batch

    store = SessionStore(session_id)
    vision = vision or default_vision()
    for index, path in enumerate(inputs):
        path = str(path).strip()
        try:
            result = _ingest_local_file(store, path, vision, settings)
        except StorageError as e:
            batch.error = str(e)
            batch.files.append(FileResult(source=os.path.basename(path), status="failed", reason=str(e)))
            for rest in inputs[index + 1 :]:
                batch.files.append(
                    FileResult(source=os.path.basename(str(rest)), status="failed", reason="not processed: " + str(e))
                )
            break
        batch.files.append(result)
        print(_summary(result))
    return batch


def _summary(result: FileResult) -> str:
    if result.status == "ok":
        extra = " (duplicate)" if result.duplicate else ""
        return f" ✓ {result.source}: {result.element_count} element(s){extra}"
    return f" ✗ {result.source}: {result.status} - {result.reason}"


# ---------------------------------------------------------------- URLs


def _store_bytes(store: SessionStore, root_dir: str, filename: str, body: bytes) -> str:
    destination_dir = os.path.join(root_dir, category_for(filename))
    os.makedirs(destination_dir, exist_ok=True)
    path = unique_destination(destination_dir, filename)
    try:
        with open(path, "wb") as f:
            f.write(body)
    except OSError as e:
        os.remove(path)
        raise StorageError(f"could not store file ({e.strerror or type(e).__name__})")
    return path


def _ingest_downloaded(
    store: SessionStore,
    root_dir: str,
    fetch: "web_loader.FetchResult",
    vision: VisionDescriber,
    settings: IngestionSettings,
) -> FileResult:
    """Stores and loads a fetched resource (a page or a linked file) as its own document."""
    filename = web_loader.filename_from_response(fetch)
    ext = web_loader.extension_for(fetch, filename)
    if not ext or get_loader(ext) is None:
        return _unsupported(fetch.url, ext or "")
    if not filename.lower().endswith(ext):
        filename = f"{os.path.splitext(filename)[0] or 'download'}{ext}"
    if not fetch.body:
        return FileResult(source=fetch.url, status="failed", reason="no readable content")

    doc_id = hashlib.sha256(fetch.body).hexdigest()[:32]
    existing = store.reserve(doc_id, fetch.url.lower(), {"filename": filename, "source_url": fetch.url})
    if existing:
        return _duplicate(fetch.url, existing)
    try:
        stored_path = _store_bytes(store, root_dir, filename, fetch.body)
    except StorageError:
        store.update(doc_id, state=FAILED, reason="could not store file")
        raise
    return _process_stored(store, stored_path, filename, doc_id, vision, settings, source_url=fetch.url)


def ingest_url(
    url: str,
    session_id: Optional[str] = None,
    vision: Optional[VisionDescriber] = None,
    settings: IngestionSettings = default_settings,
) -> BatchResult:
    """Scrapes a URL into the session: the page (or a direct file link) first, then linked documents
    and content images, each as its own document. Files go to ./data/<session_id>/web/<site>/."""
    session_id = validate_session_id(session_id) if session_id else new_session_id()
    batch = BatchResult(session_id=session_id)
    store = SessionStore(session_id)
    vision = vision or default_vision()

    clean_url, site_dir = sanitize_and_generate_dynamic_folder(url, session_id)
    source = clean_url or str(url)
    if not clean_url:
        batch.files.append(FileResult(source=source, status="failed", reason="invalid URL"))
        return batch

    created_dir = not os.path.exists(site_dir)
    try:
        fetch = web_loader.safe_get(clean_url, settings.url_timeout_s, settings.max_file_bytes)
        is_html = "html" in fetch.content_type or (
            not fetch.content_type and fetch.body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))
        )
        if is_html:
            html = fetch.body.decode("utf-8", errors="replace")
            if web_loader.readable_text_length(html) < web_loader.MIN_PAGE_CHARS:
                rendered = web_loader.render_with_playwright(fetch.url, settings.url_timeout_s)
                if rendered and web_loader.readable_text_length(rendered) >= web_loader.MIN_PAGE_CHARS:
                    html = rendered
            if web_loader.readable_text_length(html) < web_loader.MIN_PAGE_CHARS:
                raise LoaderError("page not accessible or has no readable content")
            page = web_loader.FetchResult(fetch.url, "text/html", "", html.encode("utf-8"))
            page_result = _ingest_page(store, site_dir, page, vision, settings)
        else:
            html = None
            page_result = _ingest_downloaded(store, site_dir, fetch, vision, settings)
    except LoaderError as e:
        batch.files.append(FileResult(source=source, status="failed", reason=str(e)))
        _remove_if_empty(site_dir, created_dir)
        return batch
    except StorageError as e:
        batch.error = str(e)
        batch.files.append(FileResult(source=source, status="failed", reason=str(e)))
        return batch

    batch.files.append(page_result)
    print(_summary(page_result))
    if page_result.status != "ok":
        _remove_if_empty(site_dir, created_dir)
        return batch
    if html:
        try:
            batch.files.extend(_ingest_linked_assets(store, site_dir, html, fetch.url, vision, settings, page_result))
        except StorageError as e:
            batch.error = str(e)
    return batch


def _ingest_page(store, site_dir, page, vision, settings) -> FileResult:
    page.content_disposition = 'attachment; filename="page.html"'
    return _ingest_downloaded(store, site_dir, page, vision, settings)


def _ingest_linked_assets(store, site_dir, html, base_url, vision, settings, page_result) -> List[FileResult]:
    """Linked documents and large content images. Every fetch attempt counts towards `max_linked_files`
    (so slow or broken links can't stall the request); failed ones are listed, the rest are summarised
    as a warning on the page result."""
    from io import BytesIO

    from PIL import Image

    docs, images = web_loader.discover_assets(html, base_url)
    results: List[FileResult] = []
    candidates = [(d, False) for d in docs] + [(i, True) for i in images]
    limit = settings.max_linked_files
    if len(candidates) > limit:
        page_result.warnings.append(f"{len(candidates) - limit} linked file(s) not fetched (limit {limit} per page)")
    for asset_url, is_image in candidates[:limit]:
        try:
            fetch = web_loader.safe_get(asset_url, settings.url_timeout_s, settings.max_file_bytes)
        except LoaderError as e:
            results.append(FileResult(source=asset_url, status="failed", reason=str(e)))
            continue
        if "text/html" in fetch.content_type:
            continue  # a link that leads to another page, not a file
        if is_image:
            try:
                with Image.open(BytesIO(fetch.body)) as img:
                    width, height = img.size
            except Exception:
                continue
            if width < web_loader.MIN_PAGE_IMAGE_W or height < web_loader.MIN_PAGE_IMAGE_H:
                continue  # decorations, not content
        result = _ingest_downloaded(store, site_dir, fetch, vision, settings)
        results.append(result)
        print("   " + _summary(result))
    return results


def _remove_if_empty(site_dir: str, created_dir: bool) -> None:
    """Leaves no folder behind for a page that failed."""
    if created_dir and os.path.isdir(site_dir):
        has_files = any(files for _, _, files in os.walk(site_dir))
        if not has_files:
            shutil.rmtree(site_dir, ignore_errors=True)
