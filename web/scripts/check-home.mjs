import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import ts from "../node_modules/typescript/lib/typescript.js";
import { HOME_COPY, HOME_DEMO_SCENES } from "../src/home/home-demo-data.ts";
import { SUITE, QMD_HEAD_TO_HEAD, LEDGER, HISTORICAL_QMD_URL } from "../src/lib/metrics.ts";

const root = new URL("../../", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");
const results = [];
async function check(name, fn) {
  try { await fn(); results.push({ name, status: "PASS" }); }
  catch (error) { results.push({ name, status: "FAIL", reason: String(error) }); }
}
const graph = JSON.parse(await read("site/content/graph.baked.json"));
const lessonIds = new Set(graph.nodes.map((node) => node.id));
const languages = ["zh", "en"];

await check("Chinese hero is concise and explicit about local files", () => {
  assert.match(HOME_COPY.zh.title, /本机文件/);
  assert.ok([...HOME_COPY.zh.description].length <= 90);
  assert.match(HOME_COPY.zh.description, /Mac/);
  assert.match(HOME_COPY.zh.description, /原文|片段|出处|位置/);
});
await check("both languages have complete hero copy", () => {
  for (const lang of languages) for (const field of ["title", "description"]) {
    assert.equal(typeof HOME_COPY[lang][field], "string");
    assert.ok(HOME_COPY[lang][field].trim().length > 5);
  }
});
await check("three examples have distinct queries, excerpts, files and locations", () => {
  assert.deepEqual(HOME_DEMO_SCENES.map((scene) => scene.id), ["semantic", "identifier", "screenshot"]);
  for (const lang of languages) {
    for (const field of ["label", "query", "file", "location", "excerpt", "evidence"]) {
      assert.ok(HOME_DEMO_SCENES.every((scene) => typeof scene[field][lang] === "string" && scene[field][lang].trim()));
      if (["query", "file", "location", "excerpt"].includes(field)) {
        assert.equal(new Set(HOME_DEMO_SCENES.map((scene) => scene[field][lang])).size, 3);
      }
    }
  }
});
await check("example-to-mechanism links resolve to real lessons", () => {
  for (const scene of HOME_DEMO_SCENES) assert.ok(lessonIds.has(scene.lessonId), scene.lessonId);
});
await check("example fixtures do not invent performance or relevance scores", () => {
  for (const scene of HOME_DEMO_SCENES) {
    for (const key of Object.keys(scene)) assert.ok(!/score|confidence|latency|duration/i.test(key), key);
  }
});
await check("demo is visibly illustrative and does not pretend to query files", async () => {
  const source = await read("web/src/home/HomeSearchDemo.tsx");
  assert.match(source, /示例/);
  assert.doesNotMatch(source, /<input\b|<form\b|fetch\s*\(|dangerouslySetInnerHTML/);
});
await check("five learning entry points match the actual150-concept graph", async () => {
  assert.equal(graph.nodes.length, 150);
  const source = await read("web/src/home/LearnBridge.tsx");
  const ids = [...source.matchAll(/lessonId:\s*"([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, 5); assert.equal(new Set(ids).size, 5);
  for (const id of ids) assert.ok(lessonIds.has(id), id);
  assert.match(source, new RegExp(`查看全部\\s+${graph.nodes.length}\\s+个概念`));
});
await check("home entry modules do not import the learning corpus", async () => {
  for (const name of ["Hero.tsx", "HomeSearchDemo.tsx", "home-demo-data.ts", "LearnBridge.tsx", "HomeApp.tsx"]) {
    assert.doesNotMatch(await read(`web/src/home/${name}`), /import[^;]*?(?:graph\.baked|concepts\.json|learn-data)[^;]*;/s, name);
  }
});
await check("static shell carries the exact Chinese and English hero copy", async () => {
  const html = await read("web/index.html");
  const escaped = (value) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  for (const lang of languages) for (const field of ["title", "description"]) {
    assert.ok(html.includes(escaped(HOME_COPY[lang][field])), `${lang}.${field}`);
  }
  assert.match(html, /href="\/docs#start"/);
  assert.match(html, /href="\/learn/);
});
await check("quickstart target exists in the served docs page", async () => {
  assert.match(await read("web/public/docs.html"), /id=["']start["']/);
});
await check("docs destinations agree on concepts, dated quality scope and lexical setup", async () => {
  for (const path of ["web/public/docs.html", "site/docs.html"]) {
    const html = await read(path);
    assert.match(html, new RegExp(`学习页包含${graph.nodes.length}个概念`));
    for (const value of ["2026-10-26", "product-current-017", "155/180", "17 of 19", "20/30", "6/9"]) {
      assert.ok(html.includes(value), `${path}: ${value}`);
    }
    assert.match(html, /\.venv\/bin\/goldmine ingest sample --db s\.sqlite --lexical-only/);
    assert.match(html, /\.venv\/bin\/goldmine search [^\n]+--lexical --pack/);
    assert.match(html, /本机建库与检索的文件上传/);
    assert.match(html, /onward cloud transmission depends on that client/);
    assert.doesNotMatch(html, /145 concepts|145 个概念|latest dev run fails 12|最近一次开发跑分 12|0 bytes|一切本地无云|准入 z≥3|admission z≥3|QMD 没有的维度/);
  }
});
await check("test totals retain the correct recorded scope", () => {
  assert.equal(SUITE.tests, "3,378");
  assert.match(SUITE.testsNote.zh, /分层|三类/);
  assert.doesNotMatch(SUITE.testsNote.zh, /离线|8\s*分钟/);
});
await check("historical QMD numerical cells are unchanged", () => {
  assert.deepEqual(QMD_HEAD_TO_HEAD.rows.map((row) => [row.goldmine, row.qmd]), [
    ["78.3%", "66.1%"], ["122", "60"], ["1.4s", "21.8s"], ["1.7s", "36.5s"], ["91.7%", "100%"], ["43–60%", "0%"],
  ]);
});
await check("historical comparison points to its public scoped summary", async () => {
  const id = "historical-qmd-comparison-2026-10-25";
  assert.ok((await read("docs/FEATURE_STATUS.md")).includes(`id="${id}"`));
  assert.ok(HISTORICAL_QMD_URL.endsWith(`docs/FEATURE_STATUS.md#${id}`));
  assert.match(await read("web/src/home/Tables.tsx"), /href=\{HISTORICAL_QMD_URL\}/);
});
await check("ledger no longer calls all regression deltas zero", () => {
  assert.doesNotMatch(JSON.stringify(LEDGER), /Δ 0\.0000/);
});
await check("known false claims stay out of authored homepage strings", async () => {
  const paths = ["Hero", "SceneTabs", "Bento", "Narrative", "Scenarios", "MetricsBar", "Tables", "Closing"].map((name) => `web/src/home/${name}.tsx`);
  paths.push("web/src/components/Footer.tsx", "web/src/lib/metrics.ts");
  for (const path of paths) {
    const source = await read(path);
    const tree = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, path.endsWith("tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const strings = [];
    function walk(node) {
      if (ts.isStringLiteralLike(node) || ts.isJsxText(node)) strings.push(node.text);
      ts.forEachChild(node, walk);
    }
    walk(tree);
    const text = strings.join("\n");
    assert.doesNotMatch(text, /编号按哈希|编号(?:类)?查询自动反转|精确命中压过语义|0 字节离开|0 bytes leave|离开你的机器|copy the one file|复制那一个文件|没有一项是营销话术/, path);
  }
});
await check("particle preference changes never reload the page", async () => {
  assert.doesNotMatch(await read("web/src/home/ParticleField.tsx"), /location\.reload/);
});
await check("the independent website gate exercises the real build and checks", async () => {
  const workflow = await read(".github/workflows/website.yml");
  for (const command of ["npm --prefix web ci", "node web/scripts/check-home.mjs", "node web/scripts/check-home-motion.mjs", "node web/scripts/check-mechanisms.mjs", "npm --prefix web run build"]) assert.ok(workflow.includes(command), command);
  assert.match(workflow, /contents: read/);
});

console.log(JSON.stringify({ kind: "homepage-facts-and-navigation-checks", status: results.every((r) => r.status === "PASS") ? "PASS" : "FAIL", checks: results,
  root: fileURLToPath(root), evidence_scope: "Facts, illustrative data and links. This does not grade Chinese naturalness or measure conversion, search quality or browser behavior." }, null, 2));
process.exitCode = results.every((result) => result.status === "PASS") ? 0 : 1;
