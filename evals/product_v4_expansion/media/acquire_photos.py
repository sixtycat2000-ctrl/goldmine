"""Preserve selected Commons originals and their primary attribution records.

Acquisition only: no OCR, ASR, retrieval, model, or benchmark code runs here.
"""

import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import struct
import time
import unicodedata
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parent
USER_AGENT = "Goldmine-local-eval/1.0 (open-license corpus curation)"


def fetch(url):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=90) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 429:
                delay = int(exc.headers.get("Retry-After", "600"))
                if attempt < 2 and 0 <= delay <= 30:
                    time.sleep(delay + 1)
                    continue
                raise RuntimeError(f"Publisher rate limit; honor Retry-After={exc.headers.get('Retry-After')}") from exc
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def text_html(value):
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


def jpeg_exif_dates(data):
    """Read the original TIFF date fields directly, independently of sips."""
    assert data[:2] == b"\xff\xd8"
    cursor = 2
    while cursor < len(data):
        assert data[cursor] == 255
        marker = data[cursor + 1]
        if marker in (0xda, 0xd9):
            break
        length = int.from_bytes(data[cursor + 2:cursor + 4], "big")
        payload = data[cursor + 4:cursor + 2 + length]
        cursor += 2 + length
        if marker != 0xe1 or payload[:6] != b"Exif\0\0":
            continue
        tiff = payload[6:]
        endian = {b"II": "<", b"MM": ">"}[tiff[:2]]
        assert struct.unpack_from(endian + "H", tiff, 2)[0] == 42

        def ifd(offset):
            count = struct.unpack_from(endian + "H", tiff, offset)[0]
            found = {}
            for n in range(count):
                pos = offset + 2 + n * 12
                tag, kind, size, value = struct.unpack_from(endian + "HHII", tiff, pos)
                if kind == 2 and tag in (0x132, 0x9003, 0x9004):
                    start = pos + 8 if size <= 4 else value
                    found[tag] = tiff[start:start + size].rstrip(b"\0").decode("ascii")
                elif kind == 4 and size == 1:
                    found[tag] = value
            return found

        first = ifd(struct.unpack_from(endian + "I", tiff, 4)[0])
        extra = ifd(first[0x8769])
        return {"DateTime": first.get(0x132), "DateTimeOriginal": extra.get(0x9003),
                "DateTimeDigitized": extra.get(0x9004)}
    raise ValueError("Original JPEG has no EXIF date fields")


def acquire(page):
    info = page["imageinfo"][0]
    ex = info["extmetadata"]
    assert ex["LicenseShortName"]["value"] == "CC BY-SA 4.0"
    assert ex["LicenseUrl"]["value"].rstrip("/") == "https://creativecommons.org/licenses/by-sa/4.0"
    url = urlunsplit(urlsplit(info["url"])._replace(query=""))
    basename = unicodedata.normalize("NFC", unquote(urlsplit(url).path.rsplit("/", 1)[1]))
    path = ROOT / "assets/media/photos" / basename
    if not path.exists():
        path.write_bytes(fetch(url))
        time.sleep(2)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert hashlib.sha1(data).hexdigest() == info["sha1"], (basename, "published SHA1 mismatch")
    assert len(data) == info["size"]
    revid = page["revisions"][0]["revid"]
    api_url = "https://commons.wikimedia.org/w/api.php?" + urlencode({
        "action": "query", "format": "json", "prop": "revisions", "revids": revid,
        "rvprop": "content|ids|timestamp", "rvslots": "main",
    })
    revision_path = ROOT / f"acquisition/commons-{page['pageid']}-revision.json"
    revision_raw = revision_path.read_bytes() if revision_path.exists() else fetch(api_url)
    revision = json.loads(revision_raw)
    wikitext = next(iter(revision["query"]["pages"].values()))["revisions"][0]["slots"]["main"]["*"]
    (ROOT / f"acquisition/commons-{page['pageid']}-revision.json").write_bytes(revision_raw)
    (ROOT / f"acquisition/commons-{page['pageid']}-wikitext.txt").write_text(wikitext, encoding="utf-8")
    supplemental_grant = ""
    if not re.search(r"cc.by.sa.4\.0", wikitext, re.I):
        # This selected source uses the photographer's explicit own-work
        # permission template. Preserve that template's immutable revision too.
        assert page["pageid"] == 29415136 and "{{User:Florstein/Inf" in wikitext
        template = json.loads((ROOT / "acquisition/commons-Florstein-Inf-template.json").read_text())
        rev = next(iter(template["query"]["pages"].values()))["revisions"][0]
        assert rev["revid"] == 855702164 and "cc-by-sa-4.0" in rev["slots"]["main"]["*"]
        supplemental_grant = (
            "Preferred attribution: © Alex Fedorov, Wikimedia Commons\n"
            "File's own-work/CC BY-SA 4.0 permission template: https://commons.wikimedia.org/w/index.php?oldid=855702164\n"
            "Photographer's usage/attribution notice: https://commons.wikimedia.org/w/index.php?oldid=1266782612\n"
            "The preferred credit is retained here; the grant also offers the Free Art License as an alternative.\n"
        )
    sips = subprocess.run(["sips", "-g", "creation", str(path)], text=True, capture_output=True, check=True)
    from goldmine.ingest.media import exif_taken
    taken = exif_taken(path)
    assert taken is not None, (basename, "production sips date unavailable")
    original_exif = next(m["value"] for m in info["metadata"] if m["name"] == "DateTimeOriginal")
    original_dates = jpeg_exif_dates(data)
    assert original_dates["DateTimeOriginal"] == original_exif
    capture_time = original_exif[:10].replace(":", "-") + original_exif[10:]
    permalink = f"https://commons.wikimedia.org/w/index.php?oldid={revid}"
    artist = text_html(ex["Artist"]["value"])
    if page["pageid"] == 29415136:
        artist = "Alex 'Florstein' Fedorov"
    grant = (
        f"Title: {page['title']}\nAuthor/attribution: {artist}\n"
        f"Primary file description (immutable page revision): {permalink}\n"
        f"Original published file: {url}\nOriginal upload timestamp: {info['timestamp']}\n"
        f"Published original SHA1: {info['sha1']}\nPreserved original SHA256: {digest}\n"
        f"License: CC BY-SA 4.0\nLicense URL: {ex['LicenseUrl']['value']}\n"
        "License grant verified in the primary imageinfo response and pinned page template.\n"
        "Changes to image bytes: none. Downloaded original; no thumbnail, cropping, re-encoding, or EXIF edits.\n"
        + supplemental_grant + "\n"
        + (ROOT / "licenses/media/CC-BY-SA-4.0.txt").read_text(encoding="utf-8")
    )
    license_path = ROOT / f"licenses/media/commons-{page['pageid']}.txt"
    license_path.write_text(grant, encoding="utf-8")
    result = {
        "pageid": page["pageid"], "title": page["title"], "artist": artist,
        "url": url, "permalink": permalink, "page_revision": revid,
        "upload_timestamp": info["timestamp"], "published_sha1": info["sha1"],
        "sha256": digest, "bytes": len(data), "basename": basename,
        "asset": path.relative_to(ROOT).as_posix(),
        "license_file": license_path.relative_to(ROOT).as_posix(),
        "taken": capture_time, "exif_DateTimeOriginal": original_exif,
        "original_jpeg_exif_dates": original_dates,
        "production_sips_taken": taken[0], "production_taken_matches_DateTimeOriginal": taken[0] == capture_time,
        "sips_stdout": sips.stdout, "capture_timezone": "unspecified in stored info.taken",
        "license": "CC-BY-SA-4.0", "original_bytes_changed": False,
    }
    (ROOT / f"acquisition/photo-{page['pageid']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("pageid", "basename", "artist", "bytes", "taken")}, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    selected = json.loads((ROOT / "acquisition/commons-selected.json").read_text(encoding="utf-8"))
    results = [acquire(page) for page in selected]
    (ROOT / "acquisition/photos.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
