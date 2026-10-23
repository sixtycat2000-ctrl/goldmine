"""Declared synthetic mechanics only; no real210 query, source, model or index."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from answer_eval.protocol import EvaluationError, canonical_sha256
from product_eval.schema import CLASSES
from search_comparison.measurements.selection import ARMS, VIEWS, KINDS, select_sources, select_requests, schedule, verify_schedule
from search_comparison.measurements.recipes import ordinary_update, query, operations
from search_comparison.measurements.timing import nearest, summary
from search_comparison.measurements.updates import qmd, goldmine, recoll, compare


def requests():
    return [{"id": f"fixture-{c}-{n:02d}", "q": f"synthetic {c} {n} 中文", "primary_class": c, "source_group": f"fixture-group-{n}"} for c in CLASSES for n in range(30)]


def sources():
    return {f"fixture/{kind}-{n}.txt": {"path": f"fixture/{kind}-{n}.txt", "kind": kind} for kind,count in KINDS.items() for n in range(count)}


def observations(slots):
    return [{"ordinal": s["ordinal"], "id": s["id"], "status": "OK", "wall_ms": float(s["ordinal"]),
             "command": {"path": f"/declared-synthetic/{s['ordinal']}/manifest.json", "sha256": hashlib.sha256(str(s["ordinal"]).encode()).hexdigest()},
             "resource_status": "PASS"} for s in slots]


def test_selection_has_no_result_input_and_is_order_independent():
    assert select_requests(requests()) == select_requests(list(reversed(requests())))
    assert select_sources(sources()) == select_sources(dict(reversed(list(sources().items()))))
    assert len(select_requests(requests())["requests"]) == 30
    c=select_sources(sources())
    assert len(c["base112"]) == 112 and len(c["add10"]) == 10
    assert set(c["base112"]).isdisjoint(c["add10"])
    assert set(c["base112"]) | set(c["add10"]) == set(sources())
    assert len({c["remove"],c["replace_target"],c["replace_donor"]}) == 3


@pytest.mark.parametrize("mutation",["omit", "duplicate", "class", "qrel", "nul"])
def test_request_selector_rejects_bad_or_quality_bearing_input(mutation):
    r=requests()
    if mutation=="omit": r.pop()
    elif mutation=="duplicate": r[-1]=r[0]
    elif mutation=="class": r[-1]["primary_class"]=CLASSES[0]
    elif mutation=="qrel": r[0]["score"]=1
    else:r[0]["q"]="unsafe\x00query"
    with pytest.raises(EvaluationError):select_requests(r)


def test_source_selector_refuses_shrunken_or_relabelled_denominator():
    s=sources();s.pop(next(iter(s)))
    with pytest.raises(EvaluationError):select_sources(s)
    s=sources();next(iter(s.values()))["kind"]="audio"
    with pytest.raises(EvaluationError):select_sources(s)


def test_complete_interleaved_schedule_and_prime_links():
    selection=select_requests(requests());slots=schedule(selection)
    assert verify_schedule(selection,slots)["all_slots"] == 1728
    for view in VIEWS:
        for arm in ARMS:
            for regime in ("cold_cli","warm_cli"):
                rows=[s for s in slots if (s["view"],s["arm"],s["regime"])==(view,arm,regime)]
                assert len([s for s in rows if s["role"]=="measured"])==30
                assert len([s for s in rows if s["role"]=="excluded_warmup"])==3
    for s in slots:
        if s["role"]=="measured" and s["regime"]=="warm_cli":
            prime=slots[s["prime_ordinal"]-1]
            assert prime["ordinal"]+1==s["ordinal"] and prime["id"]==s["id"]
            assert prime["role"]=="cache_prime" and not s["restore_pre_query_snapshot"]
    first=[s for s in slots if s["role"]=="measured"][:36]
    assert len({(s["view"],s["arm"]) for s in first})==18


@pytest.mark.parametrize("mutation",["drop", "swap", "duplicate", "trailing-null"])
def test_schedule_cannot_drop_or_reorder_slots(mutation):
    selected=select_requests(requests());slots=schedule(selected)
    if mutation=="drop":slots.pop()
    elif mutation=="swap":slots[0],slots[1]=slots[1],slots[0]
    elif mutation=="duplicate":slots[-1]=slots[0]
    else:slots.append(None)
    with pytest.raises(EvaluationError):verify_schedule(selected,slots)


@pytest.mark.parametrize("values,p,expected",[([1,2,3,4],.5,2),([1,2,3,4],.95,4),(list(range(1,31)),.95,29),([], .5,None)])
def test_nearest_rank_fixed_definition(values,p,expected):
    assert nearest(values,p)==expected


def test_failed_slot_preserved_and_no_success_only_latency_claim():
    selected=select_requests(requests());slots=schedule(selected);rows=observations(slots)
    n=next(i for i,s in enumerate(slots) if s["role"]=="measured" and s["regime"]=="cold_cli")
    rows[n].update(status="TIMEOUT",wall_ms=600000)
    result=summary(selected,slots,rows)
    cell=next(c for c in result["cells"] if (c["view"],c["arm"],c["regime"])==(slots[n]["view"],slots[n]["arm"],slots[n]["regime"]))
    assert cell["n_fixed"]==30 and cell["n_attempted"]==30 and cell["n_success"]==29
    assert cell["complete_service_latency"] is None
    assert cell["all_attempt_duration_including_errors"]["max_ms"]==600000
    assert cell["failed_ordinals"]==[slots[n]["ordinal"]]
    assert result["status"]=="STRUCTURAL_SUMMARY_REQUIRES_AUTHENTICATED_RAW_REPLAY"
    assert result["release_eligible"] is False


@pytest.mark.parametrize("mutation",["trailing-null","omit","wrong-id","no-raw","nan","bad-prime","prefix-resource"])
def test_timing_refuses_incomplete_or_false_success_dispositions(mutation):
    selected=select_requests(requests());slots=schedule(selected);rows=observations(slots)
    if mutation=="trailing-null":rows.append(None)
    elif mutation=="omit":rows.pop()
    elif mutation=="wrong-id":rows[0]["id"]="other"
    elif mutation=="no-raw":rows[0]["command"]=None
    elif mutation=="nan":rows[0]["wall_ms"]=float("nan")
    elif mutation=="bad-prime":
        s=next(s for s in slots if s["role"]=="measured" and s["prime_ordinal"] is not None)
        rows[s["prime_ordinal"]-1]["status"]="FAIL"
    else:
        rows[-1]["resource_status"]="FAIL"
    with pytest.raises(EvaluationError):summary(selected,slots,rows)


def test_missing_or_failed_resource_is_never_upgraded_from_successful_prefix():
    selected=select_requests(requests());slots=schedule(selected);rows=observations(slots)
    rows[-1].update(status="MISSING",wall_ms=None,command=None,resource_status="MISSING")
    assert summary(selected,slots,rows)["resource_status"]=="MISSING"
    rows[0].update(status="FAIL",resource_status="FAIL")
    assert summary(selected,slots,rows)["resource_status"]=="FAIL"


def test_fixed_query_argv_and_ordinary_update_cannot_be_free_hooks():
    values={k:"/fixture/"+k for k in ("python","goldmine_db","goldmine_config","selected_corpus","recollq","recollindex","recoll_config_dir","node","qmd_launcher","qmd_index","qmd_collection")}
    attack="--help 中文 $(touch /never) `literal` 'quoted'"
    for arm in ARMS:
        argv=query(arm,values,attack)
        assert argv[-1]==attack and argv[-2]=="--"
        if arm.startswith("goldmine."):assert "--no-serve" in argv
    assert ordinary_update("qmd.search",values)==[[values["node"],values["qmd_launcher"],"--index",values["qmd_index"],"update"]]
    values.update(qmd_collection="sources")
    for arm in ("qmd.vsearch","qmd.query"):
        assert len(ordinary_update(arm,values))==2 and "embed" in ordinary_update(arm,values)[1]
    with pytest.raises(EvaluationError):ordinary_update("goldmine.any-arbitrary-mode",values)
    with pytest.raises(EvaluationError):query("qmd.query",values,"bad\x00query")


def test_update_operations_preserve_exact_cohort_and_same_index_update_intent():
    c=select_sources(sources());ops=operations(c)
    assert ops[0]["paths"]==c["base112"] and ops[3]["paths"]==c["add10"]
    assert [x["stage"] for x in ops if x["kind"]=="ordinary_update"]==["incremental122","removed121","replaced122"]
    assert all("argv" not in x and "callback" not in x for x in ops)


def qmd_payload():
    h=lambda b:hashlib.sha256(b.encode()).hexdigest()
    docs=[{"id":1,"path":"a.md","collection":"sources","active":1,"hash":h("alpha"),"title":"a","body":"alpha"},
          {"id":2,"path":"b.md","collection":"sources","active":0,"hash":h("beta"),"title":"b","body":"beta"}]
    vectors=[{"hash":d["hash"],"seq":0,"total_chunks":1,"model":"/fixture/model.gguf","embed_fingerprint":"fixture-fp","pos":0} for d in docs]
    rows=[{"hash_seq":v["hash"]+"_0","bytes":4,"sha256":h(v["hash"])} for v in vectors]
    raw={"schema":1,"scope":"readonly-update-audit","documents":docs,"vectors":vectors,"vector_rows":rows,
         "query_eligible_keys":[rows[0]["hash_seq"]],"fts":[{"rowid":1}],"fts_verified_against_active_full_bodies":True,
         "physical":{"fixture_complete_tables":h("all tables incl history")},"fingerprint":"fixture-fp","configuration":{"models":{"embed":"/fixture/model.gguf"}}}
    mapping={d["path"]:{"source_path":"source/"+d["path"],"sha256":d["hash"]} for d in docs}
    return raw,mapping


def goldmine_payload():
    file={"id":9,"path":"/fixture/actual-working-corpus/a.md","indexed_at":1,"mtime":1,
          "source":"file","kind":"doc","size":5,"sha256":hashlib.sha256(b"alpha").hexdigest(),"state":"indexed","capability":"text","detail":None,"info":None,
          "extractor_version":"fixture-text-v1","chunker_version":"fixture-chunks-v1","embed_id":"fixture-fp","content_date":None}
    part={"id":10,"kind":"text","modality":"text","loc":1,"loc_end":1,"text":"alpha","vector":{"bytes":4,"sha256":hashlib.sha256(b"fixture-f32").hexdigest()}}
    stable={"schema":6,"meta":{"fingerprint_json":"fixture-fingerprint"},"sources":{"a.md":{"file":file,"parts":[part],"status":"OBSERVED_INDEXED_TEXT"}}}
    return {"stable":stable,"stable_sha256":canonical_sha256(stable)}


def test_goldmine_physical_ids_timestamps_change_without_losing_source_body_vector_identity():
    a=goldmine_payload();b=deepcopy(a)
    b["stable"]["sources"]["a.md"]["file"].update(id=100,indexed_at=2,mtime=2)
    b["stable"]["sources"]["a.md"]["parts"][0]["id"]=200
    b["stable_sha256"]=canonical_sha256(b["stable"])
    assert compare(goldmine(a),goldmine(b))["status"]=="LOGICAL_EQUAL"


@pytest.mark.parametrize("mutation",["text","loc","vector","duplicate","bad-raw-digest"])
def test_goldmine_update_differences_or_incomplete_identity_are_not_erased(mutation):
    a=goldmine_payload();b=deepcopy(a);p=b["stable"]["sources"]["a.md"]["parts"][0]
    if mutation=="text":p["text"]="different"
    elif mutation=="loc":p["loc_end"]=2
    elif mutation=="vector":p["vector"]["sha256"]=hashlib.sha256(b"different vector").hexdigest()
    elif mutation=="duplicate":b["stable"]["sources"]["a.md"]["parts"].append(deepcopy(p))
    if mutation!="bad-raw-digest":b["stable_sha256"]=canonical_sha256(b["stable"])
    else:b["stable_sha256"]=hashlib.sha256(b"forged declaration").hexdigest()
    if mutation in ("duplicate","bad-raw-digest"):
        with pytest.raises(EvaluationError):goldmine(b)
    else:
        diff=compare(goldmine(a),goldmine(b))
        assert diff["status"]=="LOGICAL_DIFFERENCES_OBSERVED"
        assert any(diff["counts"].values())


def with_info(info, *, detail=None):
    audit=goldmine_payload()
    audit["stable"]["sources"]["a.md"]["file"].update(info=info,detail=detail)
    audit["stable_sha256"]=canonical_sha256(audit["stable"])
    return audit


@pytest.mark.parametrize("left,right",[
    ({"exif_reader":"imageio","exif_status":"absent"},{"exif_reader":None,"exif_status":"unavailable"}),
    ({"exif_version":"exif-original.v1"},{"exif_version":"other-reader"}),
    ({"taken":"2020-01-02 03:04:05","taken_source":"Exif.DateTimeOriginal"},{"taken":"2020-01-02 03:04:05","taken_source":"IFD0.DateTime"}),
    ({"ocr_status":"complete","ocr_chars":5},{"ocr_status":"error","ocr_chars":5}),
    ({"ocr_reason":"low confidence"},{"ocr_reason":"helper unavailable"}),
    ({"asr_status":"complete","asr_segments":2},{"asr_status":"low_yield","asr_segments":2}),
    ({"duration":30.0,"read_end":30.0,"language":"zh"},{"duration":60.0,"read_end":30.0,"language":"zh"}),
    ({"asr_dropped":0},{"asr_dropped":1}),
    ({"ocr_page_states":{"1":"complete","2":"unavailable"}},{"ocr_page_states":{"1":"complete","2":"complete"}}),
    ({"future":{"provenance":[{"reader":"A"}]}},{"future":{"provenance":[{"reader":"B"}]}}),
    ({"future":{"ocr_ms":1}},{"future":{"ocr_ms":2}}),
    ({"unknown_elapsed_ms":1},{"unknown_elapsed_ms":2}),
    ({"runtime":{"pid":1}},{"runtime":{"pid":2}}),
    ({"evidence_types":["ocr_text"]},{"evidence_types":["metadata"]}),
])
def test_stable_and_unknown_info_mutations_are_never_logically_equal(left,right):
    a=with_info(json.dumps(left));b=with_info(json.dumps(right))
    assert a["stable_sha256"]!=b["stable_sha256"]
    diff=compare(goldmine(a),goldmine(b))
    assert diff["status"]=="LOGICAL_DIFFERENCES_OBSERVED"
    assert diff["changes"]["metadata"]==["a.md"]
    assert diff["counts"]["body"]==0 and diff["counts"]["vectors"]==0


@pytest.mark.parametrize("raw",['{','', '[]','null','true','1','"text"',
    '{"exif_status":"absent","exif_status":"error"}',
    '{"nested":{"k":1,"k":2}}','{"ocr_ms":1,"ocr_ms":2}','{"value":NaN}','{"value":Infinity}','{"value":1e400}',
    '{"text":"\\ud800"}', {"not":"SQL TEXT"}])
def test_info_json_is_strict_and_nonobject_or_invalid_records_reject(raw):
    with pytest.raises(EvaluationError):goldmine(with_info(raw))


@pytest.mark.parametrize("metric",[None,True,"1",-1,float("nan"),float("inf"),10**400])
def test_invalid_excluded_metric_cannot_hide_bad_info(metric):
    with pytest.raises(EvaluationError):goldmine(with_info(json.dumps({"ocr_ms":metric})))


def test_exact_top_level_volatile_only_difference_is_equal_and_raw_values_retained():
    a=with_info('{"ocr_status":"complete","ocr_chars":5,"ocr_ms":1.2}')
    b=with_info('{"ocr_ms":2.7,"ocr_chars":5,"ocr_status":"complete"}')
    left,right=goldmine(a),goldmine(b)
    assert left["raw_identity"]!=right["raw_identity"]
    assert compare(left,right)["status"]=="LOGICAL_EQUAL"
    assert left["excluded_info_values_retained"]=={"a.md":{"ocr_ms":1.2}}
    assert right["excluded_info_values_retained"]=={"a.md":{"ocr_ms":2.7}}
    assert left["sources"]["a.md"]["metadata"]["info"]=={"ocr_status":"complete","ocr_chars":5}


def test_MEASURE_INFO_01_full_semantic_exif_counterexample_is_closed():
    info={"screenshot":True,"exif_version":"exif-original.v1","exif_reader":"imageio","exif_status":"absent",
          "taken":None,"ocr_status":"complete","ocr_chars":5,"ocr_ms":1.2}
    a=with_info(json.dumps(info))
    info.update(exif_reader=None,exif_status="unavailable")
    b=with_info(json.dumps(info))
    diff=compare(goldmine(a),goldmine(b))
    assert diff["status"]=="LOGICAL_DIFFERENCES_OBSERVED"
    assert diff["counts"]=={"metadata":1,"body":0,"vectors":0,"capability":0,"added":0,"removed":0}


def test_info_formatting_only_changes_and_null_semantics_are_explicit():
    a=with_info('{"nested":{"a":1,"b":2}}')
    b=with_info('{ "nested" : {"b":2, "a":1} }')
    assert compare(goldmine(a),goldmine(b))["status"]=="LOGICAL_EQUAL"
    assert goldmine(with_info(None))["sources"]["a.md"]["metadata"]["info"] is None
    assert compare(goldmine(with_info(None)),goldmine(with_info('{}')))["status"]=="LOGICAL_DIFFERENCES_OBSERVED"


def test_semantic_detail_and_info_policy_cannot_disappear():
    a=goldmine(with_info(None,detail="OCR unavailable"));b=goldmine(with_info(None,detail="OCR decode error"))
    assert compare(a,b)["changes"]["metadata"]==["a.md"]
    del b["info_policy"]
    with pytest.raises(EvaluationError):compare(a,b)
    with pytest.raises(EvaluationError):goldmine(with_info(None,detail=42))


def test_info_oversize_or_recursion_fails_explicitly_without_field_truncation():
    with pytest.raises(EvaluationError):goldmine(with_info(json.dumps({"unbounded":"x"*(1024*1024)})))
    raw='{"nested":'*1100+'{}'+'}'*1100
    with pytest.raises(EvaluationError):goldmine(with_info(raw))


def test_qmd_history_is_retained_without_inflating_active_membership():
    raw,mapping=qmd_payload();actual=qmd(raw,mapping,{"a.md"},semantic=True)
    assert set(actual["sources"])=={"source/a.md"}
    assert len(actual["retained_history"]["inactive_documents"])==1
    assert len(actual["retained_history"]["noneligible_vector_rows"])==1
    assert actual["active_membership"]=={"missing":[],"stale_active":[]}
    fresh=deepcopy(raw);fresh["documents"]=fresh["documents"][:1];fresh["vectors"]=fresh["vectors"][:1];fresh["vector_rows"]=fresh["vector_rows"][:1]
    expected=qmd(fresh,mapping,{"a.md"},semantic=True)
    assert compare(actual,expected)["status"]=="LOGICAL_EQUAL"
    assert actual["history_sha256"]!=expected["history_sha256"]


def test_qmd_actual_stale_active_row_and_vector_change_are_reported():
    raw,mapping=qmd_payload();raw["documents"][1]["active"]=1;raw["fts"].append({"rowid":2});raw["query_eligible_keys"].append(raw["vector_rows"][1]["hash_seq"])
    a=qmd(raw,mapping,{"a.md"},semantic=True)
    assert a["active_membership"]["stale_active"]==["b.md"]
    b=deepcopy(a);b["sources"]["source/a.md"]["vectors"][0]["vector"]["sha256"]=hashlib.sha256(b"changed real-byte fixture").hexdigest()
    diff=compare(a,b)
    assert diff["counts"]["vectors"]==1 and diff["status"]=="LOGICAL_DIFFERENCES_OBSERVED"


def test_qmd_unsupported_native_sources_stay_in_source_count_without_false_missing_text():
    raw,mapping=qmd_payload()
    mapping["photo.jpg"]={"source_path":"source/photo.jpg","sha256":hashlib.sha256(b"opaque synthetic image bytes").hexdigest()}
    a=qmd(raw,mapping,{"a.md","photo.jpg"},semantic=True,view="native")
    assert a["source_count_expected"]==2 and len(a["sources"])==2
    assert a["unsupported_native"]==["photo.jpg"] and a["active_membership"]["missing"]==[]
    assert a["sources"]["source/photo.jpg"]["capability"]=="UNSUPPORTED_NATIVE_FORMAT"
    assert a["sources"]["source/photo.jpg"]["body"] is None


def test_recoll_deleted_active_membership_and_subdocument_body_differences_retained():
    known={"a.md":{"sha256":hashlib.sha256(b"alpha").hexdigest()},"b.pdf":{"sha256":hashlib.sha256(b"opaque pdf fixture").hexdigest()}}
    rows=[{"source_path":"a.md","xdocid":"1","ordinal":0,"ipath":"","mtype":"text/plain","dbytes":"5"},
          {"source_path":"b.pdf","xdocid":"2","ordinal":1,"ipath":"page:1","mtype":"application/pdf","dbytes":"4"}]
    a=recoll(rows,[b"alpha",b"beta"],known,{"a.md"})
    assert a["active_membership"]["stale_active"]==["b.pdf"]
    changed=deepcopy(rows);changed[0]["xdocid"]="100"
    assert compare(a,recoll(changed,[b"alpha",b"beta"],known,{"a.md"}))["status"]=="LOGICAL_EQUAL"
    changed[1]["ipath"]="page:2"
    assert compare(a,recoll(changed,[b"alpha",b"beta"],known,{"a.md"}))["counts"]["body"]==1
    with pytest.raises(EvaluationError):recoll(rows,[b"alpha"],known,{"a.md"})


@pytest.mark.parametrize("mutation",["eligible-inactive","missing-vector","bad-body","unknown-inactive","fts-tombstone","missing-fingerprint","alias-source","wrong-model"])
def test_qmd_update_proof_denials(mutation):
    raw,mapping=qmd_payload()
    if mutation=="eligible-inactive":raw["query_eligible_keys"].append(raw["vector_rows"][1]["hash_seq"])
    elif mutation=="missing-vector":raw["vector_rows"].pop(0)
    elif mutation=="bad-body":raw["documents"][0]["body"]="different"
    elif mutation=="unknown-inactive":raw["documents"][1]["path"]="outside.md"
    elif mutation=="fts-tombstone":raw["fts"].append({"rowid":2})
    elif mutation=="missing-fingerprint":raw["fingerprint"]=None
    elif mutation=="wrong-model":raw["vectors"][0]["model"]="/some/other-model.gguf"
    else:mapping["b.md"]["source_path"]=mapping["a.md"]["source_path"]
    with pytest.raises(EvaluationError):qmd(raw,mapping,{"a.md"},semantic=True)


@pytest.mark.native
@pytest.mark.parametrize("include_inactive_fts",[False,True])
def test_update_worker_reads_controlled_history_and_refuses_fts_tombstone(tmp_path,include_inactive_fts):
    from comparison_measurement_nodefixtures import controlled_package
    from acceptance_lib.evidence import capture_command, read_command_run
    raw,_=qmd_payload()
    raw["fts"]=[{"rowid":1,"filepath":"sources/a.md","title":"a","body":"alpha"}]
    if include_inactive_fts:raw["fts"].append({"rowid":2,"filepath":"sources/b.md","title":"b","body":"beta"})
    pkg,database,config=controlled_package(tmp_path,raw)
    node=shutil.which("node")
    if not node:pytest.fail("mechanical worker test requires explicitly provisioned Node; no engine fallback")
    node=str(Path(node).resolve())
    worker=Path(__file__).resolve().parents[1]/"scripts/search_comparison/measurements/_qmd_update_audit.mjs"
    before=database.read_bytes()
    owner=tmp_path/"raw";owner.parent.mkdir(exist_ok=True)
    capture_command(owner,"fixture.readonly-update-audit",[node,str(worker),str(pkg),str(database),str(config),"/declared-synthetic/model.gguf"],
                    cwd=tmp_path,env={"PATH":"/usr/bin:/bin"},identity={"evidence_mode":"fixture","scope":"controlled JS dependency stubs; no real QMD/SQLite/model"},
                    suite_id="comparison.measurement-fixture/v1",timeout=5,owned_process_group=True)
    _,receipt,stdout=read_command_run(owner/"manifest.json")
    assert database.read_bytes()==before
    if include_inactive_fts:
        assert receipt["exit_code"] != 0 and b"tombstones" in (owner/"stderr.log").read_bytes()
    else:
        assert receipt["exit_code"]==0
        result=json.loads(stdout.read_text())
        assert result["retained_counts"]=={"active":1,"inactive":1,"vector_rows":2}
        assert len(result["documents"])==2 and len(result["fts"])==1
        assert result["query_eligible_keys"]==raw["query_eligible_keys"]
        assert result["cache"]==[]
