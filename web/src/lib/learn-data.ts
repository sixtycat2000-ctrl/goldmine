/*
 * Boot data for the Learn page — the inline `#gm-graph` JSON baked by
 * site/tools/build_learn.py (no runtime fetch; the no-JS glossary in the page
 * doubles as the crawler-visible corpus). Parse failure or version < 3 is a
 * corruption signal: the caller reveals the baked fallback and shows a banner.
 */

export type LayerMeta = { n: number; label_zh: string; label_en: string; blurb_zh: string; blurb_en: string };
export type ClusterMeta = { id: string; token: string; label_zh: string; label_en: string; blurb_zh: string; blurb_en: string };

export type Ref = { path: string; lines?: string; note?: string };
export type DeepLink = { to: string; rel_zh: string; rel_en: string };

export type Concept = {
  id: string;
  cluster: string;
  layer: number;
  keystone: boolean;
  parent: string | null;
  title_zh: string;
  title_en: string;
  one_zh: string;
  one_en: string;
  def_zh: string;
  def_en: string;
  plain_zh: string;
  plain_en: string;
  example_zh: string;
  example_en: string;
  analogy_zh: string;
  analogy_en: string;
  refs: Ref[];
  deep: DeepLink[];
};

export type Graph = {
  version: number;
  layers: LayerMeta[];
  clusters: ClusterMeta[];
  nodes: Concept[];
};

export type LearnData =
  | { ok: true; graph: Graph; byId: Map<string, Concept>; childrenOf: Map<string, string[]> }
  | { ok: false; reason: string };

export function loadLearnData(): LearnData {
  let raw: string;
  try {
    const el = document.getElementById("gm-graph");
    raw = el?.textContent ?? "";
  } catch {
    return { ok: false, reason: "script tag missing" };
  }
  if (!raw.trim()) return { ok: false, reason: "empty payload" };

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return { ok: false, reason: `JSON.parse: ${String(e)}` };
  }

  const g = parsed as { meta?: { version?: number; layers?: LayerMeta[]; clusters?: ClusterMeta[] }; nodes?: Concept[]; version?: number };
  if (!g || typeof g !== "object" || !Array.isArray(g.nodes)) {
    return { ok: false, reason: "schema: missing nodes" };
  }
  // bake v3 keeps layers/clusters under `meta`
  const version = typeof g.version === "number" ? g.version : g.meta?.version;
  if (typeof version !== "number" || version < 3) {
    return { ok: false, reason: `version ${String(version)} < 3` };
  }
  if (!Array.isArray(g.meta?.layers) || !Array.isArray(g.meta?.clusters)) {
    return { ok: false, reason: "schema: missing meta.layers/meta.clusters" };
  }

  const graph: Graph = {
    version,
    layers: g.meta!.layers!,
    clusters: g.meta!.clusters!,
    nodes: g.nodes,
  };

  const byId = new Map<string, Concept>();
  for (const n of graph.nodes) byId.set(n.id, n);

  // parent forest comes from `parent` (bake v3); deep links may also cross layers
  const childrenOf = new Map<string, string[]>();
  for (const n of graph.nodes) {
    if (n.parent && byId.has(n.parent)) {
      const list = childrenOf.get(n.parent) ?? [];
      list.push(n.id);
      childrenOf.set(n.parent, list);
    }
  }

  return { ok: true, graph, byId, childrenOf };
}

const CLUSTER_TOKEN_COLOR: Record<string, string> = {
  A: "var(--color-cl-a)",
  B: "var(--color-cl-b)",
  C: "var(--color-cl-c)",
  D: "var(--color-cl-d)",
  E: "var(--color-cl-e)",
  F: "var(--color-cl-f)",
  G: "var(--color-cl-g)",
};

export function clusterColor(token: string): string {
  return CLUSTER_TOKEN_COLOR[token] ?? "var(--color-gold)";
}

const LAYER_ZH = ["L0 · 基础", "L1 · 领域", "L2 · 系统", "L3 · 机制"];
const LAYER_EN = ["L0 · Fundamentals", "L1 · Field methods", "L2 · System tech", "L3 · Mechanisms"];

export function layerBadge(layer: number, lang: "zh" | "en"): string {
  return (lang === "en" ? LAYER_EN : LAYER_ZH)[layer] ?? `L${layer}`;
}

/** Difficulty depth 0..1 — L0 fundamentals touch the floor, L3 mechanisms the top. */
export function layerDepth(layer: number): number {
  return Math.min(1, Math.max(0, (layer + 1) / 4));
}

/** Honest reading-time estimate from the concept's actual prose length. */
export function readingMinutes(c: Concept, lang: "zh" | "en"): number {
  const zhText = `${c.def_zh}${c.plain_zh}${c.example_zh}${c.analogy_zh}`;
  const enText = `${c.def_en}${c.plain_en}${c.example_en}${c.analogy_en}`;
  const zhMin = zhText.length / 420;
  const enWords = enText.split(/\s+/).filter(Boolean).length / 210;
  const minutes = Math.max(zhMin, enWords);
  void lang;
  return Math.max(1, Math.round(minutes));
}

/* ---------------- visited-progress store (localStorage) ---------------- */

const KEY = "gm-learn-visited";

export function loadVisited(): Set<string> {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return new Set();
    const arr: unknown = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr.filter((x): x is string => typeof x === "string")) : new Set();
  } catch {
    return new Set();
  }
}

export function saveVisited(set: Set<string>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify([...set]));
  } catch {
    /* private mode: progress is session-only */
  }
}
