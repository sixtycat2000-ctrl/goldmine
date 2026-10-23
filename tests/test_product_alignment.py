"""Mechanical source/read-back/pack proofs; no model or relevance evidence."""

import copy
import hashlib
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval.protocol import EvaluationError
from product_eval.alignment import asr_word_spans, validate_word_alignment, word_tokens
from product_eval.evidence import emitted_spans, read_segment_spans
from product_eval.results import _proof, validate_report
from product_eval.scoring import score_query, summarize


def fixture(tmp_path, canonical, observed, *, fact=None, asr=True, stored_location=None):
    """A tiny in-memory stored segment, never a production/model receipt."""
    kind, etype = ("audio", "asr_text") if asr else ("pdf", "ocr_text")
    path = "clip.flac" if asr else "scan.pdf"
    actual = tmp_path / "corpus" / path
    location = {"unit": "second", "start": 0, "end": 10} if asr else {"unit": "page", "start": 1, "end": 1}
    stored = stored_location or location
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE files(id,path,info,source,kind)")
    db.execute("CREATE TABLE segments(id,file,kind,loc,loc_end)")
    db.execute("INSERT INTO files VALUES(1,?,?,?,?)", (str(actual), "{}", "mechanical-fixture", kind))
    db.execute("INSERT INTO segments VALUES(1,1,?,?,?)", (kind, stored["start"], stored["end"]))
    store = SimpleNamespace(db=db, segment_text=lambda segment_id: observed)
    view = {"id": "body", "text": canonical, "location": location, "evidence_type": etype}
    sources = {path: {"kind": kind, "views": [{"id": "body"}]}}
    fact = fact or canonical
    start = canonical.index(fact)
    expected = {"evidence_id": "e0", "path": path, "kind": kind, "grade": 3,
                "view_id": "body", "evidence_type": etype, "location": location,
                "span": {"start_char": start, "end_char": start + len(fact), "text": fact,
                         "sha256": hashlib.sha256(fact.encode()).hexdigest()}}
    query = {"id": "mechanical-1", "primary_class": "media-text", "tags": ["asr" if asr else "ocr"],
             "q": "Mechanical fixture question", "source_group": "fixture:1", "expect": [expected],
             "required_facts": [{"fact_id": "f0", "description": "one complete fixture fact", "sufficient_evidence": [["e0"]]}]}
    bundle = SimpleNamespace(sources=sources, views={(path, "body"): view}, identity={"unit": "only"}, queries=[query], splits=None)
    return store, bundle, query, actual


def record(actual, text, *, n=1, cite=True):
    return {"n": n, "path": str(actual), "kind": "audio" if actual.suffix == ".flac" else "pdf", "text": text,
            "segments": [{"segment_id": 1}] if cite else [], "segment_id": 1 if cite else None}


def pack(records):
    return "\n\n".join(f"[{r['n']}] {r['path']} ({r['kind']}) fixture\n{r['text']}" for r in records)


def evidence(store, bundle, actual, records):
    path = next(iter(bundle.sources))
    hits = [{"path": path, "spans": read_segment_spans(store, 1, path, actual, bundle),
             "kind": bundle.sources[path]["kind"], "loc": 0, "loc_end": 10}]
    emitted = emitted_spans(store, {"records": records}, pack(records), actual.parent, bundle)
    return hits, emitted


def report(bundle, query, hits, emitted, records):
    context = pack(records)
    row = {**{key: query[key] for key in ("id", "primary_class", "tags", "source_group")},
           "metrics": score_query(query, hits, emitted), "hits": hits, "emitted_spans": emitted,
           "pack_records": records, "context_text": context, "pack_sha256": hashlib.sha256(context.encode()).hexdigest(),
           "used_tokens": 100, "token_mode": "mechanical-fixture"}
    return {"schema": 1, "suite_id": "product.v4", "split": "dev", "bundle_identity": bundle.identity,
            "production": {}, "environment": {}, "backend": "mock", "backend_identity": {}, "parameters": {},
            "summary": summarize([row]), "rows": [row], "diagnostic_only": True, "quality_evidence": False,
            "exposure": None, "fixed_suite_targets": {"queries": 420, "answerable": 360, "noise": 60},
            "missing": ["mechanical-only fixture"], "index_identity": {}, "role": "candidate", "reference_verified": False,
            "sealed_reference": False}


def test_uppercase_publisher_and_actual_case_punctuation_are_proved_without_relabeling(tmp_path):
    canonical = "THE WITNESS DID NOT BRING THE SIGNED REPORT HOME TODAY"
    observed = "The witness did not bring the signed report home today."
    store, bundle, query, actual = fixture(tmp_path, canonical, observed)
    records = [record(actual, observed)]
    hits, emitted = evidence(store, bundle, actual, records)
    assert emitted and emitted[0]["text"] == canonical
    witness = emitted[0]["word_alignment"]
    assert witness["observed_text"] == observed
    assert witness["observed_sha256"] == hashlib.sha256(observed.encode()).hexdigest()
    assert bundle.views[("clip.flac", "body")]["text"] == canonical
    assert score_query(query, hits, emitted)["all_required_facts@pack"] == 1
    validate_report(bundle, report(bundle, query, hits, emitted, records), diagnostic=True)


@pytest.mark.parametrize("observed", [
    "The witness did bring the signed report home today.",
    "Today home report signed the bring not did witness the.",
    "The witness did not bring a different parcel home today.",
    "证人今天没有把签名报告带回家。",
])
def test_missing_negation_reversal_substitution_and_translation_never_cover_full_fact(tmp_path, observed):
    canonical = "THE WITNESS DID NOT BRING THE SIGNED REPORT HOME TODAY"
    store, bundle, query, actual = fixture(tmp_path, canonical, observed)
    hits, emitted = evidence(store, bundle, actual, [record(actual, observed)])
    assert score_query(query, hits, emitted)["all_required_facts@pack"] == 0


@pytest.mark.parametrize("canonical,observed", [
    ("THE MEASURED LEVEL WAS 2.5 METRES BELOW NORMAL", "The measured level was 25 metres below normal."),
    ("THE MEASURED LEVEL WAS .5 METRES BELOW NORMAL", "The measured level was 5 metres below normal."),
    ("THE MEASURED LEVEL WAS -.5 METRES BELOW NORMAL", "The measured level was -5 metres below normal."),
    ("THE MEASURED LEVEL WAS +.5 METRES BELOW NORMAL", "The measured level was +5 metres below normal."),
    ("THE MEASURED LEVEL WAS ,5 METRES BELOW NORMAL", "The measured level was 5 metres below normal."),
    ("THE MEASURED LEVEL WAS −,5 METRES BELOW NORMAL", "The measured level was −5 metres below normal."),
    ("THE MEASURED LEVEL WAS .5m BELOW NORMAL TODAY", "The measured level was 5m below normal today."),
    ("THE MEASURED LEVEL WAS -.5m BELOW NORMAL TODAY", "The measured level was -5m below normal today."),
    ("THE MEASURED LEVEL WAS 2.5m BELOW NORMAL TODAY", "The measured level was 2 5m below normal today."),
    ("THE MEASURED LEVEL WAS 2.5GHz BELOW NORMAL TODAY", "The measured level was 2 5GHz below normal today."),
    ("THE MEASURED DELAY WAS .5s IN TOTAL TODAY", "The measured delay was 5s in total today."),
    ("THE MEASURED LEVEL WAS 2.5m_value BELOW NORMAL", "The measured level was 2 5m_value below normal."),
    ("THE ELECTRIC CURRENT WAS 5mA IN THE TEST TODAY", "The electric current was 5MA in the test today."),
    ("THE ELECTRIC CURRENT WAS 5 mA IN THE TEST TODAY", "The electric current was 5 MA in the test today."),
    ("THE MEASURED DELAY WAS 5 ms IN THE TEST TODAY", "The measured delay was 5 Ms in the test today."),
    ("THE MACHINE FREQUENCY WAS 5 MHz IN THE TEST TODAY", "The machine frequency was 5 mHz in the test today."),
    ("THE ELECTRIC CURRENT WAS 5\tmA IN THE TEST TODAY", "The electric current was 5\tMA in the test today."),
    ("THE MEASURED RATIO WAS 5 mA/mV IN THE TEST TODAY", "The measured ratio was 5 mA/MV in the test today."),
    ("THE MEASURED RATIO WAS 5 /mA IN THE TEST TODAY", "The measured ratio was 5 /MA in the test today."),
    ("THE SERVICE WAS IMPLEMENTED WITH .NET IN THAT YEAR", "The service was implemented with NET in that year."),
    ("THE CALL USED client._transport FOR THIS REQUEST TODAY", "The call used client_transport for this request today."),
    ("THE CALL USED ._private FOR THIS REQUEST TODAY", "The call used _private for this request today."),
    ("THE EXPECTED CHANGE IS -5 UNITS IN TOTAL", "The expected change is 5 units in total."),
    ("THE EXPECTED CHANGE IS −5 UNITS IN TOTAL", "The expected change is 5 units in total."),
    ("THE RECORDED COUNT WAS 1,000 ITEMS TODAY", "The recorded count was 1000 items today."),
    ("THE MEETING STARTS AT 3:15 IN THE AFTERNOON", "The meeting starts at 315 in the afternoon."),
    ("THE PROPORTION REMAINS BELOW 5% FOR ALL SAMPLES", "The proportion remains below 5 for all samples."),
    ("THE COMPARISON USES X < 5 IN THIS STEP", "The comparison uses x > 5 in this step."),
    ("THE IMPLEMENTATION USES C++ FOR THIS MODULE", "The implementation uses C for this module."),
    ("THE SELECTED NAME IS ITEM_KEY FOR THIS VALUE", "The selected name is ITEMKEY for this value."),
    ("THE WITNESS CAN'T IDENTIFY ANY OF THOSE PEOPLE", "The witness can identify any of those people."),
    ("THE DEVICE IS A WELL-KNOWN MODEL FROM THAT YEAR", "The device is a well known model from that year."),
])
def test_numbers_symbols_internal_apostrophes_and_hyphens_are_not_discarded(tmp_path, canonical, observed):
    store, bundle, query, actual = fixture(tmp_path, canonical, observed)
    hits, emitted = evidence(store, bundle, actual, [record(actual, observed)])
    assert score_query(query, hits, emitted)["all_required_facts@pack"] == 0


def test_asr_keeps_whole_signed_grouped_numeric_tokens_and_symbols():
    values = [token[0] for token in word_tokens("-5 2.5 1,000 3:15 5% C++ x>2 A_B can't well-known")]
    assert values == ["-5", "2.5", "1,000", "3:15", "5", "%", "c", "+", "+", "x", ">", "2", "A", "_", "B", "can't", "well-known"]
    assert not asr_word_spans("ONE TWO THREE", "One two three.")
    assert not asr_word_spans("ALPHA BETA GAMMA DELTA / ALPHA BETA GAMMA DELTA", "Alpha beta gamma delta.")


@pytest.mark.parametrize("canonical,observed", [
    ("THE MEASURED CURRENT WAS 5 mA IN THE TEST TODAY", "The measured current was 5 mA in the test today."),
    ("THE MEASURED RATIO WAS 5 mA/mV IN THE TEST TODAY", "The measured ratio was 5\tmA/mV in the test today."),
    ("THE CURRENT IN THE TEST TODAY WAS 5mA", "The current in the test today was 5mA."),
    ("THE CALL USED client._transport FOR THIS REQUEST TODAY", "The call used client._transport for this request today."),
])
def test_numeric_fields_and_private_identifier_dots_keep_ordinary_asr_case_controls(tmp_path, canonical, observed):
    store, bundle, query, actual = fixture(tmp_path, canonical, observed)
    hits, emitted = evidence(store, bundle, actual, [record(actual, observed)])
    assert score_query(query, hits, emitted)["all_required_facts@pack"] == 1
    validate_report(bundle, report(bundle, query, hits, emitted, [record(actual, observed)]), diagnostic=True)


def test_next_numeric_field_is_raw_without_unit_dictionary_and_bounds_keep_identifier_dots():
    assert [t[0] for t in word_tokens("First 5 mA/mV then 5 /mA and 5 widgets afterward")] == [
        "first", "5", "mA", "/", "mV", "then", "5", "/", "mA", "and", "5", "widgets", "afterward"]
    assert [t[0] for t in word_tokens("First 5 WIDGETS afterward")] == ["first", "5", "WIDGETS", "afterward"]
    for canonical, observed in [("._private FOR THIS REQUEST TODAY", "_private for this request today."),
                                 ("client._transport FOR THIS REQUEST TODAY", "client_transport for this request today.")]:
        spans = asr_word_spans(canonical, observed)
        assert not any(s["start_char"] == 0 and s["end_char"] == len(canonical) for s in spans)
        for span in spans:
            validate_word_alignment({**span, "evidence_type": "asr_text"})


def test_asr_alignment_repetition_and_oversized_inputs_have_fixed_work_bounds():
    from product_eval.alignment import MAX_ALIGNMENT_CHARS
    runs = asr_word_spans("WORD " * 400, "word " * 400)
    # The entire 400-token sequence has one placement; all shorter repeated
    # sequences are ambiguous and must not create extra proofs.
    assert len(runs) == 1 and runs[0]["text"] == "WORD " * 400
    for canonical, observed in [("X" * (MAX_ALIGNMENT_CHARS + 1), "word"),
                                 ("WORD " * 4097, "word"),
                                 ("WORD " * 600, "word " * 600),
                                 ("WORD " * 4096, "word " * 60)]:
        with pytest.raises(EvaluationError, match="bound") as error:
            asr_word_spans(canonical, observed)
        assert error.value.status == "MISSING"


def test_half_fact_two_material_records_and_missing_citation_cannot_be_joined(tmp_path):
    canonical = "THE WITNESS DID NOT BRING THE SIGNED REPORT HOME TODAY"
    observed = "The witness did not bring the signed report home today."
    store, bundle, query, actual = fixture(tmp_path, canonical, observed)
    for records in ([record(actual, "The witness did not bring")],
                    [record(actual, "The witness did not bring", n=1), record(actual, "the signed report home today.", n=2)],
                    [record(actual, observed, cite=False)]):
        hits, emitted = evidence(store, bundle, actual, records)
        assert score_query(query, hits, emitted)["all_required_facts@pack"] == 0


def test_wrong_asr_time_and_pdf_page_do_not_gain_partial_proofs(tmp_path):
    for asr, bad in [(True, {"unit": "second", "start": 20, "end": 30}),
                     (False, {"unit": "page", "start": 2, "end": 2})]:
        canonical = "THE COMPLETE SOURCE FACT HAS MANY EXACT CHARACTERS FOR THIS MECHANICAL FIXTURE"
        observed = canonical.lower() if asr else "different header " + canonical + " different footer"
        store, bundle, query, actual = fixture(tmp_path, canonical, observed, asr=asr, stored_location=bad)
        hits, emitted = evidence(store, bundle, actual, [record(actual, observed)])
        assert not hits[0]["spans"] and not emitted


def test_saved_witness_sha_tokens_source_range_and_material_membership_are_rechecked(tmp_path):
    canonical = "THE WITNESS DID NOT BRING THE SIGNED REPORT HOME TODAY"
    observed = "The witness did not bring the signed report home today."
    store, bundle, query, actual = fixture(tmp_path, canonical, observed)
    records = [record(actual, observed)]
    hits, emitted = evidence(store, bundle, actual, records)
    base = report(bundle, query, hits, emitted, records)
    for mutation in ("sha", "tokens", "source", "body", "citation"):
        bad = copy.deepcopy(base)
        row = bad["rows"][0]
        span = row["emitted_spans"][0]
        if mutation == "sha":
            span["word_alignment"]["observed_sha256"] = "0" * 64
        elif mutation == "tokens":
            witness = span["word_alignment"]
            witness["observed_text"] = witness["observed_text"].replace("not ", "")
            witness["observed_sha256"] = hashlib.sha256(witness["observed_text"].encode()).hexdigest()
        elif mutation == "source":
            span["start_char"] += 1
            span["end_char"] += 1
        elif mutation == "body":
            row["pack_records"][0]["text"] = "An unrelated actual material body."
            row["context_text"] = pack(row["pack_records"]) + "\n# query: " + observed
            row["pack_sha256"] = hashlib.sha256(row["context_text"].encode()).hexdigest()
        else:
            row["pack_records"][0]["segments"] = [{"segment_id": 2}]
            row["pack_records"][0]["segment_id"] = 2
        with pytest.raises(EvaluationError):
            validate_report(bundle, bad, diagnostic=True)
    bad = copy.deepcopy(emitted[0])
    bad["evidence_type"] = "ocr_text"
    with pytest.raises(EvaluationError, match="ASR"):
        validate_word_alignment(bad)


FACT = ("The safety ledger records the copper vessel as sealed before departure, "
        "and the receiving clerk confirms that every numbered package arrived intact.")


def test_unrelated_ocr_typo_preserves_complete_literal_fact_but_inside_typo_does_not(tmp_path):
    canonical = "Printed page heading\n" + FACT + "\nOriginal publication footer"
    for observed, covered in [(canonical.replace("heading", "headlng"), 1),
                              (canonical.replace("copper", "copqer"), 0),
                              ("other header\n" + FACT[:70] + " OCR mismatch " + FACT[70:] + "\nother footer", 0)]:
        store, bundle, query, actual = fixture(tmp_path, canonical, observed, fact=FACT, asr=False)
        records = [record(actual, observed)]
        hits, emitted = evidence(store, bundle, actual, records)
        assert all("word_alignment" not in span for span in emitted)
        assert score_query(query, hits, emitted)["all_required_facts@pack"] == covered
        for span in hits[0]["spans"] + emitted:
            _proof(bundle, span)
        validate_report(bundle, report(bundle, query, hits, emitted, records), diagnostic=True)


def test_short_coincidence_and_non_asr_case_change_cannot_use_relaxed_alignment(tmp_path):
    for canonical, observed, fact in [("First header. small shared words. Last footer", "Other small shared words. Different", "small shared words."),
                                       (FACT, FACT.lower(), FACT)]:
        store, bundle, query, actual = fixture(tmp_path, canonical, observed, fact=fact, asr=False)
        hits, emitted = evidence(store, bundle, actual, [record(actual, observed)])
        assert score_query(query, hits, emitted)["all_required_facts@pack"] == 0
