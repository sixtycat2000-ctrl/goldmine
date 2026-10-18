"""goldmine.ingest — pipeline + leaves.

Per MODULE_MAP, the pipeline classes live at the package root
(`goldmine.ingest.IngestPipeline`); the implementation is in `pipeline.py`.
`crawl.py` and `extract_pdf.py` are build passes 5 (agent B); `media.py` is a
stretch module. Everything else in this package ships in this build.

`walk` is NOT re-exported here: the submodule `goldmine.ingest.walk` must stay
reachable as a module (`from goldmine.ingest import walk` used to yield the
walk() FUNCTION via this re-export, shadowing the module — 02 F12). The
function is `walk_files` on the module itself.
"""

from .pipeline import IngestPipeline, IngestReport
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
