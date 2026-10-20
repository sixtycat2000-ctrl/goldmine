"""SIGTERM must unwind request ownership and reap only its local model."""
from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from goldmine.store import FileRec, SegmentRec, Store


def test_quality_cli_sigterm_reaps_owned_reranker_process(tmp_path):
    db, ready = tmp_path / "index.sqlite", tmp_path / "owned-child.pid"
    store = Store(db)
    fid = store.upsert_file(FileRec(path="/explicit/neutral.txt", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts="needle service")])
    store.close()
    bootstrap = '''
from pathlib import Path
import subprocess,sys,time
from goldmine.rerank import LocalReranker
import goldmine.rerank as module
from goldmine.cli import main
class Waiting(LocalReranker):
    def rank(self, query, docs):
        self._proc = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        Path(sys.argv[2]).write_text(str(self._proc.pid))
        time.sleep(60)
module.from_config=lambda cfg:Waiting(None,Path('/missing/model'))
raise SystemExit(main(['search','needle','--db',sys.argv[1],'--backend','mock','--scorer','lexical',
                       '--mode','quality','--rerank','local']))
'''
    env = {**os.environ, "GOLDMINE_HOME": str(tmp_path / "home"),
           "GOLDMINE_CONFIG": str(tmp_path / "empty.toml")}
    parent = subprocess.Popen([sys.executable, "-c", bootstrap, str(db), str(ready)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = None
    try:
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until and parent.poll() is None:
            time.sleep(.01)
        assert ready.is_file(), parent.communicate(timeout=2)
        child = int(ready.read_text())
        parent.send_signal(signal.SIGTERM)
        stdout, stderr = parent.communicate(timeout=3)
        assert parent.returncode == 130, (stdout, stderr)
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            child = None
        assert child is None, "owned ranker survived its CLI"
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait(timeout=2)
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
