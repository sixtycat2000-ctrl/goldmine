"""Repackage licensed real pages without adding source text or image pixels.

Run with the bundled Python (pypdf, reportlab, Pillow). Download receipts live
outside assets; originals are pinned by SHA. No queries or rankings are inputs.
"""
from pathlib import Path
from io import BytesIO
import hashlib
import json
import subprocess
import unicodedata

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parents[1]
CACHE = ROOT / "reports/execution/P0-05/expansion-source-cache"
ASSETS = HERE / "assets/root/pdf"
VIEWS = HERE / "views/root/pdf"
PAGES = {
    "nist-800-145": [6, 7],
    "nist-800-92": [15, 29, 30, 31, 37],
    "nist-800-22r1a": [24, 26, 29, 32, 42],
    "nist-800-38a": [15, 16, 22, 25, 28],
    "nist-800-107r1": [6, 8, 12, 16, 18],
    "pytest": [17, 64, 65, 88, 94],
    "sphinx": [21, 102, 109, 119, 229],
    "pillow": [16, 18, 30, 72, 155],
}
SCANS = ["US6469", "US174465", "US223898", "US821393"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    VIEWS.mkdir(parents=True, exist_ok=True)
    records = []
    acquisition = json.loads((CACHE / "root-acquisition.json").read_text())["pdfs"]
    acquisition += json.loads((CACHE / "pdf-acquisition.json").read_text())["pdfs"]
    original = {x["id"]: x for x in acquisition}
    for name, selected in PAGES.items():
        src = CACHE / "pdfs" / (name + ".pdf")
        assert digest(src.read_bytes()) == original[name]["sha256"]
        reader, writer = PdfReader(src), PdfWriter()
        for number in selected:
            writer.add_page(reader.pages[number - 1])
        writer.add_metadata({"/Title": name + " — unmodified published page excerpt",
                             "/Subject": "Original page map retained in provenance/pdf-pages.json"})
        output = ASSETS / (name + "-excerpt.pdf")
        writer.write(output)
        text = subprocess.check_output(["pdftotext", str(output), "-"], text=True)
        pages = text.split("\f")
        if pages[-1] == "":
            pages.pop()
        assert len(pages) == len(selected)
        for index, page in enumerate(pages, 1):
            (VIEWS / f"{name}-p{index:03d}.txt").write_text(normalized(page), encoding="utf-8")
        records.append({"id": name, "kind": "text_excerpt", "original": original[name],
                        "asset": "assets/root/pdf/" + output.name,
                        "sha256": digest(output.read_bytes()), "original_page_numbers": selected,
                        "generation": "Unmodified page objects extracted from the publisher PDF; no rasterization, new text, or source alteration. Asset page numbers follow the listed original-page map."})
    for name in SCANS:
        src = CACHE / "pdfs" / (name + ".pdf")
        assert digest(src.read_bytes()) == original[name]["sha256"]
        reader = PdfReader(src)
        output = ASSETS / (name + "-scan.pdf")
        pdf = canvas.Canvas(str(output), invariant=1, pageCompression=1)
        pixel_hashes = []
        for page in reader.pages:
            assert len(page.images) == 1, "A genuine single-raster scanned page is required"
            image = page.images[0].image.convert("RGB")
            pixel_hashes.append(digest(image.tobytes()))
            width, height = float(page.mediabox.width), float(page.mediabox.height)
            pdf.setPageSize((width, height))
            pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
            pdf.showPage()
        pdf.save()
        check = PdfReader(output)
        assert len(check.pages) == len(reader.pages)
        for index, page in enumerate(check.pages):
            assert len(page.images) == 1
            assert digest(page.images[0].image.convert("RGB").tobytes()) == pixel_hashes[index]
        text = subprocess.check_output(["pdftotext", str(output), "-"], text=True)
        assert not text.strip(), "Embedded machine OCR must not satisfy the scan test"
        records.append({"id": name, "kind": "real_scan_repackage", "original": original[name],
                        "asset": "assets/root/pdf/" + output.name,
                        "sha256": digest(output.read_bytes()),
                        "original_page_numbers": list(range(1, len(reader.pages) + 1)),
                        "original_and_repackaged_rgb_pixel_sha256": pixel_hashes,
                        "generation": "Real historical scanned raster pages repackaged losslessly, retaining identical decoded RGB pixels and page sizes. The publisher's inaccurate hidden OCR text layer is omitted to require actual OCR. No page was created from a born-digital text source; no text or pixels were added."})
    (HERE / "provenance/pdf-pages.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps({"pdf_assets": len(records), "asset_bytes": sum((HERE / x["asset"]).stat().st_size for x in records), "page_count": sum(len(x["original_page_numbers"]) for x in records)}))


if __name__ == "__main__":
    main()
