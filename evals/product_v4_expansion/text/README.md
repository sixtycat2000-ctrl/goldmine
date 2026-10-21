This is an AI-curated, source-first text shard for the separate product-v4
expansion bundle. It is **DRAFT / awaiting independent source-label audit**.
It is not a retrieval result, benchmark claim, frozen holdout, or human review.

There are 180 individually authored questions over 36 verbatim upstream files,
with five questions per file. Semantic has 60 questions (30 code / 30 docs),
CJK has 60 (35 Chinese-only / 15 Chinese with Latin API names / 5 Japanese /
5 Korean), and lookup has 60 (15 filename/path / 17 mixed identifiers /
28 punctuated identifiers). Each class uses 12 distinct source files. Files
from different translated language sites cover different topics; no parallel
translation of the same page was used as an independent source group.

The curator read the pinned source before writing each question, its required
fact and unique evidence anchor. Question text is literal in the three
`metadata/author_*.py` files; those files do not generate question wording.
`metadata/assemble_shard.py` only computes canonical views, source descriptors,
Unicode character offsets, SHA-256 hashes, actual line ranges, IDs and schema
checks. These authoring materials stay outside the indexed `assets/` tree.
No candidate relevance run, candidate rank, model grading or holdout output
was read or executed while curating this shard.

`shard.json` contains only strict `sources` and `queries` arrays. Its references
are portable within the aggregate bundle: copy `assets/text`, `views/text` and
`licenses/text` into the corresponding aggregate directories, then merge the
arrays. Source paths begin `text/`. Raw assets are byte-for-byte copies; full
body views change only NFC and line endings. Name views contain actual file
basenames. Filename/path questions identify a bound source path and basename;
they do not pretend that a basename alone proves an absolute filesystem path.
No labels, questions or compilation helpers are inserted into source assets.

Upstream attribution and exact revisions are retained below. Full original
licenses, including notices, are in `licenses/text/`; GitHub commit API replies
and each downloaded raw URL, byte count, retrieval timestamp and SHA-256 are
preserved in `metadata/`. Branch references below are pinned to these exact
commits and are not floating runtime dependencies.

| Source | Revision | License and attribution | Files |
| --- | --- | --- | ---: |
| [Requests](https://github.com/psf/requests) 2.32.3 | `0e322af87745eff34caffe4df68456ebc20d9068` | MIT; Kenneth Reitz and contributors, full upstream LICENSE preserved | 6 |
| [Click](https://github.com/pallets/click) 8.1.7 | `874ca2bc1c30d93a4ac6e36a15ed685eafe89097` | BSD-3-Clause; Pallets and contributors, full upstream LICENSE.rst preserved | 6 |
| [CPython](https://github.com/python/cpython) 3.12.7 | `0b05ead877f909b7efe712db758012d9dbece7ce` | PSF-2.0 and historical notices in full root LICENSE; file-level notices preserved | 12 |
| [Official Vue Chinese documentation](https://github.com/vuejs-translations/docs-zh-cn) | `418f5ae6e15c7a90beae017e44d58dac3d93ad95` | CC-BY-4.0; Yuxi (Evan) You and Vue documentation contributors | 10 |
| [Official Vite Japanese documentation](https://github.com/vitejs/docs-ja) | `fe8db100c93c32cfbd3d86dc7c0261677063efac` | MIT; full upstream copyright/license preserved | 1 |
| [Official Vite Korean documentation](https://github.com/vitejs/docs-ko) | `2f8f1ecbacb579273f9a7c52544a9d2e80f46d2a` | MIT; full upstream copyright/license preserved | 1 |

The Vue license excludes upstream image files. This shard contains Markdown
text only from that project; it does not download or redistribute those images.
Existing image URLs and links remain verbatim in the original source text.

Mechanical reproduction from the repository root:

```sh
python3 evals/product_v4_expansion/text/metadata/author_semantic.py
python3 evals/product_v4_expansion/text/metadata/author_cjk.py
python3 evals/product_v4_expansion/text/metadata/author_lookup.py
python3 evals/product_v4_expansion/text/metadata/assemble_shard.py
```

Pending before any freeze: an independent fixed-sample source/label audit,
aggregate duplicate and source-group review across all 420 questions, and the
root-owned grouped development / sealed-holdout split. The previously exposed
28-question diagnostic seed is not reused here. No FROZEN state or label-review
completion is asserted by this shard.
