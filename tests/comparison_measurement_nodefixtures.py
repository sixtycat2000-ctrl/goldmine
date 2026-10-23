"""Tiny controlled JS dependency stubs, explicitly not QMD/native SQLite proof."""
import json
from pathlib import Path


DATABASE_MODULE = r'''
const fs=require("fs");
module.exports=class {
  constructor(file,opts) {
    if(!opts.readonly||!opts.fileMustExist) throw Error("must be readonly");
    this.data=JSON.parse(fs.readFileSync(file+".json","utf8"));
  }
  pragma(s) { if(s==="query_only = ON") return; if(s==="user_version") return 0; throw Error("unknown pragma"); }
  exec(s) { if(!["BEGIN","ROLLBACK"].includes(s)) throw Error("write denied"); }
  close() {}
  prepare(s) {
    let rows;
    if(s.includes("FROM sqlite_master")) rows=this.data.tables;
    else if(s.startsWith("SELECT d.*")) rows=this.data.documents;
    else if(s.startsWith("SELECT * FROM content_vectors")) rows=this.data.vectors;
    else if(s.startsWith("SELECT hash_seq,embedding")) rows=this.data.vector_rows.map(r=>({hash_seq:r.hash_seq,embedding:Buffer.from(r.hex,"hex")}));
    else if(s.startsWith("SELECT rowid,filepath")) rows=this.data.fts;
    else if(s.startsWith("SELECT DISTINCT cv.hash")) rows=this.data.query_eligible_keys.map(hash_seq=>({hash_seq}));
    else if(s.startsWith("SELECT hash,result,created_at")) rows=this.data.cache;
    else if(/^SELECT \* FROM "[a-z_]+"$/.test(s)) rows=this.data.raw_tables[s.slice(15,-1)];
    else throw Error("unknown SQL: "+s);
    return {all:()=>rows,raw:()=>({all:()=>rows})};
  }
};
'''


def controlled_package(root, payload):
    pkg=root/"controlled-qmd-stub"
    pkg.mkdir()
    (pkg/"package.json").write_text('{"type":"module"}')
    for name,code in [("better-sqlite3",DATABASE_MODULE),("sqlite-vec","module.exports={load:()=>{}}"),("yaml","module.exports={parse:JSON.parse}")]:
        folder=pkg/"node_modules"/name;folder.mkdir(parents=True)
        (folder/"index.js").write_text(code)
        (folder/"package.json").write_text(json.dumps({"main":"index.js"}))
    (pkg/"dist").mkdir()
    (pkg/"dist/store.js").write_text('export const normalizeCjkForFTS=s=>s; export const getEmbeddingFingerprint=p=>"fixture-fp";')
    table_names=["documents","content","content_vectors","documents_fts","vectors_vec","llm_cache"]
    payload["tables"]=[{"name":n,"sql":("CREATE VIRTUAL TABLE " if n in ("documents_fts","vectors_vec") else "CREATE TABLE ")+n} for n in table_names]
    payload["raw_tables"]={n:[["fixture-complete-table",n]] for n in table_names}
    payload["cache"]=[]
    for row in payload["vector_rows"]:row["hex"]="0000803f"
    database=root/"synthetic-closed-index.sqlite"
    database.write_bytes(b"DECLARED SYNTHETIC SQLITE DEPENDENCY STUB; NOT A REAL ENGINE INDEX\n")
    (root/(database.name+".json")).write_text(json.dumps(payload))
    config=root/"config.json";config.write_text(json.dumps({"models":{},"collections":{}}))
    return pkg,database,config
