import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.loaders.base import LoadContext, LoaderError, register, rows_to_markdown
from src.loaders.text_loader import read_text_file
from src.schema import Element

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_REDIRECTS = 5
# Pages with less readable text than this are treated as JavaScript shells (static fetch) or inaccessible.
MIN_PAGE_CHARS = 100

CLUTTER_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript", "svg"]
NAV_NOISE = re.compile(
    r"^(continue reading|read more|click here|learn more|view more|go to.*|back to top)$", re.IGNORECASE
)
LINKED_DOC = re.compile(r"\.(pdf|xlsx|xls|csv|docx|pptx|txt|md)(\?|#|$)", re.IGNORECASE)
LINK_DENY = ("login", "logout", "javascript:", "mailto:", "facebook", "twitter")
IMAGE_NOISE = re.compile(
    r"(logo|avatar|favicon|loader|spinner|banner-ad|arrow|button|sprite|share-on|social-media|icon)", re.IGNORECASE
)
CONTENT_IMAGE = re.compile(r"\.(png|jpe?g|webp)$", re.IGNORECASE)
MIN_PAGE_IMAGE_W, MIN_PAGE_IMAGE_H = 100, 80


# ---------------------------------------------------------------- URL safety


def ip_allowed(ip: ipaddress._BaseAddress) -> bool:
    """Only public internet addresses; blocks loopback, private, link-local (cloud metadata), reserved."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url_allowed(url: str) -> None:
    """Raises LoaderError unless the URL is http(s) and every address its host resolves to is public.

    Note: the host is resolved again when the request is made, so a DNS server that changes its
    answer in between (DNS rebinding) is not fully prevented. Redirects are re-checked hop by hop.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LoaderError("invalid URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise LoaderError("page unreachable (host not found)")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip_allowed(ip):
            raise LoaderError("address not allowed")


@dataclass
class FetchResult:
    url: str
    content_type: str
    content_disposition: str
    body: bytes


def safe_get(url: str, timeout_s: int, max_bytes: int) -> FetchResult:
    """GET with SSRF checks on every redirect hop, a timeout and a size cap."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url_allowed(url)
        try:
            response = requests.get(url, headers=HEADERS, stream=True, timeout=timeout_s, allow_redirects=False)
        except requests.Timeout:
            raise LoaderError("page unreachable (timed out)")
        except requests.RequestException:
            raise LoaderError("page unreachable")
        with response:
            if response.is_redirect or response.is_permanent_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code >= 400:
                raise LoaderError(f"page returned HTTP {response.status_code}")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise LoaderError(f"file too large (limit {max_bytes // (1024 * 1024)} MB)")
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise LoaderError(f"file too large (limit {max_bytes // (1024 * 1024)} MB)")
            except requests.RequestException:
                raise LoaderError("page unreachable (connection interrupted)")
            return FetchResult(
                url=url,
                content_type=response.headers.get("Content-Type", "").lower(),
                content_disposition=response.headers.get("Content-Disposition", ""),
                body=bytes(body),
            )
    raise LoaderError("page unreachable (too many redirects)")


def render_with_playwright(url: str, timeout_s: int) -> str:
    """Renders a JavaScript page with the local Chrome. Every request the page makes is SSRF-checked.

    Returns "" when Playwright or Chrome is unavailable or rendering fails.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""

    def guard(route):
        request_url = route.request.url
        if request_url.startswith(("http://", "https://")):
            try:
                check_url_allowed(request_url)
            except LoaderError:
                return route.abort()
        return route.continue_()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            try:
                page = browser.new_context(user_agent=USER_AGENT).new_page()
                page.route("**/*", guard)
                page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        print(f"   ✗ Playwright rendering failed: {e}")
        return ""


def filename_from_response(fetch: FetchResult) -> str:
    """Filename from Content-Disposition, else from the URL path."""
    match = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';]+)", fetch.content_disposition, re.IGNORECASE)
    name = unquote(match.group(1)) if match else unquote(os.path.basename(urlparse(fetch.url).path))
    return os.path.basename(name.strip()) or "download"


# ---------------------------------------------------------------- HTML parsing


def clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(CLUTTER_TAGS):
        element.decompose()
    for noisy in soup.find_all(["a", "button", "span", "p"]):
        if NAV_NOISE.match(noisy.get_text(strip=True)):
            noisy.decompose()
    return soup


def readable_text_length(html: str) -> int:
    soup = clean_soup(html)
    return sum(len(t.get_text(" ", strip=True)) for t in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"]))


def html_table_rows(table) -> List[List[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [" ".join(td.stripped_strings) for td in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    return rows


@register(".html", ".htm")
def load_html(ctx: LoadContext) -> List[Element]:
    """Page text split into sections by heading, plus one element per table."""
    soup = clean_soup(read_text_file(ctx.path))
    page_title = soup.title.get_text(strip=True) if soup.title else ""
    elements: List[Element] = []
    heading_path: List[str] = []
    buf: List[str] = []
    section_no = 0
    table_no = 0
    max_chars = ctx.settings.max_text_element_chars

    def label() -> str:
        return f'section "{heading_path[-1]}"' if heading_path else "page"

    def flush():
        nonlocal buf, section_no
        if buf:
            section_no += 1
            elements.append(
                ctx.make(
                    "text",
                    "\n\n".join(buf),
                    {"section": section_no},
                    label(),
                    heading_path=heading_path,
                    title=heading_path[-1] if heading_path else page_title,
                )
            )
        buf = []

    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "pre", "blockquote"]):
        if element.find_parent(["table", "ul", "ol", "pre", "blockquote"]):
            continue
        if element.name.startswith("h") and element.name[1:].isdigit():
            text = element.get_text(" ", strip=True)
            if text:
                flush()
                level = int(element.name[1])
                heading_path = heading_path[: level - 1] + [text]
                buf.append(text)
            continue
        if element.name == "table":
            rows = html_table_rows(element)
            if rows:
                flush()
                table_no += 1
                elements.append(
                    ctx.make(
                        "table",
                        rows_to_markdown(rows[0], rows[1:]),
                        {"table_index": table_no},
                        f"table {table_no}" + (f' in "{heading_path[-1]}"' if heading_path else ""),
                        heading_path=heading_path,
                        title=heading_path[-1] if heading_path else page_title,
                        structured={"header": rows[0], "rows": rows[1:]},
                    )
                )
            continue
        if element.name in ("ul", "ol"):
            items = [f"- {li.get_text(' ', strip=True)}" for li in element.find_all("li") if li.get_text(strip=True)]
            text = "\n".join(items)
        else:
            text = element.get_text(" ", strip=True)
        if not text:
            continue
        if buf and sum(len(t) for t in buf) + len(text) > max_chars:
            flush()
        buf.append(text)
    flush()
    return elements


def discover_assets(html: str, base_url: str) -> Tuple[List[str], List[str]]:
    """Linked documents and content images on a page, as absolute URLs (deduplicated, in page order)."""
    soup = BeautifulSoup(html, "html.parser")
    docs, images = [], []
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if LINKED_DOC.search(href) and not any(term in href.lower() for term in LINK_DENY):
            url = urljoin(base_url, href)
            if url not in docs:
                docs.append(url)
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        name = os.path.basename(urlparse(src).path)
        if src.startswith("data:") or IMAGE_NOISE.search(name) or not CONTENT_IMAGE.search(name):
            continue
        url = urljoin(base_url, src)
        if url not in images:
            images.append(url)
    return docs, images


def extension_for(fetch: FetchResult, filename: str) -> Optional[str]:
    """Extension from the filename, or guessed from the Content-Type."""
    ext = os.path.splitext(filename)[1].lower()
    if ext:
        return ext
    content_type = fetch.content_type
    guesses = [
        ("pdf", ".pdf"),
        ("presentationml", ".pptx"),
        ("wordprocessingml", ".docx"),
        ("spreadsheetml", ".xlsx"),
        ("ms-excel", ".xls"),
        ("csv", ".csv"),
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/webp", ".webp"),
        ("text/plain", ".txt"),
        ("text/markdown", ".md"),
        ("text/html", ".html"),
    ]
    for needle, guessed in guesses:
        if needle in content_type:
            return guessed
    return None
