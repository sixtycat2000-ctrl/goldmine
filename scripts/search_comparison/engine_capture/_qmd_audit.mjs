// Exact Node-only readonly SQLite audit; no createStore, native LLM, or model load.
import {createRequire} from "node:module";
import {join} from "node:path";
import {pathToFileURL} from "node:url";
import {readFileSync} from "node:fs";
import {createHash} from "node:crypto";
const [pkg, database, config, model] = process.argv.slice(2);
if (process.argv.length !== 6 || ![pkg,database,config].every(v=>v.startsWith("/")) || !(model === "-" || model.startsWith("/"))) throw Error("fixed absolute audit arguments required");
const require = createRequire(join(pkg,"package.json"));
const Database = require("better-sqlite3");
const sqliteVec = require("sqlite-vec");
const YAML = require("yaml");
const db = new Database(database,{readonly:true,fileMustExist:true});
const bytesBefore = createHash("sha256").update(readFileSync(database)).digest("hex");
try {
  db.pragma("query_only = ON"); db.exec("BEGIN");
  const tables = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r=>r.name));
  const documents = db.prepare("SELECT d.*,c.doc AS body FROM documents d JOIN content c ON c.hash=d.hash ORDER BY d.id").all();
  let vectors = [], vectorRows = [], fingerprint = null;
  if (tables.has("content_vectors")) vectors = db.prepare("SELECT * FROM content_vectors ORDER BY hash,seq").all();
  if (tables.has("vectors_vec")) {
    sqliteVec.load(db);
    vectorRows = db.prepare("SELECT hash_seq,embedding FROM vectors_vec ORDER BY hash_seq").all().map(r=>({hash_seq:r.hash_seq,bytes:r.embedding.length,sha256:createHash("sha256").update(r.embedding).digest("hex")}));
  }
  const store = await import(pathToFileURL(join(pkg,"dist/store.js")).href);
  fingerprint = model === "-" ? null : store.getEmbeddingFingerprint(model);
  const fts = db.prepare("SELECT rowid,filepath,title,body FROM documents_fts ORDER BY rowid").all();
  const expectedFts = documents.map(r=>({rowid:r.id,filepath:store.normalizeCjkForFTS(r.collection+"/"+r.path),title:store.normalizeCjkForFTS(r.title),body:store.normalizeCjkForFTS(r.body)}));
  if (JSON.stringify(fts)!==JSON.stringify(expectedFts)) throw Error("actual FTS rows differ from full prepared bodies");
  const ftsTables = {};
  for (const name of ["documents_fts_data","documents_fts_idx","documents_fts_content","documents_fts_docsize","documents_fts_config"]) {
    const rows = db.prepare(`SELECT * FROM ${name}`).all().map(r=>JSON.stringify(r)).sort();
    ftsTables[name] = {rows:rows.length,sha256:createHash("sha256").update(JSON.stringify(rows)).digest("hex")};
  }
  const collections = db.prepare("SELECT * FROM store_collections ORDER BY name").all();
  const tableSpecs = db.prepare("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").all();
  const hashJson = v=>createHash("sha256").update(JSON.stringify(v)).digest("hex");
  const physical = {schema:tableSpecs.filter(r=>r.name!=="llm_cache").map(r=>[r.name,r.sql]),tables:{},user_version:db.pragma("user_version",{simple:true})};
  for (const {name,sql} of tableSpecs) {
    if (name === "llm_cache" || (sql || "").toUpperCase().startsWith("CREATE VIRTUAL TABLE")) continue;
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw Error("unexpected native SQLite table name");
    const rows = db.prepare(`SELECT * FROM "${name}"`).raw(true).all().map(row=>hashJson(row.map(v=>{
      if (Buffer.isBuffer(v)) return {bytes:v.length,sha256:createHash("sha256").update(v).digest("hex")};
      if (typeof v === "number") {
        if (!Number.isFinite(v) || (Number.isInteger(v) && !Number.isSafeInteger(v))) throw Error("unsupported non-exact SQL numeric value");
        const b = Buffer.alloc(8); b.writeDoubleBE(v); return {number_f64_be:b.toString("hex")};
      }
      return v;
    }))).sort();
    physical.tables[name] = {rows:rows.length,sha256:hashJson(rows)};
  }
  if (createHash("sha256").update(readFileSync(database)).digest("hex")!==bytesBefore) throw Error("read-only index audit changed bytes");
  console.log(JSON.stringify({schema:1,documents,vectors,vector_rows:vectorRows,has_vector_table:tables.has("vectors_vec"),fingerprint,
    physical,database_sha256:bytesBefore,configuration_sha256:createHash("sha256").update(readFileSync(config)).digest("hex"),fts,fts_tables:ftsTables,collections,configuration:YAML.parse(readFileSync(config,"utf8")),native_paths:Object.keys(require.cache).sort()}));
  db.exec("ROLLBACK");
} finally { db.close(); }
