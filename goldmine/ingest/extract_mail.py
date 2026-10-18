"""EML extraction (v2, 02 §5.5): stdlib `email` -> Extracted.

Headers: Subject (RFC2047-decoded by policy.default) -> title; From/Date ->
`info` fields the pipeline merges into files.info. Body = the first
text/plain part; when the mail is html-only the html part goes through the
shared stripper (extract_html.html_to_text). Attachments are NEVER extracted
or indexed — only counted, recorded as info.attachments_not_indexed.
extractor_version "eml-stdlib.v2".

`.msg` (Outlook OLE compound) stays honestly unsupported: it needs a
third-party parser and textutil cannot read it (02 §5.5).
"""

from __future__ import annotations

import email
import logging
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from .extract_html import html_to_text
from .extract_text import Extracted

logger = logging.getLogger("goldmine.extract_mail")

EML_TOOL_VERSION = "eml-stdlib.v2"


def _decode_header(msg, name: str) -> str:
    """policy.default already decodes RFC2047 words; missing -> ''."""
    try:
        return str(msg.get(name, "") or "").strip()
    except Exception:  # unparseable encoded words: keep the raw value
        raw = msg.get(name)
        return str(raw).strip() if raw else ""


def extract_eml(path: Path) -> Extracted:
    """One .eml -> Extracted(text, title=Subject, info={format,from,date,...})."""
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    subject = _decode_header(msg, "Subject")
    from_header = _decode_header(msg, "From")
    date_header = _decode_header(msg, "Date")
    date_iso: str | None = None
    if date_header:
        try:
            date_iso = parsedate_to_datetime(date_header).isoformat()
        except (TypeError, ValueError):
            logger.debug("eml: unparseable Date %r", date_header)

    body: str | None = None
    html_body: str | None = None
    attachments = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            attachments += 1
            continue
        ctype = part.get_content_type()
        if body is None and ctype == "text/plain":
            try:
                body = part.get_content()
            except Exception as exc:  # bad charset/payload: try the raw bytes
                logger.debug("eml: text/plain decode failed (%s)", exc)
                body = None
        elif html_body is None and ctype == "text/html":
            try:
                html_body = part.get_content()
            except Exception:
                html_body = None

    if body is None and html_body is not None:
        body, _title = html_to_text(html_body)

    info: dict = {"format": "eml"}
    if from_header:
        info["from"] = parseaddr(from_header)[1] or from_header
    if date_iso:
        info["date"] = date_iso
    if attachments:
        info["attachments_not_indexed"] = attachments

    return Extracted(
        text=body or "",
        title=subject or None,
        detail=None,
        tool_version=EML_TOOL_VERSION,
        info=info,
    )
