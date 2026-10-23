# goldmine — Module Map

> Implementation update (2026-10-22): see [the current feature status](FEATURE_STATUS.md) and the [P0–P2 execution record](execution-plan/PROGRESS.md). Older design and benchmark sections below retain their historical scope; they are not final candidate verification.

## Current boundaries (2026-10-22)

| Owner | Modules and responsibility |
|---|---|
| Entry and adapters | `cli.py` dispatch and exit mapping; `commands/{search,ingest,distill,inspection,lifecycle,maintenance,models,opener}.py`; `command_support.py` adapter runtime |
| Read application | `application.py`, `contracts.py`, `citations.py`, `evidence.py`; one request snapshot for rank/details/pack and stored-evidence reads |
| Retrieval | `search.py`, `lexical.py`, `lexical_store.py`, `retrieval_{lexical,evidence}.py`, `admission.py`; derived normalization, exact tiers and evidence admission |
| Source obligations | `query_obligations.py`, `anchor_resolution.py`, `anchor_admission.py`; typed complete anchors, actual stored-source witnesses, same-file lexical remainder and final clause coverage; `legacy_anchors.py` and `metadata_prelookup.py` provide bounded legacy recall and complete metadata eligibility |
| Optional quality | `query_plan.py`, `quality.py`, `rerank.py`; conservative clauses, bounded fusion and explicitly owned local model |
| Packs | `pack.py`, `pack_allocation.py`, `retrieval_evidence.py`; stored original text, source-order support, fair bounded allocation, complete-envelope budget and exact-material deduplication |
| Storage | `store.py`, `storage_connections.py`, `storage_writes.py`, `migrations.py`; read-only URI/snapshots, writer protocol and transactional schema work; `read_source.py` handles declared legacy/current text projections and query capabilities |
| Derived vector cache | `vector_cache.py`, `_vector_cache_files.py`; explicit immutable generation build, epoch validation and SQLite fallback |
| Backend lifecycle | `backends/`, `deadline.py`, `http_transport.py`; shared embedding interface, monotonic budget and bounded local HTTP |
| Ingest evidence | `ingest/{pipeline,extraction,commit,pdf_evidence,metrics}.py` plus format extractors; units, retries, atomic publication and actual-call instrumentation |
| MCP | `mcp_server.py`, `mcp_contracts.py`; lazy optional SDK and strict transport schemas, sharing Application behavior |
| Diagnostics | `doctor.py`, `evidence_status.py`; stored capability/staleness and honest tool/evidence states |
| Validation | `scripts/acceptance_lib/`, `answer_eval/`, `product_eval/`, `search_bench/`, `search_comparison/`, `distill_eval/`; strict raw evidence, separated data and optional/live scopes |

Base runtime remains standard library plus numpy. MCP dependencies
are optional and locked separately; ordinary help/fast/lexical paths do not import
the SDK or load the experimental reranker. Setuptools discovers `goldmine*`,
including the command and ingest subpackages. Current behavior and evidence are
tracked in [FEATURE_STATUS](FEATURE_STATUS.md) and [PROGRESS](execution-plan/PROGRESS.md).

## Historical v1 design map

The remaining sections preserve the original v1 module proposal and API sketches;
they are historical design inputs, not the present module count or dependency pin.
The proposal used a flat package plus two subpackages and twenty implementation
modules. Every proposed module lists its public surface and the
requirement it satisfies (R# = DESIGN_DECISIONS §#, V#.# = consultation verdict item).

Packaging (not a module): `pyproject.toml` — setuptools backend,
`dependencies = ["numpy>=1.26"]`, `optional-dependencies = {dev = ["pytest>=8"]}`, `scripts: goldmine = "goldmine.cli:main"`.

Legend: **[v1]** required for the first working release · [stretch] deliberately deferred but
scaffolded (stub raises/returns honest "not supported").

---

## Top-level modules

### `goldmine/__init__.py` **[v1]**
```python
__version__ = "0.1.0"
```

### `goldmine/cli.py` **[v1]**
Owns: argparse tree, exit-code mapping, human/JSON rendering, logging setup, `status` command.
```python
def main(argv: list[str] | None = None) -> int          # entry; never raises, always int
def build_parser() -> argparse.ArgumentParser
def cmd_ingest(args) -> int; def cmd_search(args) -> int
def cmd_distill(args) -> int; def cmd_status(args) -> int
def cmd_doctor(args) -> int; def cmd_backends(args) -> int
def cmd_reset(args) -> int
def render_hits(hits, json_mode: bool) -> str           # SPEC §3 formats
def render_ingest_summary(report, json_mode: bool) -> str
```
Satisfies: SPEC (whole contract), V5a (status surfaces capability counts).

### `goldmine/config.py` **[v1]**
Owns: defaults dataclasses, config.toml, env resolution (flag > env > file > default).
```python
@dataclass class LlamaCfg:    server: Path; model: Path; ctx: int = 2048; batch: int = 16
                              startup_timeout: float = 120.0; request_timeout: float = 60.0
@dataclass class IngestCfg:   max_file_mb=200; max_pages=5000; max_chunks=5000
                              audio_max_minutes=120; workers=2; politeness=1.0
@dataclass class SearchCfg:   limit=20; z_gate=2.0; scorer="hybrid"
@dataclass class DistillCfg:  threshold=0.70; min_members=3; max_chunks=50000
@dataclass class Config:      home: Path; backend: str; llama: LlamaCfg
                              ingest: IngestCfg; search: SearchCfg; distill: DistillCfg
def load(config_path: Path | None = None, env: Mapping[str, str] = os.environ) -> Config
def resolve_home(cfg: Config) -> Path                   # expanduser, mkdir -p
def default_config_path() -> Path                       # ~/.config/goldmine/config.toml
```
Satisfies: SPEC §10–11, V1 (stdlib tomllib).

### `goldmine/util.py` **[v1]**
Owns: hashing, subprocess runner, fp16 packing, text normalization, magic sniffing, excerpt.
```python
def sha256_file(path: Path, chunk: int = 1 << 20) -> str
def sha256_bytes(data: bytes) -> str
def run_tool(argv: list[str], timeout_s: float, max_output: int = 1 << 20,
             cwd: Path | None = None) -> ToolResult      # ToolResult(rc, stdout, stderr, timed_out)
                                                        # kills on timeout; bounded capture
def pack_fp16(vec: "np.ndarray") -> bytes               # 768×float16 little-endian
def unpack_fp16(blob: bytes) -> "np.ndarray"            # float32 out
def normalize_text(text: str) -> tuple[str, bool]       # NFC; (normalized, changed)
def excerpt(text: str, limit: int = 240) -> str         # DigUp excerpt(): one line, …
def sniff_image(head: bytes) -> str | None              # jpeg/png/gif/webp/heic(ftyp) magic
def is_mpeg_ts(head: bytes) -> bool                     # 0x47 @188/192 (DigUp Rules.swift)
def human_size(n: int) -> str
```
Satisfies: V5c (bounded subprocess), D7 (fp16 BLOB contract), D6 (magic sniff replaces UTType).

### `goldmine/fingerprints.py` **[v1]**
Owns: fingerprint identity + the refusal gate.
```python
def build(backend: EmbeddingBackend, preprocessing: str = "pipeline.v1") -> dict   # full JSON
def fingerprint_id(fp: dict) -> str                      # sha256(canonical json)[:16]
def canonical(fp: dict) -> bytes                         # sorted keys, no whitespace
def check(stored_id: str, stored_json: str, live: dict) -> FingerprintReport
                            # .ok, .diff_fields: list[str], .message, .remediation
class FingerprintMismatch(RuntimeError): ...             # cli maps to exit 3
```
Satisfies: V2 CRITICAL paragraph, D2 refinement (build ≠ fingerprint).

### `goldmine/chunker.py` **[v1]**
Owns: text → located chunks; token guard. DigUp `DocExtractor.chunks` semantics + paragraph
preference (V3 chunking fix).
```python
@dataclass class Chunk: text: str; loc: int | None; loc_end: int | None
def chunk_text(text: str, size: int = 1800, overlap: int = 200,
               max_chunks: int = 5000) -> list[Chunk]
    # clean whitespace (DigUp regex), prefer breaking at paragraph boundary (\n\n) inside the
    # window, else last whitespace ≥ size/2 in; next chunk starts on a word boundary;
    # loc/loc_end = line numbers when source had lines
def chunk_code(text: str, stretch: int = 1800) -> list[Chunk]
    # whole-line stretches (DigUp codeStretch=1800 won its eval); a longer line is its own
def est_tokens(text: str) -> int                        # len//4; warn-only guard vs ~512 limit
```

### `goldmine/store.py` **[v1]**
Owns: SQLite schema/migrations (ARCHITECTURE §6 DDL), all SQL; fp16 blobs; single writer.
```python
class Store:
    SCHEMA_VERSION = 1
    def __init__(self, path: Path)                      # create/migrate, WAL, busy_timeout 60s
    def meta(self, key: str) -> str | None
    def set_meta(self, key: str, value: str | None) -> None
    def upsert_file(self, rec: FileRec) -> int          # pending row + name segment/fts row
    def mark_file(self, fid: int, *, state: str, capability: str | None, detail: str | None,
                  info: dict | None, extractor_version: str | None) -> None
    def replace_segments(self, fid: int,
                         segs: list[SegmentRec]) -> None  # ONE transaction: del old + ins new
                                                        # (V5d atomic segment replacement)
    def fts_text(self, fid: int, kind: str, loc: float | None, text: str, vec_ok: bool) -> None
                                                        # fts(name, body, original) row pattern
    def delete_file(self, fid: int) -> None
    def prune_under(self, roots: list[str]) -> int       # remove files outside roots; count
    def find_by_path(self, path: str) -> FileRec | None
    def unchanged(self, path: str, size: int, sha: str) -> bool
    def pending(self) -> list[FileRec]
    def load_vectors(self) -> VectorTable                # ids, file_ids, modalities, (N,768) f32
    def search_fts(self, match: str, limit: int = 1000) -> list[FtsRow]
        # SELECT … ORDER BY bm25(fts,3.0,1.0) LIMIT ?   ← ascending: lower=better, best first
    def coverage(self, patterns: list[str], file_ids: list[int]) -> dict[int, float]
    def counts(self) -> StatusCounts                     # kind×capability grid, vectors, bytes
    def cards(self) -> list[CardRow]
    def save_cards(self, cards: list[Card]) -> None
    def close(self) -> None
@dataclass class FileRec: path; source; kind; size; mtime; sha256; state; capability;
                          detail; info; extractor_version
@dataclass class SegmentRec: kind; modality; loc; loc_end; excerpt; text_for_fts; original_fts; vec
```
Satisfies: V5a/d storage columns, D6 (schema), V3 (bm25 direction lives here, documented).

### `goldmine/search.py` **[v1]**
Owns: scoring, fusion, baselines. No DB writes, no backend lifecycle.
```python
STOPWORDS: frozenset[str]; KIND_WORDS: dict[str, str]     # DigUp lists, English subset
@dataclass class Hit: rank; score; z; cosine; coverage; kind; path; loc; loc_end;
                     segment; excerpt; file_status; scorer
class Searcher:
    def __init__(self, store: Store, backend: EmbeddingBackend | None)
    def search(self, query: str, *, limit: int = 20, scorer: str = "hybrid",
               z_gate: float = 2.0, kinds: set[str] | None = None,
               lexical_only: bool = False) -> list[Hit]
    # internals:
    @staticmethod def terms(text: str) -> list[str]
    @staticmethod def fts_query(terms: list[str]) -> str | None   # quoted OR, prefix last
    @staticmethod def kind_hints(text: str) -> set[str]
    @staticmethod def is_lookup(terms: list[str]) -> bool
    @staticmethod def zscores(scores: "np.ndarray", modalities: "np.ndarray") -> "np.ndarray"
        # per-modality centering; groups < 20 → global mean; std floor 0.02
    @staticmethod def rrf(rank_a: list[int], rank_b: list[int], k: int = 60) -> dict[int, float]
```
Satisfies: V3 all six fixes; SPEC §3 (prompts verbatim, lookup inversion, file-level collapse).

### `goldmine/distill.py` **[v2 — R6, design of record docs/v2.1-r6-distill.md]**
Owns: dedupe, τ calibration, guarded clustering, two-level split, cards, incremental runs.
Pure functions over store data; constants are module-level names (not config).
```python
@dataclass class Scope: kinds | under | after          # canonical_json() feeds view/run ids
@dataclass class TauResult: tau, tau_source, tau_raw, histogram, quartiles, fallback_reason
@dataclass class Card: title, title_kind, terms, excerpts, members, n_files, parent,
                      status (active|header|dissolved), centroid, chunk_weights, topic_id
def measure_tau(matrix, *, manual=None) -> TauResult   # D1/D2: stride-sampled pairwise
                                                       # gram, 256-bin histogram (F6
                                                       # clamp), Otsu + valley/mode-
                                                       # proximity fallback → P90, clamp
                                                       # [0.55, 0.85]; REAL corpora run
                                                       # 'percentile' (F2a, expected)
def cluster_v2(items, tau, min_members) -> (kept, hist)# leader + average-link admission
                                                       # guard (D4): first cluster where
                                                       # cos-to-centroid >= tau AND
                                                       # (S_c·x̂)/n_c >= max(tau-0.10, 0.35);
                                                       # guard reads member sums, never a
                                                       # centroid shortcut (F7)
def split_two_level(clusters, tau) -> (leaves, headers)# D5: n >= 12, s̄ < τ−0.12, recursive
                                                       # leader at τ+0.08, accept when
                                                       # weighted children s̄ tightens ≥0.05;
                                                       # internal nodes = header rows
def run_distill(store, *, scope, threshold=None, min_members,
                max_chunks, refresh, preview) -> dict  # full OR incremental (§4.3/F8:
                                                       # dissolve → snapshot → assign first
                                                       # qualifying card → leader remainder);
                                                       # caller persists via store.save_run
def eligibility(prev_run, ...) -> (ok, reason)         # D8/F4: view+algo+τ+embed_id+
                                                       # ≤20% vanished files, never corpus_hash
def corpus_hash_for(store, scope) -> str               # D9: embed_id INSIDE; audit-only
def render_markdown / render_json / render_preview     # §6.3 json = goldmine.cards/v2
def view_id_for(scope) / run_id_for(...)               # D7: sha256(algo+scope)[:16]; τ NOT in view
```
Satisfies: V4 (algorithm pinned in docstrings; refuses beyond max_chunks).

### `goldmine/doctor.py` **[v1]**
```python
@dataclass class CheckResult: name: str; status: str        # ok|warn|fail
                              detail: str; fix_hint: str | None
def run_checks(cfg: Config, db: Path | None, probe: bool = False) -> list[CheckResult]
def summary(results: list[CheckResult]) -> dict
CHECKS: list[Callable[[Config, ...], CheckResult]]   # registry: python, numpy, llama,
    # mock, pdftotext, textutil, ffmpeg, whisper[stretch], index, config, disk
```
Satisfies: V5f, SPEC §6.

---

## `goldmine/backends/`

### `backends/__init__.py` **[v1]**
```python
BACKENDS: dict[str, type[EmbeddingBackend]]          # {"llama":…, "mock":…}
def create_backend(cfg: Config, name: str | None = None) -> EmbeddingBackend
def probe_all(cfg: Config, live: bool = False) -> dict[str, dict]   # for backends/doctor
```

### `backends/base.py` **[v1]**
```python
@dataclass(frozen=True) class Capability: text: bool; image: bool; audio: bool
class NotSupported(RuntimeError): ...
class EmbeddingError(RuntimeError): ...              # carries tool log tail
class EmbeddingBackend(ABC):
    backend_id: str
    @property def dim(self) -> int                   # 768
    def capability(self) -> Capability
    def fingerprint_fields(self) -> dict             # model id/sha (or helper version), pooling…
    def embed_texts(self, texts: list[str]) -> list["np.ndarray"]        # L2-normalized f32
    def embed_image(self, path: Path) -> "np.ndarray"                    # raises NotSupported
    def embed_audio_wav(self, wav: bytes) -> "np.ndarray"                # raises NotSupported
    def describe(self) -> dict                       # availability, version, fix hints
    def close(self) -> None                          # kill only OUR children
    def __enter__ / __exit__
def query_prompt(text: str) -> str    # "task: search result | query: {text}"  (V2/D7 verbatim)
def document_prompt(title: str | None, text: str) -> str
                                      # "title: {none} | text: {text}"
```

### `backends/mock.py` **[v1]** — offline-first (TEST_PLAN tier 1–2 enabler)
```python
class MockBackend(EmbeddingBackend):
    backend_id = "mock"
    DIM = 768
    def __init__(self, seed: int = 0)                # deterministic bag-of-token hashing
        # token → blake2b(token, seed) → 2 dims; NOT python hash() (salted!)  (D1/mock note)
    def embed_texts(...); def embed_image(path)      # content-bytes hash → same space
    def embed_audio_wav(wav: bytes)                  # content-bytes hash → same space
    def fingerprint_fields(self) -> {"model_id": "mock-v1", "model_sha256": "n/a", ...}
```

### `backends/llama.py` **[v1]** — the only process-spawning module
```python
class LlamaServerBackend(EmbeddingBackend):
    backend_id = "llama"; capability = Capability(text=True, image=False, audio=False)
    def __init__(self, cfg: LlamaCfg)
    def start(self) -> None       # spawn → /health probe (§7.2 lifecycle); idempotent
    def embed_texts(self, texts)  # batches of cfg.batch via POST /v1/embeddings (urllib);
                                  # socket deadlines; retries only on stale-server (restart once)
    @staticmethod def probe_version(binary: Path) -> str | None     # --version, 3s timeout
    def model_sha256(self) -> str                # cached sidecar <model>.sha256; recompute on change
    def stderr_tail(self) -> str                 # last 4 KB ring for error messages
    def close(self) -> None                      # SIGTERM → 5 s → SIGKILL, OUR Popen only
```
Satisfies: V2 every clause; SPEC §12 runtime facts.

---

## `goldmine/ingest/`

### `ingest/__init__.py` **[v1]** — the pipeline
```python
@dataclass class IngestReport:  # everything SPEC §2 stdout shows, incl. capability grid
class IngestPipeline:
    def __init__(self, cfg: Config, store: Store, backend: EmbeddingBackend | None,
                 lexical_only: bool = False)
    def run(self, roots: list[str], *, code: bool = False, exclude: list[str] = [],
            dry_run: bool = False, reindex: bool = False,
            crawl_bounds: CrawlBounds | None = None,
            progress: Callable[[str], None] | None = None) -> IngestReport
    # walk → rules → unchanged? → extract (ThreadPool) → chunk → embed (serial batches)
    # → store.replace_segments; classification per file (V5a); SIGINT-safe cancel flag
```

### `ingest/rules.py` **[v1]** — DigUp Rules.swift port, extension-set based
```python
DOC_EXTS, CODE_EXTS, SECRET_EXTS, SECRET_PREFIXES, MINIFIED_SUFFIXES, LICENSE_PREFIXES
SKIP_FOLDERS, PROJECT_MARKERS, DATASET_HINTS[stretch]     # verbatim DigUp sets + additions
@dataclass class Decision: kind: str | None; skip_reason: str | None   # kind ∈ doc|pdf|code|
                                                       # image|audio|video|webpage|None
def classify(name: str, ext: str, *, code: bool) -> Decision
def is_secret(name: str) -> bool
def looks_minified(name: str) -> bool
def is_license(name: str) -> bool
def folder_skip(name: str) -> str | None               # None = descend
def dataset_folders(cands: list[tuple[str, str]]) -> dict[str, int]   # [stretch] DigUp heuristic
```
Satisfies: D6 (Rules port; replaces UTType with extension sets + `util` magic sniffing).

### `ingest/walk.py` **[v1]**
```python
@dataclass class Candidate: path: Path; kind: str; size: int; mtime: float
@dataclass class SkipNotice: path: str; reason: str          # unsupported/skip reporting
def walk(roots: list[Path], *, code: bool, exclude: list[str],
         max_file_bytes: int) -> Iterator[Candidate | SkipNotice]
    # os.scandir recursion, sorted, deterministic; SKIP_FOLDERS + exclude globs;
    # dir symlinks not followed + (st_dev, st_ino) visited set (V5c cycle guard);
    # MPEG-TS sniff for .ts (Rules.isMPEGTransportStream port)
def compute_deletions(store: Store, roots: list[str]) -> list[int]
```

### `ingest/extract_text.py` **[v1]**
```python
@dataclass class Extracted: text: str; title: str | None; detail: str | None
                            # detail: "lossy-decode: windows-1252", "textutil@26.5", …
                            # tool_version: recorded per file (V5d)
def decode_bytes(data: bytes, declared_charset: str | None = None,
                 override: str | None = None) -> tuple[str, str | None]
    # BOM (utf-8-sig/utf-16/32) → utf-8 strict → declared → windows-1252 lossy (+detail)  (V5b)
def extract_txt(path, encoding_override=None) -> Extracted
def extract_docx(path) -> Extracted               # zipfile + xml.etree: w:t text, w:p newlines
def extract_via_textutil(path, fmt_hint: str) -> Extracted | None   # rtf/doc/odt; None if
                                                                    # textutil missing → caller
                                                                    # records metadata_only
def extract_markdown(path, encoding_override=None) -> Extracted   # txt path + title from `# `
```

### `ingest/extract_pdf.py` **[v1]**
```python
def pdf_info(path: Path) -> dict | None            # pdfinfo: pages, title (None if missing tool)
def extract_pages(path: Path, *, max_pages: int = 5000, first_step: int = 30,
                  timeout_s: float = 60.0) -> PdfResult
    # PdfResult(pages: list[tuple[int, str]], total: int, title: str | None, truncated: bool,
    #            tool_version: str)
    # pdftotext -f/-l batches; pages < 200 chars → thin-text (FTS only, no vector)  (V5c/d)
```

### `ingest/extract_html.py` **[v1]**
```python
def charset_declared(head: bytes) -> str | None   # <meta charset> / <meta http-equiv>
def html_to_text(html: str, base_url: str | None = None) -> tuple[str, str | None]
    # HTMLParser subclass: drop script/style/comment/head, block tags → \n, entities decoded
    # (DigUp plainText(fromHTML:) port incl. numeric entities); title from <title>
def extract_html_file(path, encoding_override=None) -> Extracted
```

### `ingest/crawl.py` **[v1]**
```python
@dataclass class CrawlBounds: max_pages=50; max_depth=2; max_mb_page=5; max_mb_total=200;
                              politeness=1.0; timeout_s=30.0; respect_robots=True
@dataclass class CrawledPage: url: str; final_url: str; status: int; html: str
                             ; sha256: str; fetched_at: float; detail: str | None
def canonicalize(url: str) -> str                  # lowercase scheme/host, strip fragment,
                                                   # resolve ../, drop default port
def crawl(start_urls: list[str], bounds: CrawlBounds) -> Iterator[CrawledPage | SkipNotice]
    # same-host; urllib with content-type check; ≤5 in-host redirects; urllib.robotparser;
    # politeness sleep between same-host fetches; 429/5xx backoff 1→2→4 s (≤3); URL dedupe;
    # all bounds enforced before side effects (V5e)
```

### `ingest/media.py` **[v1 for metadata; embedding via a multimodal backend = stretch]**
```python
def ffprobe_duration(path: Path) -> float | None          # None → metadata_only + detail
def audio_windows(path: Path, *, window: float = 30.0, hop: float = 25.0,
                  max_minutes: float = 120.0, tmp: Path) -> list[MediaPiece]   # [stretch-embed]
    # ffmpeg -ss/-t per window → 16 kHz mono PCM16 WAV in tmp; silence gate skipped
    # (rms < 0.003, DigUp's −50 dBFS); windows ≤ 30 s because the model ignores more
def video_frames(path: Path, *, interval: float = 3.0, max_frames: int = 1200,
                 max_minutes: float = 120.0, tmp: Path) -> list[MediaPiece]   # [stretch-embed]
    # ffmpeg fps=1/3 → JPEGs; dedup by 8×8 average hash ≤ 4 bits (shot dedup);
    # the hash itself is computed via `ffmpeg -vf scale=8:8 -f rawvideo -pix_fmt gray`
    # (64 gray bytes) because stdlib cannot decode JPEG — ffmpeg is already required here
def sniff_media(path: Path) -> str | None                 # "image"|"audio"|"video"|None
    # extension + magic (riff/ftyp/mp3/ogg/flac…) — runs even with no ffmpeg, so capability
    # reporting is exact without any tool present
```

---

## Tests (repo, not shipped in package) — see [TEST_PLAN.md](TEST_PLAN.md)

```
tests/conftest.py            fixtures: tmp index, mock backend, fixture builders
tests/fixtures/tiny.pdf      committed 1-page PDF with known text
tests/fake_llama_server.py   stdlib http.server: /health 200, /v1/embeddings deterministic
tests/test_rules.py  test_walk.py  test_decode.py  test_chunker.py
tests/test_store.py  test_search_offline.py  test_distill.py  test_crawl_bounds.py
tests/test_llama_client.py   (against fake_llama_server — subprocess+HTTP logic, no binary)
tests/test_cli.py            end-to-end: ingest/search/distill/status with mock backend
tests/eval/test_relevance.py R@1/R@5 harness: lexical vs hybrid vs rrf (mock, offline);
                             GOLDMINE_LIVE=1 adds llama
```

## Build order (suggested passes, each lands green)

1. `config` + `util` + `store` (+DDL) + `fingerprints` + `backends/{base,mock}` — offline core
2. `rules` + `walk` + `extract_text` + `extract_html` + `chunker` + pipeline (lexical-only) —
   searchable without any backend
3. `search` (+baselines) + `cli` status/search — first usable release offline
4. `backends/llama` + `fake_llama_server` tests — embeddings online
5. `extract_pdf` + `crawl` + `doctor`/`backends`/`reset` commands — v1 complete
6. `distill` — cards
7. [stretch] `media` windows/frames embedding behind GOLDMINE_LIVE verification
