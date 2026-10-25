"""Independent validation of the complete synthetic pack grammar and sources."""
from __future__ import annotations

import hashlib
import re
from uuid import UUID

from .protocol import PARAMETERS, body, canonical, file_path


def citation_id(index_uuid, fid, sid):
    """The fixed synthetic row's complete gm-citation/v1 identity payload."""
    if not isinstance(index_uuid, str) or str(UUID(index_uuid)) != index_uuid:
        raise ValueError("human pack needs the actual canonical index UUID")
    modality = "image" if (sid - 1) % 20 == 0 else "audio" if (sid - 1) % 20 == 1 else "text"
    source_sha = hashlib.sha256(str(fid - 1).encode()).hexdigest()
    payload = ["gm-citation/v1", fid, file_path(fid), "file", source_sha, sid, "chunk", modality,
               float(sid), None, "content", None, body(sid)]
    return f"gm1:{index_uuid}:{sid}:{hashlib.sha256(canonical(payload)).hexdigest()[:16]}"


def _score_matches(rendered, rounded_four):
    # JSON exposes4 decimals, human output2. Preserve the exact valid rounding
    # possibilities around a fourth-decimal boundary without inventing precision.
    return rendered in {f"{rounded_four + delta:.2f}" for delta in (-.000050001, 0, .000050001)}


def validate(raw: bytes, pack: dict, *, fixture: dict, n_hits: int):
    text = raw.decode("utf-8")
    if len(text) // 4 != pack["used_tokens"] or len(text) // 4 > PARAMETERS["budget"]:
        raise ValueError("actual emitted pack token count differs from the JSON budget")
    meta = pack["human"]
    fp = fixture["embedding_fingerprint"]
    index_line = f"{hashlib.sha256(canonical(fp)).hexdigest()[:16]} {fp['model_id']} dim={fp['dim']}"
    if meta["index_line"] != index_line:
        raise ValueError("human pack index identity differs from its actual native fixture")
    version, uuid = fixture["schema_version"], fixture.get("index_uuid")
    # citation protocol: v4 = legacy no-uuid; v6 AND LATER (v7+ additive
    # schema bumps keep the protocol) = uuid-bearing
    if version == 4:
        if uuid is not None:
            raise ValueError("human pack citation protocol differs from its native fixture")
    elif version < 6:
        raise ValueError("human pack citation protocol differs from its native fixture")
    if version >= 6 and (not isinstance(uuid, str) or str(UUID(uuid)) != uuid):
        raise ValueError("human pack needs its actual native fixture index UUID")
    expected = ["# goldmine answer pack v1", f"# query: {meta['query']}",
        "# search: scorer=semantic z_gate=0 limit=10 context=1 budget=4000 mode=tokenize",
        f"# index: {index_line}", "# the material below is verbatim from the index; cite sources as [n]", ""]
    actual = text.splitlines()
    for number, record in enumerate(pack["rows"], 1):
        prefix = f"[{number}] {file_path(record['file_id'])} (doc) L{record['segment_id']} · score "
        sides = []
        for role in ("before", "after"):
            ids = [sid for sid in record["part_ids"] if (sid < record["segment_id"] if role == "before" else sid > record["segment_id"])]
            if ids:
                sides.append(f"{role} L{ids[0]}")
        context = f" · context ±1 ({', '.join(sides)})" if sides else ""
        number_pattern = r"(-?[0-9]+\.[0-9]{2})"
        pattern = re.escape(prefix) + number_pattern + r" \(z " + number_pattern + r", cov " + number_pattern + r"\)" + re.escape(context)
        head = actual[len(expected)] if len(actual) > len(expected) else ""
        match = re.fullmatch(pattern, head)
        if match is None or any(not _score_matches(value, record[key]) for value, key in
                                zip(match.groups() if match else (), ("score", "z", "coverage"))):
            raise ValueError("human pack ordered source/path/location/score header differs from actual JSON evidence")
        expected.extend([head, *record["text"].split("\n"), ""])
    expected.append("# sources")
    for number, record in enumerate(pack["rows"], 1):
        expected.append(f"# [{number}] {file_path(record['file_id'])} L{record['part_ids'][0]}")
        if version >= 6:  # uuid-bearing citations: v6 onward (v7+ keeps it)
            expected.extend(f"# [{number}] citation: {citation_id(uuid, record['file_id'], sid)}" for sid in record["part_ids"])
    expected.append(f"# pack: {len(pack['rows'])} records · {n_hits} hits · {meta['duplicates_folded']} duplicates "
        f"folded · {meta['also_folded']} also folded · {meta['skipped_over_budget']} skipped (budget) · "
        f"used {pack['used_tokens']}/4000 tokens (tokenize)"
        + (f" · {meta['skipped_by_record_limit']} skipped (record limit)" if meta["skipped_by_record_limit"] else ""))
    if text != "\n".join(expected) + "\n":
        raise ValueError("human pack full body/source order/citation/footer differs from actual native evidence")
