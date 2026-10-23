"""Pinned real-model compatibility; quality A/B requires the product dev set."""
from __future__ import annotations
import os

import pytest

pytestmark = [pytest.mark.live, pytest.mark.assets]


def test_pinned_rank_head_respects_document_caps_and_releases_process():
    if os.environ.get("GOLDMINE_LIVE") != "1":
        pytest.skip("set GOLDMINE_LIVE=1 for the explicitly installed reranker")
    from goldmine.config import load
    from goldmine.deadline import request_timeout
    from goldmine.rerank import from_config

    backend = from_config(load())
    try:
        with request_timeout(10):
            scores, metadata = backend.rank("中国的首都是哪里？", [
                "北京是中国的首都。" + "中文背景资料。" * 200,
                "酸面包需要定期喂养酵母。",
            ])
        assert scores[0] > scores[1] and scores[0] > .5
        assert max(metadata["document_tokens"]) <= 384
        assert metadata["documents_truncated"] == [True, False]
        proc = backend._proc
    finally:
        backend.close()
    assert proc.poll() is not None and backend._proc is None
