// Installed LlamaCpp model-load methods, with explicit local GGUF paths.
// Non-evaluation compatibility only. No prompts, retrieval, query or downloader.
import {pathToFileURL} from "node:url";
import {join} from "node:path";
const [pkg, modelJson, cache] = process.argv.slice(2);
const models = JSON.parse(modelJson);
const roles = Object.keys(models);
if (process.argv.length !== 5 || ![pkg, cache,...Object.values(models)].every(v => v?.startsWith("/")) ||
    !["embed,generate","embed,generate,rerank"].includes(roles.join(","))) throw Error("fixed absolute canary arguments required");
const {LlamaCpp, canWriteLlamaDir} = await import(pathToFileURL(join(pkg,"dist/llm.js")).href);
if (canWriteLlamaDir()) throw Error("automatic native build/download is possible");
const llm = new LlamaCpp({embedModel:models.embed,generateModel:models.generate,rerankModel:models.rerank,modelCacheDir:cache,inactivityTimeoutMs:0});
try {
  for (const role of roles) {
    const method = {embed:"ensureEmbedModel",generate:"ensureGenerateModel",rerank:"ensureRerankModel"}[role];
    if (!await llm[method]()) throw Error("actual model role did not load");
  }
  console.log(JSON.stringify({schema:1,scope:"compatibility_only",llama_directory_writable:false,
    models:Object.fromEntries(roles.map(role=>[role,{path:models[role],loaded:true}]))}));
} finally { await llm.dispose(); }
