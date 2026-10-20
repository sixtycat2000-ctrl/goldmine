"""Score frozen topics and trace each excerpt to a particular stored segment."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json


def normalized_span(text, excerpt):
    """Whitespace folding only; retain case and map matches to original offsets."""
    characters, offsets = [], []
    pending = False
    for index, char in enumerate(text):
        if char.isspace():
            pending = bool(characters)
            continue
        if pending:
            characters.append(" ")
            offsets.append(index)
            pending = False
        characters.append(char)
        offsets.append(index)
    rendered = "".join(characters)
    needle = " ".join(excerpt.removesuffix("…").split())
    at = rendered.find(needle)
    if not needle or at < 0:
        return None
    return [offsets[at], offsets[at + len(needle) - 1] + 1]


def excerpt_audit(store, cards):
    from goldmine.citations import read_evidence, resolve_citation

    rows, review = [], []
    for card in cards:
        if card.status != "active":
            continue
        verdicts = []
        for excerpt in card.excerpts:
            matches = [m for m in card.members if (m.path, m.kind, m.loc, m.loc_end) ==
                       (excerpt.get("path"), excerpt.get("kind"), excerpt.get("loc"), excerpt.get("loc_end"))]
            found = None
            for member in matches:
                row = store.db.execute("SELECT coalesce(original,body) FROM fts WHERE rowid=?", (member.sid,)).fetchone()
                original = row[0] if row else None
                span = normalized_span(original, excerpt["text"]) if original else None
                if span is None:
                    continue
                citation = read_evidence(store, member.sid).citation
                recovered = resolve_citation(store, citation.citation_id)
                if recovered.text != original:
                    continue
                found = {"segment_id": member.sid, "citation_id": citation.citation_id,
                         "stored_sha256": hashlib.sha256(original.encode()).hexdigest(), "char_span": span}
                break
            verdict = {"card_id": card.topic_id, "excerpt": excerpt, "passed": found is not None,
                       "evidence": found}
            rows.append(verdict)
            verdicts.append(verdict["passed"])
        original_text = "\n".join(store.segment_text(m.sid) or "" for m in card.members)
        if card.title_kind == "sentence":
            supported = normalized_span(original_text, card.title) is not None
        else:
            supported = bool(card.terms) and all(term.casefold() in original_text.casefold() for term in card.terms)
        review.append({"card_id": card.topic_id, "title": card.title, "title_kind": card.title_kind,
                       "title_supported": supported, "sources_locatable": bool(verdicts) and all(verdicts),
                       "verdict": "PASS" if supported and verdicts and all(verdicts) else "FAIL"})
    review.sort(key=lambda row: row["card_id"])
    return {"checked": len(rows), "passed": sum(row["passed"] for row in rows), "rows": rows,
            "normalization": "whitespace-v1; displayed truncation ellipsis excluded; original char spans retained",
            "fixed_card_review": review[:20], "review_mode": "automatic original-source/title-support audit"}


def topic_metrics(cards, kept, labels, topics):
    active = [card for card in cards if card.status == "active"]
    dominant_total, total = 0.0, 0
    recovery, assigned = set(), set()
    totals = Counter(topic for row in labels.values() if row["role"] == "topic" for topic in row["topics"])
    for card in active:
        paths = {member.path for member in card.members}
        counts = Counter()
        for path in sorted(paths):
            row = labels[path]
            assigned.add(path)
            for topic in row["topics"]:
                counts[topic] += 1 / len(row["topics"])
        largest = counts.most_common(1)
        total += len(paths)
        if largest:
            topic, support = largest[0]
            dominant_total += support
            primary = sum(labels[p]["role"] == "topic" and labels[p]["topics"] == [topic] for p in paths)
            if support / max(1, len(paths)) >= .70 and primary >= max(1, totals[topic] * .5):
                recovery.add(topic)
    represented = {member.path for member in kept}
    return {"purity": dominant_total / total if total else 0.0, "topics_recovered": len(recovery),
            "recovered_topic_ids": sorted(recovery), "topics_total": len(topics),
            "document_coverage": len(assigned) / len(represented) if represented else 0.0,
            "unclustered_ratio": 1 - sum(len(card.members) for card in active) / len(kept) if kept else 1.0,
            "max_card_share": max((len(card.members) for card in active), default=0) / len(kept) if kept else 0.0,
            "recovery_rule": "dominant support>=70% of card documents and >=50% of frozen primary topic documents"}


def membership_changes(incremental, full):
    def groups(cards):
        return {m.sid: tuple(sorted(x.sid for x in card.members)) for card in cards
                if card.status == "active" for m in card.members}

    inc, recomputed = groups(incremental), groups(full)
    return [{"segment_id": sid, "incremental_members": inc.get(sid, ()),
             "full_members": recomputed.get(sid, ()),
             "explanation": "incremental preserves eligible existing memberships; full reruns the leader at identical inherited tau"}
            for sid in sorted(set(inc) | set(recomputed)) if inc.get(sid) != recomputed.get(sid)]
