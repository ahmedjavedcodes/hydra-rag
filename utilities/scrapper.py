"""URL helpers for the scraper. Fetching and parsing live in `src/loaders/web_loader.py`
(SSRF-checked requests, Playwright rendering, HTML to elements); `run_scrapper` runs the full
URL ingestion from `src/pipeline/ingest.py`."""

import os
import re
from urllib.parse import parse_qsl, urlparse

from utilities.session import get_session_dir


def fix_url_schema(url: str) -> str:
    """Fixes common URL typos such as missing slashes ('https:/domain.com' -> 'https://domain.com')."""
    if not url or not isinstance(url, str):
        return ""

    cleaned_url = url.strip().replace("\n", "").replace("\r", "")

    # Fix single slash after protocol scheme
    cleaned_url = re.sub(r"^(https?):/(?!/)", r"\1://", cleaned_url)

    # Prepend https:// if no scheme is provided
    if not cleaned_url.startswith(("http://", "https://")):
        cleaned_url = "https://" + cleaned_url

    return cleaned_url


def sanitize_and_generate_dynamic_folder(raw_url: str, session_id: str) -> tuple:
    """Cleans trailing whitespaces, fixes broken URL slashes, and generates safe folder paths."""
    sanitized_url = fix_url_schema(raw_url)
    if not sanitized_url:
        return None, None

    parsed = urlparse(sanitized_url)
    netloc = parsed.netloc.lower()

    if not netloc:
        netloc = "unknown_domain"

    query_dict = dict(parse_qsl(parsed.query, keep_blank_values=False))
    # Ports and other characters (":") are not allowed in Windows folder names
    domain_clean = re.sub(r"[^a-zA-Z0-9_.\-]", "_", netloc.replace("www.", ""))
    id_marker = (
        query_dict.get("id") or query_dict.get("oga_service") or "index"
    )

    id_marker_clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(id_marker))[
        :30
    ].strip("_")
    final_folder_name = f"{domain_clean}_{id_marker_clean}"

    unique_target_path = os.path.normpath(
        os.path.join(get_session_dir(session_id), "web", final_folder_name)
    )

    return sanitized_url, unique_target_path


def get_target_subfolder(filename: str, content_type: str = "") -> str:
    """Maps extracted files to target subfolders based on extension or Content-Type."""
    ext = os.path.splitext(filename)[1].lower()

    if ext in [".csv", ".xlsx", ".xls"] or any(
        t in content_type for t in ["csv", "excel", "spreadsheet"]
    ):
        return "spreadsheets"
    elif ext in [".pdf", ".docx", ".doc", ".pptx", ".txt", ".rtf"] or any(
        t in content_type for t in ["pdf", "word", "presentation", "text/plain"]
    ):
        return "documents"
    elif ext in [".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp"] or "image" in content_type:
        return "images"
    return "others"


def run_scrapper(url: str, session_id: str) -> str:
    """Pipeline execution wrapper.

    Takes a URL and the caller's session id, ingests the page and its linked files
    into ./data/<session_id>/web/<site>/, and returns that path ("" on failure).
    """
    from src.pipeline.ingest import ingest_url

    print(f"🚀 Initializing scraper for: {url}")
    batch = ingest_url(url, session_id)
    page = batch.files[0] if batch.files else None
    if not page or page.status != "ok":
        print(f" ✗ Error: {page.reason if page else batch.error}")
        return ""

    _, target_path = sanitize_and_generate_dynamic_folder(url, session_id)
    print(f" Task finished! Data directory: {os.path.abspath(target_path)}")
    return target_path
