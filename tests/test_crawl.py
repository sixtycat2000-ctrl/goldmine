"""Tier-2: crawler bounds against a local http.server — zero external network.

Fixture site (built per-test in tmp): index.html links page2.html, a
robots-disallowed path, an off-host URL and duplicate forms of page2 (fragment
+ trailing slash); /private/hidden.html is robots-disallowed; /429.html 429s
twice then succeeds; /big is 3 MB of non-HTML; robots.txt carries a crawl-delay.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.ingest import IngestPipeline
from goldmine.ingest.crawl import CrawlBounds, CrawledPage, crawl
from goldmine.ingest.extract_html import Fetcher
from goldmine.store import Store
from goldmine.ingest.walk import SkipNotice


class Site:
    """Tiny in-memory site with hit counters."""

    def __init__(self):
        self.hits: dict[str, int] = {}
        self.redirects: dict[str, str] = {}  # path -> Location for 302s
        self.pages = {
            "/": (
                "<html><head><title>Root Page</title></head><body>"
                "<p>root words about kubernetes pods</p>"
                '<a href="/page2.html">two</a>'
                '<a href="/page2.html#frag">dupe fragment</a>'
                '<a href="/page2.html/">trailing-slash dupe</a>'
                '<a href="/private/hidden.html">secret</a>'
                '<a href="http://external.example/off">off host</a>'
                '<a href="mailto:x@example.com">mail</a>'
                "</body></html>"
            ),
            "/page2.html": (
                "<html><body><p>page two words about sourdough starter</p>"
                '<a href="/">back</a>'
                '<a href="/page2.html#top">fragment dupe</a></body></html>'
            ),
            "/private/hidden.html": "<html><body>hidden content</body></html>",
            "/429.html": "<html><body>finally served</body></html>",
        }

    def robots(self) -> str:
        return "User-agent: *\nDisallow: /private/\nCrawl-delay: 0\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    site: "Site" = None  # set by make_server

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/html"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        site = self.site
        site.hits[path] = site.hits.get(path, 0) + 1
        if path == "/robots.txt":
            self._send(200, site.robots())
            return
        if path in site.redirects:
            self.send_response(302)
            self.send_header("Location", site.redirects[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/429.html":
            if site.hits[path] <= 2:
                self._send(429, "slow down")
            else:
                self._send(200, site.pages["/429.html"])
            return
        if path == "/big":
            self._send(200, "x" * (3 * 1024 * 1024), ctype="application/octet-stream")
            return
        page = site.pages.get(path)
        if page is None:
            self._send(404, "nope")
        else:
            self._send(200, page)


def make_server(site: Site):
    handler = type("BoundHandler", (_Handler,), {"site": site})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def site():
    server, base = make_server(Site())

    class Handle:
        pass

    handle = Handle()
    handle.base = base
    handle.server = server
    handle.hits = server.RequestHandlerClass.site.hits
    handle.redirects = server.RequestHandlerClass.site.redirects
    handle.url = lambda path: base + path
    yield handle
    server.shutdown()
    server.server_close()


def _fast(bounds_kwargs: dict | None = None) -> CrawlBounds:
    return CrawlBounds(politeness=0.0, timeout_s=10.0, **(bounds_kwargs or {}))


def _collect(gen):
    pages: list[CrawledPage] = []
    skips: list[SkipNotice] = []
    for item in gen:
        if isinstance(item, CrawledPage):
            pages.append(item)
        else:
            skips.append(item)
    return pages, skips


def test_bfs_same_host_and_dedupe(site):
    pages, skips = _collect(crawl([site.url("/")], _fast()))
    assert sorted(p.url for p in pages) == [
        site.url("/"), site.url("/page2.html"),
    ]
    reasons = {s.path: s.reason for s in skips}
    assert reasons.get(site.url("/private/hidden.html")) == "robots.txt disallow"
    assert reasons.get("http://external.example/off") == "off-host link"
    # off-host was never fetched: its host has no DNS here, and the fetcher
    # never saw it (the skip happened in the frontier)


def test_depth_zero_fetches_seed_only(site):
    pages, _skips = _collect(crawl([site.url("/")], _fast({"max_depth": 0})))
    assert [p.url for p in pages] == [site.url("/")]
    assert site.hits.get("/page2.html") is None  # link never fetched


def test_max_pages_budget(site):
    pages, skips = _collect(crawl([site.url("/")], _fast({"max_pages": 1})))
    assert len(pages) == 1
    assert any("budget exhausted" in s.reason for s in skips)


def test_per_page_byte_cap(site):
    _pages, skips = _collect(crawl([site.url("/big")], _fast({"max_mb_page": 1.0})))
    assert any("cap" in s.reason for s in skips)


def test_non_html_content_type_skipped(site):
    _pages, skips = _collect(crawl([site.url("/big")], _fast()))
    assert any("non-HTML" in s.reason for s in skips)


def test_429_backoff_then_success(site):
    started = time.monotonic()
    pages, _skips = _collect(crawl([site.url("/429.html")], _fast()))
    elapsed = time.monotonic() - started
    assert [p.url for p in pages] == [site.url("/429.html")]
    assert site.hits["/429.html"] == 3  # two 429s then success
    assert elapsed >= 3.0  # 1s + 2s exponential backoff actually waited


def test_robots_off_flag():
    server, base = make_server(Site())
    try:
        bounds = CrawlBounds(politeness=0.0, timeout_s=10.0, respect_robots=False)
        pages, _skips = _collect(
            crawl([base + "/private/hidden.html"], bounds)
        )
        assert [p.url for p in pages] == [base + "/private/hidden.html"]
    finally:
        server.shutdown()
        server.server_close()


def test_politeness_delay_between_same_host_fetches(site):
    bounds = CrawlBounds(politeness=0.4, timeout_s=10.0, max_depth=1)
    started = time.monotonic()
    pages, _skips = _collect(crawl([site.url("/")], bounds))
    elapsed = time.monotonic() - started
    assert len(pages) == 2
    assert elapsed >= 0.4  # the second same-host fetch waited


def test_off_host_redirect_reported():
    class RedirectHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "http://external.example/x")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        pages, skips = _collect(
            crawl([f"http://127.0.0.1:{server.server_address[1]}/redirect"], _fast())
        )
        assert pages == []
        assert any("off-host redirect" in s.reason for s in skips)
    finally:
        server.shutdown()
        server.server_close()


def _redirect_site(location: str):
    """A tiny origin serving a permissive robots.txt and 302ing everything
    else to `location`."""

    class Bouncer(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Bouncer)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_robots_disallow_on_redirect_target(site):
    """P2-6: robots.txt gates the FINAL URL too — a 302 that lands on a
    disallowed path (same origin) is refused and recorded; the disallowed body
    is never read into a page (urllib performs the redirected GET internally,
    which is why the check runs on resp.geturl() right after open)."""
    site.redirects["/bouncer"] = site.url("/private/hidden.html")
    pages, skips = _collect(crawl([site.url("/bouncer")], _fast()))
    assert pages == []
    reasons = [s.reason for s in skips]
    assert any(r.startswith("robots.txt disallow") for r in reasons), reasons
    # no crawled page carries the disallowed content
    assert all("/private/" not in p.url for p in pages)
    assert all("hidden content" not in p.html for p in pages)


def test_cross_port_redirect_skipped(site):
    """P3-16: a redirect to the same host on a DIFFERENT port is off-origin —
    skipped with the honest reason, target never fetched."""
    bserver = _redirect_site(site.url("/page2.html"))
    try:
        pages, skips = _collect(
            crawl([f"http://127.0.0.1:{bserver.server_address[1]}/bouncer"], _fast())
        )
        assert pages == []
        assert any(s.reason == "redirect left origin (port)" for s in skips)
        assert site.hits.get("/page2.html") is None
    finally:
        bserver.shutdown()
        bserver.server_close()


def test_robots_unreachable_returns_within_timeout():
    """P2-7: a robots endpoint that never answers is bounded by timeout_s
    (the old parser.read() used urllib's unbounded default and could hang a
    crawl forever); unreachable robots means default-allow (RFC 9309)."""
    import socket

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead.listen(1)  # TCP handshake completes in the backlog, nobody ever answers
    try:
        fetcher = Fetcher(timeout_s=0.5, politeness=0.0)
        started = time.monotonic()
        parser = fetcher.robots_for(f"http://127.0.0.1:{dead.getsockname()[1]}/")
        elapsed = time.monotonic() - started
        assert parser is None
        assert elapsed < 5.0
        # crawl proceeds: robots=None => allowed
        assert fetcher.allowed(f"http://127.0.0.1:{dead.getsockname()[1]}/page")
    finally:
        dead.close()


def _pipeline(tmp_path):
    cfg = Config()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "index.sqlite")
    return cfg, store


def test_pipeline_ingests_url_roots(site, tmp_path):
    cfg, store = _pipeline(tmp_path)
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([site.url("/")], crawl_bounds=_fast())
    assert report.by_kind.get("webpage", {}).get("content_indexed") == 2
    paths = {
        row[0] for row in store.db.execute("SELECT path FROM files WHERE source = 'url'")
    }
    # the robots-disallowed link is recorded too (unsupported, source=url);
    # so is the off-host link — P1-2: URL rows are never pruned by deletions
    assert paths == {
        site.url("/"), site.url("/page2.html"), site.url("/private/hidden.html"),
        "http://external.example/off",
    }
    hits = store.search_fts('"sourdough"')
    assert hits
    rec = store.find_by_path(site.url("/page2.html"))
    assert rec.capability == "content_indexed"
    assert rec.info.get("final_url") == site.url("/page2.html")
    assert rec.extractor_version == "url-stdlib.v1"
    store.close()


def test_pipeline_url_robots_disallow_recorded(site, tmp_path):
    cfg, store = _pipeline(tmp_path)
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([site.url("/private/hidden.html")], crawl_bounds=_fast())
    assert report.by_kind.get("webpage", {}).get("unsupported") == 1
    rec = store.find_by_path(site.url("/private/hidden.html"))
    assert rec.capability == "unsupported"
    assert rec.detail == "robots.txt disallow"
    store.close()
