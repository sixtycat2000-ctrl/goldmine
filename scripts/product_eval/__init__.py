"""Portable product evaluation: audited inputs, grouped folds, actual evidence."""

from .bundle import ProductBundle, build_corpus, load_bundle
from .scoring import score_query, summarize

__all__ = ["ProductBundle", "build_corpus", "load_bundle", "score_query", "summarize"]
