"""Index untouched OpenSLR members for source-first manual question curation."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parent


def catalog():
    chapters = {}
    for number, line in enumerate((ROOT / "acquisition/CHAPTERS.TXT").read_text().splitlines(), 1):
        if line.startswith(";") or not line.strip():
            continue
        cols = [c.strip() for c in line.split("|")]
        chapters[cols[0]] = dict(zip(
            ("chapter_id", "speaker_id", "minutes", "subset", "project_id", "book_id", "chapter_title", "project_title"), cols
        ), metadata_line=number)
    transcripts, audio = {}, {}
    # One forward pass: avoid repeatedly seeking backward through gzip members.
    with tarfile.open(ROOT / "acquisition/dev-clean.tar.gz", "r|gz") as tar:
        for member in tar:
            if member.name.endswith(".trans.txt"):
                transcripts[member.name] = tar.extractfile(member).read().decode("utf-8").splitlines()
            elif member.name.endswith(".flac"):
                header = tar.extractfile(member).read(42)
                assert header[:4] == b"fLaC" and header[4] & 127 == 0
                packed = int.from_bytes(header[18:26], "big")
                rate, frames = (packed >> 44) & 0xfffff, packed & 0xfffffffff
                audio[member.name] = {"sample_rate": rate, "frames": frames, "duration_s": frames / rate}
    records = []
    for transcript, lines in transcripts.items():
        chapter = chapters[transcript.split("/")[-2]]
        choices = []
        for number, line in enumerate(lines, 1):
            uid, text = line.split(" ", 1)
            name = transcript.rsplit("/", 1)[0] + "/" + uid + ".flac"
            info = audio[name]
            if 5 <= info["duration_s"] <= 12 and len(text.split()) >= 14:
                choices.append({"id": uid, "text": text, **info, "member": name,
                                "transcript_member": transcript, "transcript_line": number})
        if choices:
            records.append({**chapter, "choices": choices})
    (ROOT / "acquisition/asr-candidates.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    books, speakers, selected = set(), set(), []
    # Diversity rule only. No model or retrieval scores are involved.
    for record in records:
        if record["book_id"] in books or record["speaker_id"] in speakers:
            continue
        selected.append(record)
        books.add(record["book_id"])
        speakers.add(record["speaker_id"])
    (ROOT / "acquisition/asr-diverse-candidates.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
    for i, record in enumerate(selected, 1):
        print(i, record["book_id"], record["speaker_id"], record["project_title"], record["chapter_id"])
        for choice in record["choices"][:3]:
            print(" ", choice["id"], round(choice["duration_s"], 2), choice["text"])
    print("distinct book/speaker pairs", len(selected))


def extract():
    acquisition = ROOT / "acquisition"
    authored = json.loads((acquisition / "asr-authored.json").read_text())
    candidates = json.loads((acquisition / "asr-candidates.json").read_text())
    choices = {choice["id"]: {**record, **choice} for record in candidates for choice in record["choices"]}
    selected = {a["id"]: choices[a["id"]] for a in authored}
    assert len({r["book_id"] for r in selected.values()}) == 20
    assert len({r["speaker_id"] for r in selected.values()}) == 20
    wanted_audio = {r["member"]: uid for uid, r in selected.items()}
    wanted_transcripts = {r["transcript_member"] for r in selected.values()}
    (acquisition / "transcripts").mkdir(exist_ok=True)
    with tarfile.open(acquisition / "dev-clean.tar.gz", "r|gz") as tar:
        for member in tar:
            if member.name in wanted_audio:
                uid = wanted_audio[member.name]
                (ROOT / f"assets/media/asr/{uid}.flac").write_bytes(tar.extractfile(member).read())
            elif member.name in wanted_transcripts:
                (acquisition / "transcripts" / Path(member.name).name).write_bytes(tar.extractfile(member).read())
    speakers = {}
    for number, line in enumerate((acquisition / "SPEAKERS.TXT").read_text().splitlines(), 1):
        if line.startswith(";") or not line.strip():
            continue
        cols = [c.strip() for c in line.split("|")]
        speakers[cols[0]] = {"reader_name": cols[4], "speaker_metadata_line": number}
    release = json.loads((acquisition / "openslr-release.json").read_text())
    records = []
    for authored_question in authored:
        uid = authored_question["id"]
        record = selected[uid]
        asset = ROOT / f"assets/media/asr/{uid}.flac"
        data = asset.read_bytes()
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,sample_rate,channels,duration",
            "-of", "json", str(asset),
        ], text=True))
        duration = float(probe["format"]["duration"])
        assert abs(duration - record["duration_s"]) < 0.000001
        assert probe["streams"][0]["sample_rate"] == "16000"
        assert probe["streams"][0]["channels"] == 1
        transcript = acquisition / "transcripts" / Path(record["transcript_member"]).name
        original = transcript.read_bytes()
        raw_line = original.decode("utf-8").splitlines()[record["transcript_line"] - 1]
        assert raw_line == uid + " " + record["text"]
        assert authored_question["span"] in record["text"]
        record = {k: v for k, v in record.items() if k != "choices"}
        record.update(speakers[record["speaker_id"]])
        record.update({
            "asset": asset.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "ffprobe": probe,
            "transcript_file": transcript.relative_to(ROOT).as_posix(),
            "transcript_sha256": hashlib.sha256(original).hexdigest(),
            "gutenberg_book_url": f"https://www.gutenberg.org/ebooks/{record['book_id']}",
            "librivox_reader_url": f"https://librivox.org/reader/{record['speaker_id']}",
            "librivox_project_api_url": f"https://librivox.org/api/feed/audiobooks?id={record['project_id']}&format=json",
        })
        grant = (
            "LibriSpeech (c) 2014 by Vassil Panayotov; LibriSpeech corpus CC BY 4.0.\n"
            f"Primary corpus page: {release['publisher']}\nOriginal archive: {release['url']}\n"
            f"Archive MD5: {release['published_md5']}\nArchive SHA256: {release['archive_sha256']}\n"
            f"Unchanged original audio member: {record['member']}\nAudio SHA256: {record['sha256']}\n"
            f"LibriVox reader: {record['reader_name']} ({record['librivox_reader_url']})\n"
            f"Book: {record['project_title']} ({record['gutenberg_book_url']})\n"
            f"Chapter: {record['chapter_title']} (LibriVox chapter ID {record['chapter_id']})\n"
            f"Official transcript member: {record['transcript_member']}\n"
            f"Official transcript SHA256: {record['transcript_sha256']}; line {record['transcript_line']}\n"
            "Audio changes: none. The official publisher already segmented these natural human recordings.\n"
            "Oracle changes: remove publisher utterance-ID prefix; NFC/LF normalization only.\n"
            "No speech synthesis, ASR output, or generated test fixture supplied the oracle.\n\n"
            + (acquisition / "LICENSE.TXT").read_text()
            + "\n" + (ROOT / "licenses/media/CC-BY-4.0.txt").read_text()
        )
        license_path = ROOT / f"licenses/media/librispeech-{uid}.txt"
        license_path.write_text(grant, encoding="utf-8")
        record["license_file"] = license_path.relative_to(ROOT).as_posix()
        records.append(record)
        print(uid, record["project_title"], record["reader_name"], round(duration, 4))
    (acquisition / "asr.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    extract() if "--extract" in sys.argv else catalog()
