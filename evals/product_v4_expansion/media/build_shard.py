"""Compile manually authored, source-first media labels; never run retrieval."""

import hashlib
import json
from pathlib import Path
import unicodedata


ROOT = Path(__file__).resolve().parent


def read_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reference(path):
    return {"path": path, "sha256": sha((ROOT / path).read_bytes())}


def view(path, view_id, text, evidence_type, location=None, storage_field=None):
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / path).write_text(text, encoding="utf-8")
    return {"id": view_id, **reference(path), "evidence_type": evidence_type,
            "location": location, "storage_field": storage_field, "normalization": "nfc-lf-v1"}


def evidence(source, oracle, text):
    full = (ROOT / oracle["path"]).read_text(encoding="utf-8")
    start = full.index(text)
    expected = {
        "evidence_id": "e1", "path": source["path"], "kind": source["kind"], "grade": 3,
        "evidence_type": oracle["evidence_type"], "view_id": oracle["id"], "location": oracle["location"],
        "span": {"start_char": start, "end_char": start + len(text), "text": text, "sha256": sha(text.encode("utf-8"))},
    }
    if oracle["storage_field"] is not None:
        expected["storage_field"] = oracle["storage_field"]
    return expected


def question(qid, source, oracle, text, q, fact, tags, needs):
    return {"id": qid, "primary_class": "media-text", "tags": ["real", *tags], "q": q,
            "expect": [evidence(source, oracle, text)],
            "required_facts": [{"fact_id": "f1", "description": fact, "sufficient_evidence": [["e1"]]}],
            "needs": needs, "source_group": source["source_group"]}


def asr():
    release = read_json("acquisition/openslr-release.json")
    authored = {a["id"]: a for a in read_json("acquisition/asr-authored.json")}
    sources, queries = [], []
    for record in read_json("acquisition/asr.json"):
        uid = record["id"]
        label = authored[uid]
        location = {"unit": "second", "start": 0, "end": record["duration_s"]}
        oracle = view(f"views/media/asr/{uid}.txt", "official-transcript", record["text"] + "\n", "asr_text", location)
        source = {
            "path": f"media/asr/{uid}.flac", "asset": reference(record["asset"]), "kind": "audio",
            "source_group": f"librispeech:book:{record['book_id']}", "parents": [],
            "provenance": {
                "type": "real", "origin": {"kind": "url", "url": release["url"],
                    "revision": f"archive_sha256:{release['archive_sha256']};member:{record['member']};member_sha256:{record['sha256']}"},
                "license": "CC-BY-4.0", "license_file": reference(record["license_file"]),
                "generation": (
                    "No generated media. Original publisher-segmented natural LibriVox recording, unchanged FLAC bytes. "
                    f"Independent official transcript member {record['transcript_member']}, line {record['transcript_line']}, "
                    f"SHA256 {record['transcript_sha256']}; remove only the utterance-ID prefix and apply NFC/LF. "
                    "Official uppercase spelling is preserved; no Whisper output or punctuation edits. "
                    f"Time range is the whole utterance, measured as {record['frames']} frames / {record['sample_rate']} Hz and checked by ffprobe; "
                    "no internal word timestamps are claimed. "
                    f"Reader {record['reader_name']} ({record['librivox_reader_url']}); "
                    f"book {record['project_title']} ({record['gutenberg_book_url']})."
                ),
            }, "views": [oracle],
        }
        sources.append(source)
        queries.append(question(f"media-real-asr-{uid}", source, oracle, label["span"], label["q"], label["fact"], ["asr"], ["asr"]))
    return sources, queries


def photos():
    authored = {a["pageid"]: a for a in read_json("acquisition/photos-authored.json")}
    sources, queries = [], []
    for record in read_json("acquisition/photos.json"):
        pageid = record["pageid"]
        label = authored[pageid]
        basename = view(f"views/media/photos/{pageid}-basename.txt", "original-basename", record["basename"],
                        "name_metadata", storage_field="basename")
        taken = view(f"views/media/photos/{pageid}-DateTimeOriginal.txt", "exif-DateTimeOriginal", record["taken"],
                     "name_metadata", storage_field="info.taken")
        source = {
            "path": f"media/photos/{record['basename']}", "asset": reference(record["asset"]), "kind": "image",
            "source_group": f"commons:photographer:{record['artist']}", "parents": [],
            "provenance": {
                "type": "real", "origin": {"kind": "url", "url": record["permalink"],
                    "revision": f"description_revid:{record['page_revision']};upload:{record['upload_timestamp']};sha1:{record['published_sha1']};sha256:{record['sha256']}"},
                "license": "CC-BY-SA-4.0", "license_file": reference(record["license_file"]),
                "generation": (
                    f"No generated media or metadata. Original Commons JPEG bytes and published URL basename preserved; author {record['artist']}. "
                    f"Original image URL {record['url']}. EXIF DateTimeOriginal {record['exif_DateTimeOriginal']} was verified both in the "
                    "primary imageinfo metadata and directly in the original JPEG APP1/TIFF bytes, then formatted as YYYY-MM-DD HH:MM:SS "
                    "for the intended info.taken storage field. No timezone is inferred. The oracle is camera-original time, not modified time; "
                    "production sips disagreements are retained in acquisition diagnostics and never change this truth. "
                    "Filename facts concern the actual original basename only; no visual description or OCR text is claimed. "
                    "Primary CC BY-SA 4.0 license/attribution verified in the pinned file-description revision and retained with legal terms."
                ),
            }, "views": [basename, taken],
        }
        sources.append(source)
        queries.append(question(f"media-real-photo-{pageid}-filename", source, basename, record["basename"], label["filename_q"],
                                f"The original published filename is {record['basename']}.", ["metadata", "filename"], []))
        queries.append(question(f"media-real-photo-{pageid}-taken", source, taken, record["taken"], label["taken_q"],
                                f"EXIF DateTimeOriginal is {record['taken']} in the camera's unspecified local timezone.", ["metadata"], ["exif"]))
    return sources, queries


if __name__ == "__main__":
    audio_sources, audio_queries = asr()
    photo_sources, photo_queries = photos()
    shard = {"sources": audio_sources + photo_sources, "queries": audio_queries + photo_queries}
    assert len(shard["sources"]) == 30 and len(shard["queries"]) == 40
    (ROOT / "shard.json").write_text(json.dumps(shard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Wrote 30 real sources and 40 source-first questions (20 ASR, 20 metadata).")
