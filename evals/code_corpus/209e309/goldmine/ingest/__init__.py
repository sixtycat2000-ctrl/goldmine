"""goldmine.ingest — pipeline + leaves.

Per MODULE_MAP, the pipeline class lives at the package root
(`goldmine.ingest.IngestPipeline`); the run itself is in `pipeline.py`, built
from moved-out mixins: `extraction.py` (candidate -> _Outcome + OCR batch),
`commit.py` (outcomes -> store rows, incl. the IngestReport schema) and
`urls.py` (URL roots). `crawl.py` and `extract_pdf.py` are build passes 5
(agent B); `media.py` is a stretch module. Everything else in this package
ships in this build.

`walk` is NOT re-exported here: the submodule `goldmine.ingest.walk` must stay
reachable as a module (`from goldmine.ingest import walk` used to yield the
walk() FUNCTION via this re-export, shadowing the module — 02 F12). The
function is `walk_files` on the module itself.
"""

from .commit import IngestReport
from .pipeline import IngestPipeline
from .rules import Decision
from .walk import Candidate, SkipNotice, walk_files

__all__ = [
    "IngestPipeline",
    "IngestReport",
    "Decision",
    "Candidate",
    "SkipNotice",
    "walk_files",
]
