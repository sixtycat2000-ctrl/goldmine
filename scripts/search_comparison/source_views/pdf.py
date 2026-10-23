"""Every asset page, independently counted, with an explicit text/empty-OCR rule."""
from __future__ import annotations

import json
import re

from answer_eval.protocol import EvaluationError
from .coverage import unit
from .media import ocr_text


def convert_pdf(source, asset, commands, limits):
    raw, _, generated = commands.call("pdf.probe", "pdf-probe", input=asset)
    if generated:
        raise EvaluationError("PDF page probe produced unregistered sidecars", status="MISSING")
    matches = re.findall(r"^Pages:\s*([0-9]+)\s*$", raw, flags=re.MULTILINE)
    if len(matches) != 1 or not 1 <= int(matches[0]) <= limits["max_pages"]:
        raise EvaluationError("pdfinfo did not establish a bounded complete page denominator", status="MISSING")
    count = int(matches[0])
    match = re.search(r"asset-to-original page map=(\[[0-9, ]+\])", source["provenance"]["generation"])
    publisher = json.loads(match[1]) if match else None
    if publisher is not None and (len(publisher) != count or any(type(p) is not int or p < 1 for p in publisher)):
        raise EvaluationError("publisher page mapping disagrees with independently counted asset pages", status="MISSING")
    units = []
    for page in range(1, count + 1):
        details = {"publisher_page": publisher[page - 1] if publisher else None}
        try:
            text, _, generated = commands.call("pdf.text", f"page-{page:05d}-text", input=asset, page=page)
            if generated:
                raise EvaluationError("pdftotext produced unexpected unregistered files")
            parts = text.split("\f")
            if len(parts) > 1 and parts[-1] == "":
                parts.pop()
            if len(parts) != 1:
                raise EvaluationError("one requested page returned a different page window", status="MISSING")
            text = parts[0]
            method = "pdftotext-full-page/v1"
            if not text.strip():
                _, directory, generated = commands.call("pdf.raster", f"page-{page:05d}-raster", input=asset, page=page)
                filenames = list(generated)
                if len(filenames) != 1 or re.fullmatch(r"output-0*" + str(page) + r"\.png", filenames[0]) is None:
                    raise EvaluationError("raster output does not identify the exact requested full page", status="MISSING")
                text, blocks = ocr_text(directory / filenames[0], commands, f"page-{page:05d}-ocr")
                details["blocks"] = blocks
                method = "vision-full-raster-page/v1"
            units.append(unit("page", page, page, text, method, details=details))
        except (EvaluationError, OSError, ValueError) as error:
            units.append(unit("page", page, page, "", "page-conversion-failed", error=str(error), details=details))
    return count, units
