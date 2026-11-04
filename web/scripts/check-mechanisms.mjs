#!/usr/bin/env node
/* Analytic fixtures for the instructional model, not retrieval-quality evidence. */
import assert from "node:assert/strict";
import { readFile, access, mkdtemp, writeFile, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import {
  INITIAL_LEASE, INITIAL_SNAPSHOT, INSPECTION_SECONDS, MODE_ORDER,
  inspectionFrame, leaseStep, nextInspectionTime, normalizeScores, postingScenario, pruningScenario,
  scoreScenario, snapshotStep, snapshotView,
} from "../src/learn/mechanism-model.ts";
import { parseLearnHash } from "../src/lib/learn-data.ts";

const root = new URL("../../", import.meta.url);
const checks = [];
const close = (actual, expected, tolerance = 1e-12) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`);
const check = async (name, run) => { await run(); checks.push(name); };

await check("ideal cosine includes opposite directions", () => {
  close(scoreScenario(0).target, 1); close(scoreScenario(90).target, 0); close(scoreScenario(180).target, -1);
});
await check("analytic one-of26 construction has z=5 and z=-5", () => {
  close(scoreScenario(0).z[0], 5); close(scoreScenario(180).z[0], -5);
  close(scoreScenario(0).globalMean, 1 / 26); close(scoreScenario(0).spread, 5 / 26);
});
await check("constant corpus stays finite under the spread floor", () => {
  const result = normalizeScores(Array(26).fill(0.8), Array(26).fill("text"));
  assert.equal(result.denominator, 0.02); result.z.forEach((z) => close(z, 0));
});
await check("spread floor limits the one-outlier construction", () => {
  const result = normalizeScores([0.01, ...Array(25).fill(0)], Array(26).fill("text"));
  assert.equal(result.denominator, 0.02); close(result.z[0], (0.01 * 25 / 26) / 0.02);
  assert.ok(result.z[0] < 5);
});
await check("small modality uses global mean; global scaling is shared", () => {
  const scores = [...Array(20).fill(0), ...Array(6).fill(1)];
  const modalities = [...Array(20).fill("text"), ...Array(6).fill("image")];
  const result = normalizeScores(scores, modalities);
  close(result.modalityMeans.text, 0); close(result.modalityMeans.image, 6 / 26);
  close(result.z[0], -Math.sqrt(6 / 20)); close(result.z[25], Math.sqrt(20 / 6));
});
await check("mismatched score/modality rows are rejected", () => {
  assert.throws(() => normalizeScores([1, 0], ["text"]), /unaligned/);
});
await check("corpus changes z without changing target cosine", () => {
  const quiet = scoreScenario(0, "quiet"), crowded = scoreScenario(0, "crowded");
  assert.equal(quiet.target, crowded.target); assert.ok(crowded.z[0] < quiet.z[0]);
});
await check("pruned toy objects retain low-z lexical evidence and final output", () => {
  const result = pruningScenario(true);
  assert.deepEqual(result.kept, [1, 3]); assert.deepEqual(result.original, [1, 3]);
  assert.deepEqual(result.after, result.original); assert.ok(result.maxima.get(3) < 3);
  assert.equal(result.vectorRows, 26); assert.equal(result.fullObjects, 7); assert.equal(result.reducedObjects, 2);
});
await check("removing lexical route changes only that toy candidate", () => {
  const result = pruningScenario(false);
  assert.deepEqual(result.kept, [1]); assert.deepEqual(result.after, result.original);
  assert.equal(result.vectorRows, 26);
});
await check("empty intersection saves one lookup without changing output", () => {
  const empty = postingScenario(0), nonempty = postingScenario(4);
  assert.deepEqual(empty.intersection, []); assert.equal(empty.oldLookups, 1); assert.equal(empty.newLookups, 0);
  assert.deepEqual(nonempty.intersection, [2, 4]); assert.equal(nonempty.newLookups, 1);
});
await check("publication cannot split current rank/evidence/citation versions", () => {
  const state = snapshotStep(INITIAL_SNAPSHOT, "publish");
  const view = snapshotView(state);
  assert.equal(view.published, 2); assert.equal(view.rankVersion, 1);
  assert.equal(view.evidenceVersion, 1); assert.equal(view.citationVersion, 1);
});
await check("SQLite path stays pinned after maintenance until the next Begin", () => {
  const state = snapshotStep(snapshotStep(INITIAL_SNAPSHOT, "publish"), "begin");
  assert.equal(snapshotView(state).cacheValid, false);
  const maintained = snapshotStep(state, "maintain");
  assert.equal(snapshotView(maintained).requestPath, "SQLite");
  assert.equal(snapshotView(maintained).loadedCache, null);
  const next = snapshotStep(maintained, "begin");
  assert.equal(snapshotView(next).cacheValid, true); assert.equal(snapshotView(next).loadedCache, 2);
  const disabled = snapshotStep(next, "cache-toggle");
  assert.equal(snapshotView(disabled).requestPath, "mmap");
  assert.equal(snapshotView(snapshotStep(disabled, "begin")).requestPath, "SQLite");
});
await check("loaded v1 mmap survives v2 publication and cache rebuild", () => {
  const published = snapshotStep(INITIAL_SNAPSHOT, "publish");
  const maintained = snapshotStep(published, "maintain");
  assert.equal(maintained.cache, 2); assert.equal(maintained.published, 2);
  assert.equal(snapshotView(maintained).requestPath, "mmap"); assert.equal(snapshotView(maintained).loadedCache, 1);
  const next = snapshotStep(maintained, "begin");
  assert.equal(snapshotView(next).request, 2); assert.equal(snapshotView(next).loadedCache, 2);
});
await check("a lease is lazy until use", () => {
  const acquired = leaseStep(INITIAL_LEASE, "acquire");
  assert.equal(acquired.active, 1); assert.equal(acquired.resident, false);
  assert.equal(leaseStep(acquired, "use").resident, true);
});
await check("only last release starts the300-second idle period", () => {
  let state = leaseStep(leaseStep(INITIAL_LEASE, "acquire"), "acquire");
  state = leaseStep(state, "use"); state = leaseStep(state, "release");
  assert.equal(state.active, 1); assert.equal(state.idleAt, null);
  state = leaseStep(state, "release"); assert.equal(state.idleAt, 300);
  state = leaseStep(state, "advance"); assert.equal(state.resident, true);
  state = leaseStep(state, "advance"); assert.equal(state.resident, false);
  assert.equal(state.event, "unloaded");
});
await check("never-used idle expiry does not claim a model was unloaded", () => {
  let state = leaseStep(leaseStep(INITIAL_LEASE, "acquire"), "release");
  state = leaseStep(leaseStep(state, "advance"), "advance");
  assert.equal(state.resident, false); assert.equal(state.event, "idle-expired");
});
await check("failed new request releases its own newly acquired lease", () => {
  let state = leaseStep(leaseStep(INITIAL_LEASE, "acquire"), "use");
  const failed = leaseStep(state, "fail");
  assert.equal(failed.active, state.active); assert.equal(failed.acquired, state.acquired + 1);
  assert.equal(failed.released, state.released + 1); assert.equal(failed.resident, true);
});
await check("reacquisition keeps model alive after a stale callback", () => {
  let state = leaseStep(leaseStep(leaseStep(INITIAL_LEASE, "acquire"), "use"), "release");
  state = leaseStep(state, "advance"); state = leaseStep(state, "acquire");
  const after = leaseStep(state, "stale");
  assert.equal(after.active, 1); assert.equal(after.resident, true); assert.equal(after.idleAt, null);
  assert.equal(after.acquired - after.released, after.active);
});
await check("manual steps and animation share causal inspection phases", () => {
  const context = { optimized: true, candidates: 0, retained: 2, total: 7 };
  for (const mode of MODE_ORDER) {
    for (let stage = 0; stage < 4; stage++) {
      const frame = inspectionFrame(mode, (stage + 0.5) * INSPECTION_SECONDS, context);
      assert.equal(frame.index, stage); assert.ok(frame.label[0] && frame.label[1]);
      assert.ok(Number.isFinite(frame.x));
    }
  }
  const skipped = inspectionFrame("postings", 2.5 * INSPECTION_SECONDS, context);
  assert.equal(skipped.skipped, true); assert.equal(skipped.x, 0);
});
await check("12 manual phase steps cannot strand at a floating boundary", () => {
  const context = { optimized: true, candidates: 0, retained: 2, total: 7 };
  for (const mode of ["scores", "pruning", "postings"]) {
    let phase = 0; const observed = [];
    for (let i = 0; i < 12; i++) {
      phase = inspectionFrame(mode, nextInspectionTime(phase), context).index;
      observed.push(phase);
    }
    assert.deepEqual(observed, [1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3, 0]);
  }
});
await check("lesson graph remains reachable, bilingual and source-linked", async () => {
  const graph = JSON.parse(await readFile(new URL("site/content/graph.baked.json", root), "utf8"));
  const ids = new Set(graph.nodes.map((n) => n.id));
  assert.equal(graph.nodes.length, 150); assert.equal(ids.size, 150);
  for (const id of ["exact-candidate-pruning", "empty-posting-intersection", "read-snapshot", "vector-cache", "reranker-lease"]) {
    const concept = graph.nodes.find((n) => n.id === id);
    for (const key of ["why_zh", "why_en", "how_zh", "how_en", "try_zh", "try_en"]) assert.ok(concept[key]?.trim(), `${id}: ${key}`);
    for (const ref of concept.refs) await access(new URL(ref.path, root));
  }
  for (const n of graph.nodes) for (const link of n.deep) assert.ok(ids.has(link.to), `${n.id}->${link.to}`);
});
await check("static fallback contains bilingual why/how/try explanation blocks", async () => {
  const html = await readFile(new URL("web/learn.html", root), "utf8");
  for (const label of ["为什么", "怎么做", "动手验证", "Why", "How", "Try it"]) assert.ok(html.includes(`<span class="lbl">${label}</span>`));
  assert.ok(!html.includes("{{GRAPH_JSON}}"));
});
await check("metadata and boot counts derive from the actual baked graph", async () => {
  const graph = JSON.parse(await readFile(new URL("site/content/graph.baked.json", root), "utf8"));
  const count = String(graph.nodes.length);
  for (const name of ["site/learn.html", "web/learn.html"]) {
    const html = await readFile(new URL(name, root), "utf8");
    const descriptions = [...html.matchAll(/<meta[^>]+(?:name|property)="(?:description|og:description)"[^>]+content="([^"]*)"/g)].map((m) => m[1]);
    assert.ok(descriptions.length >= 2);
    descriptions.forEach((value) => assert.ok(value.includes(count)));
    assert.ok(!html.includes("{{STATION_"));
    if (name.startsWith("web/")) {
      assert.ok(html.match(/<title>(.*?)<\/title>/s)?.[1].includes(count));
      const h1 = html.match(/<h1[^>]+data-zh="([^"]*)"[^>]+data-en="([^"]*)"[^>]*>(.*?)<\/h1>/s);
      assert.ok(h1); h1.slice(1).forEach((value) => assert.ok(value.includes(count)));
      const countFor = (...clusters) => graph.nodes.filter((n) => clusters.includes(n.cluster)).length;
      assert.ok(html.includes(`走盘 ${countFor("systems")} · 身份裁决 ${countFor("identity")} · 理解嵌入 ${countFor("models", "media")} · 检索求证 ${countFor("search")}`));
      assert.ok(html.includes(`${countFor("eval", "process")} 概念 · 17/19`));
    }
  }
});
await check("template count replacement cannot rewrite a literal token in lesson content", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "goldmine-learn-count-"));
  try {
    const fixture = { meta: { layers: [{ n: 0, label_zh: "基础", label_en: "Fundamentals" }] }, nodes: [
      { id: "literal", cluster: "search", layer: 0, title_zh: "literal {{CONCEPT_COUNT}}", title_en: "Literal token" },
    ] };
    const graph = join(temporary, "graph.json"), template = join(temporary, "template.html"), output = join(temporary, "out.html");
    await writeFile(graph, JSON.stringify(fixture));
    await writeFile(template, '<title>{{CONCEPT_COUNT}}</title><script id="gm-graph">{{GRAPH_JSON}}</script><div class="fallback" hidden></div>');
    execFileSync(process.env.GOLDMINE_LEARN_PYTHON || "python3", [fileURLToPath(new URL("site/tools/build_learn.py", root)), "--graph", graph, "--template", template, "--out", output], { stdio: "pipe" });
    const html = await readFile(output, "utf8");
    assert.ok(html.includes("<title>1</title>"));
    const embedded = JSON.parse(html.match(/<script id="gm-graph">(.*?)<\/script>/s)[1]);
    assert.equal(embedded.nodes[0].title_zh, "literal {{CONCEPT_COUNT}}");
  } finally { await rm(temporary, { recursive: true, force: true }); }
});
await check("cold and same-document lesson hashes use one parser", () => {
  const has = (id) => ["z-score", "reranker-lease"].includes(id);
  assert.deepEqual(parseLearnHash("#c=z-score", has), { kind: "concept", id: "z-score" });
  assert.deepEqual(parseLearnHash("#c=reranker-lease", has), { kind: "concept", id: "reranker-lease" });
  assert.deepEqual(parseLearnHash("#stage=search", has), { kind: "station", id: "search" });
  assert.deepEqual(parseLearnHash("#l0", has), { kind: "l0" });
  assert.deepEqual(parseLearnHash("", has), { kind: "stage" });
});
await check("encoded query ampersands survive and malformed hashes do not throw", () => {
  const has = (id) => id === "z-score";
  assert.deepEqual(parseLearnHash("#q=alpha%26beta", has), { kind: "search", query: "alpha&beta" });
  assert.deepEqual(parseLearnHash("#q=%E0%A4%A", has), { kind: "stage" });
  assert.deepEqual(parseLearnHash("#c=missing", has), { kind: "stage" });
});

console.log(JSON.stringify({ kind: "learn-mechanism-model-checks", status: "PASS", checks: checks.length,
  names: checks, node: process.version, root: fileURLToPath(root),
  evidence_scope: "analytic teaching-model and content checks; not browser QA or search-quality measurement" }, null, 2));
