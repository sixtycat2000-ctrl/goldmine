// Update-only read-only audit. Full history is retained; no models are loaded.
import {createRequire} from "node:module";
import {join} from "node:path";
import {pathToFileURL} from "node:url";
import {readFileSync} from "node:fs";
import {createHash} from "node:crypto";
const [pkg, database, config, model] = process.argv.slice(2);
if (process.argv.length !== 6 || ![pkg,database,config].every(v=>v.startsWith("/")) || !(model === "-" || model.startsWith("/"))) throw Error("fixed absolute update-audit arguments required");
const require = createRequire(join(pkg,"package.json"));
const Database = require("better-sqlite3"), sqliteVec = require("sqlite-vec"), YAML = require("yaml");
const db = new Database(database,{readonly:true,fileMustExist:true});
const sha = bytes=>createHash("sha256").update(bytes).digest("hex");
const hashJson = v=>sha(JSON.stringify(v));
const before = sha(readFileSync(database));
try {
  db.pragma("query_only = ON"); db.exec("BEGIN");
  const tableSpecs = db.prepare("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").all();
  const tables = new Set(tableSpecs.map(r=>r.name));
  const documents = db.prepare("SELECT d.*,c.doc AS body FROM documents d JOIN content c ON c.hash=d.hash ORDER BY d.id").all();
  if (documents.some(r=>r.active!==0 && r.active!==1)) throw Error("invalid active state");
  const active = documents.filter(r=>r.active===1);
  let vectors=[], vectorRows=[];
  if (tables.has("content_vectors")) vectors=db.prepare("SELECT * FROM content_vectors ORDER BY hash,seq").all();
  if (tables.has("vectors_vec")) {
    sqliteVec.load(db);
    vectorRows=db.prepare("SELECT hash_seq,embedding FROM vectors_vec ORDER BY hash_seq").all().map(r=>({hash_seq:r.hash_seq,bytes:r.embedding.length,sha256:sha(r.embedding)}));
  }
  const store=await import(pathToFileURL(join(pkg,"dist/store.js")).href);
  const fts=db.prepare("SELECT rowid,filepath,title,body FROM documents_fts ORDER BY rowid").all();
  const expectedFts=active.map(r=>({rowid:r.id,filepath:store.normalizeCjkForFTS(r.collection+"/"+r.path),title:store.normalizeCjkForFTS(r.title),body:store.normalizeCjkForFTS(r.body)}));
  if (JSON.stringify(fts)!==JSON.stringify(expectedFts)) throw Error("actual FTS differs from active full bodies; tombstones must not be indexed");
  const eligible=tables.has("content_vectors") ? db.prepare("SELECT DISTINCT cv.hash || '_' || cv.seq AS hash_seq FROM content_vectors cv JOIN documents d ON d.hash=cv.hash AND d.active=1 ORDER BY hash_seq").all().map(r=>r.hash_seq) : [];
  const physical={schema:tableSpecs.filter(r=>r.name!=="llm_cache").map(r=>[r.name,r.sql]),tables:{},user_version:db.pragma("user_version",{simple:true})};
  for (const {name,sql} of tableSpecs) {
    if (name==="llm_cache" || (sql||"").toUpperCase().startsWith("CREATE VIRTUAL TABLE")) continue;
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw Error("unexpected native SQL table name");
    const rows=db.prepare(`SELECT * FROM "${name}"`).raw(true).all().map(row=>hashJson(row.map(v=>{
      if (Buffer.isBuffer(v)) return {bytes:v.length,sha256:sha(v)};
      if (typeof v==="number") {
        if (!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v))) throw Error("non-exact SQL numeric value");
        const b=Buffer.alloc(8);b.writeDoubleBE(v);return {number_f64_be:b.toString("hex")};
      }
      return v;
    }))).sort();
    physical.tables[name]={rows:rows.length,sha256:hashJson(rows)};
  }
  const cache=tables.has("llm_cache") ? db.prepare("SELECT hash,result,created_at FROM llm_cache ORDER BY hash").all() : [];
  const configuration=YAML.parse(readFileSync(config,"utf8"));
  if (sha(readFileSync(database))!==before) throw Error("read-only update audit changed SQLite bytes");
  console.log(JSON.stringify({schema:1,scope:"readonly-update-audit",documents,vectors,vector_rows:vectorRows,query_eligible_keys:eligible,
    fts,fts_verified_against_active_full_bodies:true,physical,database_sha256:before,
    fingerprint:model==="-"?null:store.getEmbeddingFingerprint(model),configuration,
    configuration_sha256:sha(readFileSync(config)),cache,cache_sha256:hashJson(cache),
    retained_counts:{active:active.length,inactive:documents.length-active.length,vector_rows:vectorRows.length},
    native_paths:Object.keys(require.cache).sort()}));
  db.exec("ROLLBACK");
} finally {db.close();}
