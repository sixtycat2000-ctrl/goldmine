"""HTML -> text (stdlib HTMLParser) plus the URL fetch primitives crawl.py builds on.

The extraction half ports DigUp's plainText(fromHTML:): drop script/style/comment/
head, block ends become line breaks, strip remaining tags, decode entities. The
fetch half is agent-B crawl.py's toolkit: canonicalization + dedupe, a scoped
redirect handler, robots.txt via urllib.robotparser, politeness delay, per-page
byte cap, content-type check, and 1s->2s->4s backoff on 429/5xx (D5e / V5e).
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .extract_text import Extracted, decode_bytes

logger = logging.getLogger("goldmine.extract_html")

# ---------------------------------------------------------------- extraction


_BLOCK_END_TAGS = {
    "p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "blockquote", "pre",
}
_DROP_TAGS = {"script", "style", "head", "template", "noscript"}


class _TextExtractor(HTMLParser):
    """convert_charrefs=True decodes named + numeric entities (superset of DigUp's
    hand list, including the numeric-entity pass)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":  # <title> lives inside <head>: capture even while skipping
            self._in_title = True
        elif tag == "br" and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in _BLOCK_END_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        elif self._skip_depth == 0:
            self._parts.append(data)

    def handle_comment(self, data):  # comments dropped
        pass

    def text(self) -> str:
        import re

        raw = "".join(self._parts)
        raw = re.sub(r"[ \t\u00A0]+", " ", raw)
        raw = re.sub(r"\s*\n\s*", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def charset_declared(head: bytes) -> str | None:
    """`<meta charset=...>` / `<meta http-equiv=content-type ... charset=...>`."""
    import re

    try:
        text = head.decode("ascii", errors="ignore")
    except Exception:  # pragma: no cover - decode with errors=ignore never raises
        return None
    match = re.search(
        r"""<meta[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9._\-]+)""", text, re.IGNORECASE
    )
    return match.group(1) if match else None


def html_to_text(html: str, base_url: str | None = None) -> tuple[str, str | None]:
    """Strip to plain text; returns (text, title). `base_url` reserved for link
    extraction in the crawler (agent B); text extraction does not need it."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # malformed HTML: keep what we got
        logger.debug("html_to_text: parser error (%s) — using partial text", exc)
    title = (parser.title or "").strip() or None
    return parser.text(), title


def extract_html_file(path: Path, encoding_override: str | None = None) -> Extracted:
    """Read an .html/.htm file: declared charset -> decode ladder -> strip."""
    with open(path, "rb") as f:
        data = f.read()
    declared = charset_declared(data[:2048])
    text, detail = decode_bytes(data, declared_charset=declared, override=encoding_override)
    if declared and detail is None and declared.lower() not in ("utf-8", "utf8", "ascii"):
        detail = f"declared-charset: {declared}"
    plain, title = html_to_text(text)
    return Extracted(text=plain, title=title, detail=detail, tool_version="html-stdlib.v1")


# ---------------------------------------------------------------- fetch primitives


def canonicalize(url: str) -> str:
    """Lowercase scheme/host, strip fragment, resolve ../, drop default port.

    Trailing slashes and fragments collapse so the crawler dedupes
    `https://x.dev/a/`, `https://x.dev/a#top` and `https://x.dev/a` to one URL.
    """
    parts = urllib.parse.urlsplit(url.strip())
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    default_port = {"http": 80, "https": 443}.get(scheme)
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = parts.path or "/"
    if path not in ("", "/"):
        # resolve . and .. segments, then collapse the trailing slash so
        # "/a/" and "/a" dedupe to one URL (the crawl frontier keys on this)
        segments: list[str] = []
        for seg in path.split("/"):
            if seg == ".":
                continue
            if seg == "..":
                if segments and segments[-1] not in ("", ".."):
                    segments.pop()
                continue
            if seg != "" or segments == [""]:
                segments.append(seg)
        while len(segments) > 1 and segments[-1] == "":
            segments.pop()
        path = "/".join(segments)
        if not path.startswith("/"):
            path = "/" + path
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


@dataclass
class FetchResult:
    url: str                    # canonical request URL
    final_url: str | None       # after redirects (canonical)
    status: int                 # HTTP status; 0 = blocked/failed before a response
    html: str                   # decoded body ('' unless 200 text/html)
    sha256: str
    fetched_at: float
    bytes_read: int = 0
    content_type: str | None = None
    detail: str | None = None   # skip/failure reason for the ingest report

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.html != ""


class _ScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow <= max_redirects redirects that stay on the first request's host
    AND port (a different port is a different origin — P3-16)."""

    def __init__(self, first_host: str, first_port: int | None, max_redirects: int = 5):
        self.first_host = first_host
        self.first_port = first_port
        self.max_redirects = max_redirects
        self.count = 0
        self.off_host = False

    @staticmethod
    def _effective_port(parts: urllib.parse.ParseResult) -> int | None:
        try:
            port = parts.port
        except ValueError:
            return None
        if port is None:
            port = {"http": 80, "https": 443}.get(parts.scheme)
        return port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > self.max_redirects:
            raise urllib.error.HTTPError(newurl, 400, "too many redirects", headers, fp)
        parts = urllib.parse.urlsplit(newurl)
        host = (parts.hostname or "").lower()
        if host != self.first_host:
            self.off_host = True
            raise urllib.error.HTTPError(newurl, 400, "off-host redirect", headers, fp)
        if self._effective_port(parts) != self.first_port:
            self.off_host = True
            raise urllib.error.HTTPError(
                newurl, 400, "redirect left origin (port)", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class Fetcher:
    """Stateful fetcher: politeness per host + robots cache. crawl.py (agent B)
    drives this over its URL frontier and enforces max_pages/max_depth/total-bytes.

    bounds: timeout_s, max_mb_page, politeness, respect_robots (CrawlBounds fields
    with the same names; the dataclass itself lives in ingest/crawl.py).
    """

    timeout_s: float = 30.0
    max_mb_page: float = 5.0
    politeness: float = 1.0
    respect_robots: bool = True
    user_agent: str = "goldmine/0.1 (local-first indexer)"
    _last_hit: dict[str, float] = field(default_factory=dict, repr=False)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict, repr=False
    )

    # -- robots ------------------------------------------------------------

    def _origin(self, canonical_url: str) -> str:
        parts = urllib.parse.urlsplit(canonical_url)
        return f"{parts.scheme}://{parts.netloc}"

    def robots_for(self, canonical_url: str) -> urllib.robotparser.RobotFileParser | None:
        """Per-origin robots parser, or None when robots is absent/unreachable.

        Robots-unreachable behavior (RFC 9309 default-allow, kept from v1): a
        4xx/5xx/network failure on /robots.txt yields None -> the URL is
        treated as ALLOWED. The fetch is bounded by `timeout_s` (P2-7: the old
        parser.read() used urllib's unbounded default and could hang a crawl).
        """
        origin = self._origin(canonical_url)
        if origin in self._robots:
            return self._robots[origin]
        parser: urllib.robotparser.RobotFileParser | None = None
        if self.respect_robots:
            try:
                request = urllib.request.Request(
                    f"{origin}/robots.txt", headers={"User-Agent": self.user_agent}
                )
                with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(
                        resp.read().decode("utf-8", "replace").splitlines()
                    )
            except (urllib.error.URLError, OSError, ValueError):
                # Unreachable robots: default-allow (4xx semantics, RFC 9309).
                parser = None
        self._robots[origin] = parser
        return parser

    def allowed(self, canonical_url: str) -> bool:
        parser = self.robots_for(canonical_url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, canonical_url)

    # -- fetching ----------------------------------------------------------

    def _polite_wait(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None and self.politeness > 0:
            remaining = self.politeness - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_hit[host] = time.monotonic()

    def fetch(self, url: str, encoding_hint: str | None = None) -> FetchResult:
        """One polite, robots-checked, byte-capped, backoff-retried GET.

        Non-HTML content types, robots disallows, off-host/too-many redirects and
        oversized pages return FetchResult with status 0 (or the HTTP status) and
        a `detail` — never raise for ordinary crawl conditions.
        """
        import hashlib

        canonical = canonicalize(url)
        now = time.time()

        if self.respect_robots and not self.allowed(canonical):
            return FetchResult(canonical, None, 0, "", "", now, detail="robots.txt disallow")

        origin = urllib.parse.urlsplit(canonical)
        host = (origin.hostname or "").lower()
        origin_port = _ScopedRedirectHandler._effective_port(origin)
        max_bytes = int(self.max_mb_page * 1024 * 1024)
        backoffs = [1.0, 2.0, 4.0]
        detail: str | None = None
        status = 0

        for attempt in range(len(backoffs) + 1):
            self._polite_wait(host)
            # a fresh handler per attempt: redirect count must not accumulate
            # across 429/5xx backoff retries
            redirector = _ScopedRedirectHandler(host, origin_port)
            opener = urllib.request.build_opener(redirector)
            request = urllib.request.Request(
                canonical, headers={"User-Agent": self.user_agent}
            )
            try:
                with opener.open(request, timeout=self.timeout_s) as resp:
                    status = int(resp.status)
                    content_type = resp.headers.get_content_type()
                    final_url = canonicalize(resp.geturl())
                    if self.respect_robots and not self.allowed(final_url):
                        # P2-6: robots.txt gates the FINAL URL too — a redirect
                        # that lands on a disallowed path is refused exactly
                        # like a direct request, before the body is read.
                        return FetchResult(
                            canonical, final_url, 0, "", "", now,
                            content_type=content_type,
                            detail="robots.txt disallow (redirect target)",
                        )
                    body = resp.read(max_bytes + 1)
            except urllib.error.HTTPError as exc:
                # the scoped redirect handler signals policy violations by
                # raising HTTPError(400, "off-host redirect"/"too many
                # redirects"/"redirect left origin (port)") — surface those as
                # their real reasons, not 400
                reason_msg = str(getattr(exc, "msg", "") or "")
                if "off-host redirect" in reason_msg:
                    return FetchResult(
                        canonical, None, 0, "", "", now, detail="off-host redirect"
                    )
                if "redirect left origin (port)" in reason_msg:
                    return FetchResult(
                        canonical, None, 0, "", "", now,
                        detail="redirect left origin (port)",
                    )
                if "too many redirects" in reason_msg:
                    return FetchResult(
                        canonical, None, 0, "", "", now, detail="too many redirects"
                    )
                status, detail = exc.code, f"HTTP {exc.code}"
                if exc.code in (429, 500, 502, 503, 504) and attempt < len(backoffs):
                    time.sleep(backoffs[attempt])  # exponential backoff 1->2->4 s
                    continue
                return FetchResult(canonical, None, status, "", "", now, detail=detail)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                reason = getattr(exc, "reason", exc)
                if "off-host redirect" in str(reason):
                    return FetchResult(
                        canonical, None, 0, "", "", now, detail="off-host redirect"
                    )
                if "redirect left origin (port)" in str(reason):
                    return FetchResult(
                        canonical, None, 0, "", "", now,
                        detail="redirect left origin (port)",
                    )
                if "too many redirects" in str(reason):
                    return FetchResult(
                        canonical, None, 0, "", "", now, detail="too many redirects"
                    )
                return FetchResult(canonical, None, 0, "", "", now, detail=f"fetch failed: {reason}")

            if redirector.off_host:
                return FetchResult(canonical, None, 0, "", "", now, detail="off-host redirect")

            if len(body) > max_bytes:
                return FetchResult(
                    canonical, final_url, status, "", "", now, bytes_read=len(body),
                    content_type=content_type,
                    detail=f"page exceeds {self.max_mb_page:g} MB cap",
                )
            if content_type != "text/html":
                return FetchResult(
                    canonical, final_url, status, "", "", now, bytes_read=len(body),
                    content_type=content_type,
                    detail=f"non-HTML content-type: {content_type}",
                )

            declared = encoding_hint or charset_declared(body[:2048])
            html, _lossy = decode_bytes(body, declared_charset=declared)
            return FetchResult(
                url=canonical,
                final_url=final_url,
                status=status,
                html=html,
                sha256=hashlib.sha256(body).hexdigest(),
                fetched_at=now,
                bytes_read=len(body),
                content_type=content_type,
            )
        return FetchResult(canonical, None, status, "", "", now, detail=detail)
