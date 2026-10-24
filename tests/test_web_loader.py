"""URL ingestion tests (spec 01: AC7, AC11 and the URL edge cases). Uses a local HTTP server."""

import http.server
import os
import threading

import pytest

from src.loaders import web_loader
from src.pipeline.ingest import ingest_url
from src.pipeline.store import SessionStore
from tests.conftest import png_bytes

ARTICLE = """<!doctype html><html><head><title>Field notes</title></head><body>
<nav>Home | About</nav>
<h1>Monsoon report</h1>
<p>The 2026 monsoon brought above-average rainfall to Punjab, with Lahore recording 310 mm in July alone.</p>
<h2>Impact</h2>
<p>Farmers reported higher rice yields, while several low-lying neighbourhoods saw short-term flooding.</p>
<table><tr><th>City</th><th>Rain (mm)</th></tr><tr><td>Lahore</td><td>310</td></tr></table>
<a href="/files/rain.csv">Download the data</a>
<a href="/login.pdf">Login</a>
<img src="/img/photo.png"><img src="/img/logo.png">
</body></html>"""

ROUTES = {
    "/article": ("text/html; charset=utf-8", ARTICLE.encode()),
    "/files/rain.csv": ("text/csv", b"city,rain\nLahore,310\nMultan,120\n"),
    "/img/photo.png": ("image/png", png_bytes(400, 300)),
    "/img/logo.png": ("image/png", png_bytes(400, 300)),
    "/shell": ("text/html", b"<html><body><div id='root'></div></body></html>"),
    "/report.pdf": None,  # filled in by the fixture
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()
            return
        route = ROUTES.get(self.path)
        if not route:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = route
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(samples):
    with open(samples["pdf"], "rb") as f:
        ROUTES["/report.pdf"] = ("application/pdf", f.read())
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def allow_local(monkeypatch):
    """The test server is on loopback, which the SSRF guard blocks by default."""
    monkeypatch.setattr(web_loader, "ip_allowed", lambda ip: True)
    monkeypatch.setattr(web_loader, "render_with_playwright", lambda url, timeout_s: "")


def test_ac7_article_page_and_linked_files(server, allow_local, vision):
    batch = ingest_url(f"{server}/article", vision=vision)
    page = batch.files[0]
    assert page.status == "ok", page.reason
    assert page.source == f"{server}/article"

    store = SessionStore(batch.session_id)
    elements = store.read_elements(page.doc_id)
    texts = [e for e in elements if e["type"] == "text"]
    assert all(e["metadata"]["source_url"] == f"{server}/article" for e in elements)
    assert any("310 mm" in e["content_text"] for e in texts)
    assert any(e["heading_path"] == ["Monsoon report", "Impact"] for e in texts)
    assert not any("Home | About" in e["content_text"] for e in texts)  # navigation stripped
    assert any(e["type"] == "table" and "Lahore" in e["content_text"] for e in elements)

    linked = {f.source: f for f in batch.files[1:]}
    assert linked[f"{server}/files/rain.csv"].status == "ok"
    assert linked[f"{server}/img/photo.png"].status == "ok"
    assert f"{server}/img/logo.png" not in linked  # logo filtered as noise
    assert f"{server}/login.pdf" not in linked
    assert "web" in page.stored_path and batch.session_id in page.stored_path


def test_direct_file_url_uses_the_file_loader(server, allow_local, vision):
    batch = ingest_url(f"{server}/report.pdf", vision=vision)
    result = batch.files[0]
    assert result.status == "ok" and result.element_count > 0
    elements = SessionStore(batch.session_id).read_elements(result.doc_id)
    assert all(e["metadata"]["source_url"] == f"{server}/report.pdf" for e in elements)
    assert any(e["metadata"].get("page") == 2 for e in elements)


def test_same_url_twice_is_a_duplicate(server, allow_local, vision):
    first = ingest_url(f"{server}/article", vision=vision)
    again = ingest_url(f"{server}/article", first.session_id, vision=vision)
    assert again.files[0].duplicate and again.files[0].doc_id == first.files[0].doc_id


def test_ac11_loopback_is_blocked_by_default(server, vision, data_root):
    batch = ingest_url(f"{server}/article", vision=vision)
    assert batch.files[0].status == "failed" and batch.files[0].reason == "address not allowed"
    assert not os.path.exists(os.path.join(data_root, batch.session_id, "web"))


def test_redirect_to_private_address_is_blocked(server, monkeypatch, vision):
    allowed = lambda ip: str(ip) == "127.0.0.1"
    monkeypatch.setattr(web_loader, "ip_allowed", allowed)
    batch = ingest_url(f"{server}/to-metadata", vision=vision)
    assert batch.files[0].reason == "address not allowed"


def test_missing_page_leaves_no_folder(server, allow_local, vision, data_root):
    batch = ingest_url(f"{server}/nope", vision=vision)
    assert batch.files[0].status == "failed" and "404" in batch.files[0].reason
    web_dir = os.path.join(data_root, batch.session_id, "web")
    assert not os.path.exists(web_dir) or all(not files for _, _, files in os.walk(web_dir))


def test_javascript_shell_without_content_fails_clearly(server, allow_local, vision):
    batch = ingest_url(f"{server}/shell", vision=vision)
    assert batch.files[0].status == "failed" and "no readable content" in batch.files[0].reason


def test_malformed_url_is_repaired_or_rejected(vision):
    batch = ingest_url("https:/", vision=vision)
    assert batch.files[0].status == "failed" and batch.files[0].reason == "invalid URL"


def test_private_addresses_are_not_allowed():
    import ipaddress

    for address in ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "::1", "::ffff:10.0.0.1"]:
        assert not web_loader.ip_allowed(ipaddress.ip_address(address)), address
    assert web_loader.ip_allowed(ipaddress.ip_address("8.8.8.8"))
