"""BFS web crawler over the Fetcher (extract_html): bounded, polite, honest.

SPEC §2 URL flags are enforced HERE (the Fetcher handles per-request concerns:
robots cache, politeness wait, byte cap, content-type check, scoped redirects,
429/5xx backoff); this module owns the frontier:
  - BFS from the seed URLs, depth <= max_depth (0 = the seed pages only)
  - same-host only (host of each seed); off-host links are skipped with reason
  - robots.txt via the Fetcher's per-origin cache; a robots crawl-delay raises
    the effective politeness for that host when larger than the configured one
  - max_pages fetches and max_mb_total bytes, enforced BEFORE side effects
  - canonicalize + seen-set dedupe (fragment/trailing-slash collapse upstream)
  - every skipped URL becomes a SkipNotice with a machine-stable reason so the
    ingest report can count it (V5e)
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterator

from .extract_html import Fetcher, canonicalize
from .walk import SkipNotice

logger = logging.getLogger("goldmine.crawl")


@dataclass
class CrawlBounds:
    """SPEC §2 crawler flags (MODULE_MAP defaults)."""

    max_pages: int = 50
    max_depth: int = 2          # 0 = the URL itself only
    max_mb_page: float = 5.0
    max_mb_total: float = 200.0
    politeness: float = 1.0
    timeout_s: float = 30.0
    respect_robots: bool = True


@dataclass
class CrawledPage:
    """One fetched HTML page, ready for the pipeline's webpage row."""

    url: str                    # canonical request URL
    final_url: str              # canonical post-redirect URL
    status: int
    html: str
    sha256: str
    fetched_at: float
    detail: str | None = None


class _LinkExtractor(HTMLParser):
    """Collect href targets; relative links are resolved by the caller."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


def extract_links(html: str) -> list[str]:
    parser = _LinkExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML: keep the links parsed so far
        pass
    return parser.links


def _host_of(canonical_url: str) -> str:
    return (urllib.parse.urlsplit(canonical_url).hostname or "").lower()


def crawl(start_urls: list[str], bounds: CrawlBounds, excluded=()) -> Iterator[CrawledPage | SkipNotice]:
    """BFS fetch. Yields CrawledPage for fetched HTML and SkipNotice for every
    URL that was seen but not ingested (with the reason). Never raises for
    ordinary crawl conditions — hard failures come back as SkipNotices too."""
    fetcher = Fetcher(
        timeout_s=bounds.timeout_s,
        max_mb_page=bounds.max_mb_page,
        politeness=bounds.politeness,
        respect_robots=bounds.respect_robots,
    )
    allowed_hosts: set[str] = set()
    frontier: list[tuple[str, int]] = []
    seen: set[str] = set()
    for seed in start_urls:
        canonical = canonicalize(seed)
        if canonical in seen:
            continue
        seen.add(canonical)
        allowed_hosts.add(_host_of(canonical))
        frontier.append((canonical, 0))

    pages_fetched = 0
    bytes_total = 0

    def budget_left() -> bool:
        return pages_fetched < bounds.max_pages and bytes_total < int(
            bounds.max_mb_total * 1024 * 1024
        )

    head = 0
    while head < len(frontier):
        url, depth = frontier[head]
        head += 1
        from ..index_state import contains

        if any(contains(root, url) for root in excluded):
            continue
        host = _host_of(url)
        if host not in allowed_hosts:
            yield SkipNotice(url, "off-host link")
            continue
        if not budget_left():
            # one honest notice for the queue we are abandoning (never a flood
            # of per-URL unsupported rows)
            remaining = len(frontier) - head + 1
            yield SkipNotice(
                url,
                f"crawl budget exhausted ({remaining} URL(s) not fetched)",
            )
            break

        # robots crawl-delay raises the per-host politeness when larger
        fetcher.politeness = bounds.politeness
        if bounds.respect_robots:
            parser = fetcher.robots_for(url)
            if parser is not None:
                crawl_delay = parser.crawl_delay(fetcher.user_agent)
                if crawl_delay:
                    fetcher.politeness = max(bounds.politeness, float(crawl_delay))

        result = fetcher.fetch(url)
        pages_fetched += 1
        bytes_total += result.bytes_read

        if result.detail and result.detail.startswith("robots.txt disallow"):
            # covers both the direct disallow and a redirect that landed on a
            # disallowed path (P2-6)
            yield SkipNotice(url, result.detail)
            continue
        if not result.ok:
            yield SkipNotice(url, result.detail or f"HTTP {result.status}")
            continue

        yield CrawledPage(
            url=result.url,
            final_url=result.final_url or result.url,
            status=result.status,
            html=result.html,
            sha256=result.sha256,
            fetched_at=result.fetched_at,
        )

        if depth >= bounds.max_depth:
            continue
        for href in extract_links(result.html):
            absolute = urllib.parse.urljoin(result.final_url or result.url, href)
            if not absolute.lower().startswith(("http://", "https://")):
                continue  # mailto:, javascript:, anchors
            child = canonicalize(absolute)
            if child in seen:
                continue
            seen.add(child)
            if _host_of(child) not in allowed_hosts:
                yield SkipNotice(child, "off-host link")
                continue
            frontier.append((child, depth + 1))
