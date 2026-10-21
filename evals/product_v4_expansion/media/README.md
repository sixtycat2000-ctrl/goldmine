# Real media source shard

This is source-first curation for a new, separate product.v4 expansion. It is not
a frozen benchmark, a label-audit approval, a retrieval run, or a quality claim.

The target is 40 new media-text questions: 20 ASR questions from 20 untouched
LibriSpeech recordings, and 20 metadata questions from 10 untouched Commons
photographs. The ASR recordings have 20 distinct Project Gutenberg book IDs and
20 distinct LibriVox reader IDs. The photographs have 10 distinct credited
photographers. Each audio source has one question; each photo has two.

## Portable layout

`shard.json` contains only the strict `sources` and `queries` arrays. Copy the
three trees `assets/`, `views/`, and `licenses/` into the aggregate bundle without
changing their relative paths. Manifest asset references are
`assets/media/...`, oracle references `views/media/...`, and licence references
`licenses/media/...`. Corpus source paths are `media/asr/...` and
`media/photos/...`. Do not index the oracle views, question labels, acquisition
records, attribution text, or curation utilities as source content.

Keep this directory's `acquisition/` records with the curation materials. They
include the complete official transcript files used by the selected utterances,
official book/reader/chapter metadata, archive hashes, pinned Commons responses,
per-image original EXIF verification, and manually authored question worksheets.

## Audio provenance and truth

[OpenSLR SLR12](https://www.openslr.org/12) explicitly licenses LibriSpeech under
CC BY 4.0. The official `dev-clean.tar.gz` was downloaded from the publisher's
mirror, verified against its published MD5
`42e2234ba48799c1f50f24a7926300a1`, and given a local SHA256. The original corpus
`LICENSE.TXT`, reader/chapter/book metadata and selected complete `.trans.txt`
members are retained. Each retained FLAC is the publisher's original short
segmentation of a natural human LibriVox recording. No new audio was synthesized,
trimmed, re-encoded, or otherwise edited.

Question authors read the official transcripts before selecting evidence and
writing questions. Selection used source diversity and short clip duration, not
ASR output, retrieval ranking, or model success. Every canonical oracle retains
the official uppercase text, removing only the utterance-ID prefix and applying
NFC/LF normalization. Evidence spans use exact Unicode character offsets and
SHA256. The official transcript file/member, its SHA256, utterance ID and actual
line number are retained independently of the oracle view.

All audio locations are `[0, duration]` for a complete utterance. Duration is
computed from original FLAC STREAMINFO frame count/sample rate and independently
checked with `ffprobe`. The 20 utterances total 167.8200625 seconds. No internal
word timestamps are asserted. No ASR model was run during curation. Whisper's
case and punctuation may differ from the official transcript; changing the
oracle to follow model output would invalidate the independent truth. Alignment
normalization is an evaluator concern requiring a separately reviewed contract.

## Photograph provenance and truth

Every original was selected from a primary Wikimedia Commons file-description
page with an explicit CC BY-SA 4.0 grant. Per-source licence files include author,
immutable page revision, original image URL/upload timestamp, published SHA1,
local SHA256, a no-byte-changes declaration and the full licence terms. Original
bytes must match Commons imageinfo's published SHA1 and byte size. These are
original JPEG downloads, not Commons thumbnails. Published URL basenames are
preserved, including their underscores, accents and identifiers.

The first question for each photograph concerns its actual original filename.
It does not claim that a visible object has been recognized. The second concerns
camera-original EXIF `DateTimeOriginal`, verified both against the primary
imageinfo metadata and by directly reading the original JPEG APP1/TIFF bytes.
The oracle uses `YYYY-MM-DD HH:MM:SS`, the intended `info.taken` representation;
no timezone is invented. `needs=["exif"]` is declared on all capture-time
questions. Metadata evidence has no invented page, line, time span or body text.

The River Narmada photograph intentionally retains a real counterexample:
`DateTimeOriginal` and `DateTimeDigitized` are `2016:12:23 15:59:38`, whereas IFD0
`DateTime` is `2017:02:19 23:57:51`. Current `sips -g creation` and production
`exif_taken()` return the latter. The oracle keeps the genuine camera-original
time. `acquisition/photo-56412240.json` records the disagreement; it must not be
filtered out, relabeled as modified time, or used to soften acceptance.
Seven of the ten selected originals exhibit this modified-versus-capture
disagreement; all seven are listed in `validation.json`. None was excluded.

The ambiguous historical licensing coverage of `ianare/exif-samples` was not
assumed. No files from that collection are used. Commons returned a publisher
HTTP 429 during initial acquisition with `Retry-After: 600`; the acquisition was
paused for the required cooldown before continuation, without bypassing it.

## Reproduction and validation

The acquisition scripts are curation utilities, not production dependencies.
`acquire_asr.py` catalogs an official archive and `--extract` preserves the
manually selected members. `acquire_photos.py` preserves the selected publisher
originals and primary revision/EXIF records. `build_shard.py` compiles the fixed
manual worksheets, computes exact source/view/span/licence hashes, and writes
the strict shard. The large original archive is not needed in the portable
corpus once its members and provenance have been verified; it can be
re-downloaded from the recorded official URL.

Validation must call the actual product bundle source and label validators,
check the subtype counts, retain source groups, and verify all payload hashes.
An intentionally incomplete shard should not be passed off as the complete
420-question bundle. Merging, grouped splitting and the independent label audit
belong to the aggregate dataset workflow. No split, FROZEN marker or audit
approval is authored here.
