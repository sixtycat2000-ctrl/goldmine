"""URL-root ingestion (SPEC §2): crawl() pages land as kind=webpage rows with
path=<canonical URL>, source='url'. The page commit flows through the shared
text-commit helper (commit.CommitMixin._commit_chunks), so pages get the same
ctx guard, batched embedding and verdict tail as file chunks — pages commit
with loc=None (with_loc=False): crawled text has no line numbers."""

from __future__ import annotations

from ..backends.base import EmbeddingError
from ..store import FileRec, _folder_words
from . import walk
from .extraction import _Piece, _has_words

URL_EXTRACTOR_VERSION = "url-stdlib.v1"


class UrlIngestMixin:
    """URL-roots branch of the run (moved out of pipeline.py). Host supplies
    store/cfg/backend/lexical_only, the chunk + commit helpers and
    _record_unsupported."""

    def _record_url_unimplemented(self, url: str, report) -> None:
        self.store.upsert_file(
            FileRec(
                path=url, source="url", kind="webpage", size=0, mtime=None,
                sha256=None, state="done", capability="metadata_only",
                detail="crawler not implemented in this build",
                chunker_version=self._chunker_version,
            )
        )
        report.scanned += 1
        self._tally(report, "webpage", "metadata_only")

    def _ingest_urls(self, urls: list[str], bounds, report) -> None:
        """Driven by ingest/crawl.py (agent B, build pass 5): crawl() yields
        CrawledPage | SkipNotice; each page is stored as kind=webpage rows with
        path=<canonical URL>, source='url' (SPEC §2)."""
        from .crawl import CrawlBounds, crawl  # agent B's module

        bounds = bounds or CrawlBounds(politeness=self.cfg.ingest.politeness)
        bounds = bounds or CrawlBounds(politeness=self.cfg.ingest.politeness)
        for item in crawl(urls, bounds):
            if isinstance(item, walk.SkipNotice):
                self._record_unsupported(
                    self.store, item.path, item.reason, report,
                    kind_override="webpage", source="url",
                )
                continue
            from .extract_html import html_to_text

            plain, title = html_to_text(item.html)
            fid = self.store.upsert_file(
                FileRec(
                    path=item.url, source="url", kind="webpage",
                    size=len(item.html.encode("utf-8")), mtime=None,
                    sha256=item.sha256, state="pending",
                )
            )
            if not plain.strip():
                self._finish_file(
                    fid, capability="metadata_only", detail="no text extracted",
                    info={"fetched_at": item.fetched_at, "final_url": item.final_url},
                    extractor_version=URL_EXTRACTOR_VERSION,
                    report=report, kind="webpage",
                )
                continue
            # token-budget chunking (D3) + the same paranoid ctx guard as the
            # file path: an over-ceiling page chunk is split, never failed.
            # Pages carry no line numbers: pieces commit with loc=None.
            pieces = [
                _Piece(c, _has_words(c.text))
                for c in self._chunk_text(plain, "webpage",
                                          self.cfg.ingest.max_chunks)
            ]
            try:
                self._commit_chunks(
                    fid,
                    name=item.url,
                    fts_words=_folder_words(item.url, "url"),
                    pieces=pieces,
                    title=title or "page",
                    embed=self.backend if not self.lexical_only else None,
                    batch=max(1, int(self.cfg.llama.batch)),
                    report=report, kind="webpage",
                    detail=None,
                    info={"fetched_at": item.fetched_at, "final_url": item.final_url},
                    extractor_version=URL_EXTRACTOR_VERSION,
                    with_loc=False,
                )
            except EmbeddingError as exc:
                # P1-5: the re-crawled page failed — its previous extract
                # must not stay searchable.
                self.store.delete_content_segments(fid)
                self._tally(report, "webpage", "failed")
                report.failures.append(
                    {"path": item.url, "detail": f"embedding failed: {exc}"}
                )
                report.failed += 1
                continue
