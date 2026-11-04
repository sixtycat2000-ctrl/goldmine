/* Deterministic teaching models. These never query an index or start a model.
 * The scoring formula mirrors Searcher.zscores; the corpus is deliberately tiny.
 */
export type MechanismMode = "scores" | "pruning" | "postings" | "snapshot" | "leases";
export type CellTone = "quiet" | "keep" | "lexical" | "matched" | "stale";
export const MODE_ORDER: MechanismMode[] = ["scores", "pruning", "postings", "snapshot", "leases"];
export const Z_GATE = 3;
export const STD_FLOOR = 0.02;
export const IDLE_SECONDS = 300;
export const DEMO_ROWS = 26;

export const MODE_LABELS: Record<MechanismMode, [string, string]> = {
  scores: ["打分", "Scores"], pruning: ["精确筛选", "Exact pruning"],
  postings: ["空集合", "Empty sets"], snapshot: ["快照与缓存", "Snapshots"],
  leases: ["模型借还", "Model leases"],
};

const mean = (xs: readonly number[]) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;

export function normalizeScores(scores: readonly number[], modalities: readonly string[]) {
  if (scores.length !== modalities.length) throw new Error("unaligned score and modality rows");
  const globalMean = mean(scores);
  const modalityMeans: Record<string, number> = {};
  for (const kind of new Set(modalities)) {
    const group = scores.filter((_, i) => modalities[i] === kind);
    modalityMeans[kind] = group.length >= 20 ? mean(group) : globalMean;
  }
  const centered = scores.map((s, i) => s - modalityMeans[modalities[i]]);
  const centeredMean = mean(centered);
  const spread = Math.sqrt(mean(centered.map((s) => (s - centeredMean) ** 2)));
  const denominator = Math.max(spread, STD_FLOOR);
  return { globalMean, modalityMeans, centeredMean, spread, denominator,
    z: centered.map((s) => (s - centeredMean) / denominator) };
}

export type ScorePreset = "quiet" | "crowded" | "mixed";
export function scoreScenario(angle: number, preset: ScorePreset = "quiet") {
  const target = Math.cos(angle * Math.PI / 180);
  const scores = Array.from({ length: DEMO_ROWS }, (_, i) => i === 0 ? target :
    preset === "quiet" ? 0 : preset === "crowded" ? 0.68 + ((i * 7) % 13) / 60 :
      i < 20 ? 0.3 + (i % 5) / 10 : 0.88 + (i % 3) / 40);
  const modalities = scores.map((_, i) => preset === "mixed" && i >= 20 ? "image" : "text");
  return { angle, target, scores, modalities, ...normalizeScores(scores, modalities) };
}

export function pruningScenario(lexical = true) {
  const scores = [1, ...Array.from({ length: DEMO_ROWS - 1 }, () => 0)];
  const { z } = normalizeScores(scores, scores.map(() => "text"));
  const fileIds = scores.map((_, i) => Math.floor(i / 4) + 1);
  const maxima = new Map<number, number>();
  fileIds.forEach((id, i) => maxima.set(id, Math.max(maxima.get(id) ?? -Infinity, z[i])));
  const lexicalIds = lexical ? [3] : [];
  const kept = [...maxima].filter(([id, value]) => value >= Z_GATE || lexicalIds.includes(id)).map(([id]) => id);
  // Independently run the old flow: first allocate every record, then admit it.
  // This deliberately tiny RRF example is not the complete hybrid admission policy.
  const oldRecords = [...maxima].map(([id, value]) => ({ id, z: value, wordRank: lexicalIds.includes(id) ? 1 : null }));
  const admit = (records: typeof oldRecords) => records.filter((row) => row.wordRank !== null || Number.isFinite(row.z) && row.z >= Z_GATE).map((row) => row.id);
  const original = admit(oldRecords);
  const candidateRecords = kept.map((id) => ({ id, z: maxima.get(id)!, wordRank: lexicalIds.includes(id) ? 1 : null }));
  const after = admit(candidateRecords);
  return { scores, z, fileIds, maxima, kept, original, after, lexicalIds,
    fullObjects: maxima.size, reducedObjects: kept.length, vectorRows: scores.length };
}

export function postingScenario(count: number) {
  const candidates = Array.from({ length: Math.max(0, Math.min(8, Math.round(count))) }, (_, i) => i + 1);
  const postings = [2, 4, 7];
  const intersection = candidates.filter((id) => postings.includes(id));
  return { candidates, postings, intersection, oldLookups: 1, newLookups: candidates.length ? 1 : 0 };
}

export type SnapshotState = {
  published: number; request: number; cache: number; enabled: boolean;
  requestPath: "mmap" | "SQLite"; loadedCache: number | null;
};
export const INITIAL_SNAPSHOT: SnapshotState = {
  published: 1, request: 1, cache: 1, enabled: true, requestPath: "mmap", loadedCache: 1,
};
export type SnapshotAction = "publish" | "begin" | "cache-toggle" | "maintain" | "reset";
export function snapshotStep(state: SnapshotState, action: SnapshotAction): SnapshotState {
  switch (action) {
    case "publish": return { ...state, published: state.published + 1 };
    case "begin": {
      const matched = state.enabled && state.cache === state.published;
      return { ...state, request: state.published, requestPath: matched ? "mmap" : "SQLite",
        loadedCache: matched ? state.cache : null };
    }
    case "cache-toggle": return { ...state, enabled: !state.enabled };
    case "maintain": return { ...state, cache: state.published };
    case "reset": return { ...INITIAL_SNAPSHOT };
  }
}
export function snapshotView(state: SnapshotState) {
  return { ...state, cacheValid: state.requestPath === "mmap",
    rankVersion: state.request, evidenceVersion: state.request, citationVersion: state.request };
}

export type LeaseState = {
  acquired: number; released: number; active: number; clock: number;
  idleAt: number | null; resident: boolean; generation: number;
  event: "ready" | "acquired" | "used" | "released" | "failed" | "waiting" | "unloaded" | "idle-expired" | "stale";
};
export const INITIAL_LEASE: LeaseState = {
  acquired: 0, released: 0, active: 0, clock: 0, idleAt: null,
  resident: false, generation: 0, event: "ready",
};
export type LeaseAction = "acquire" | "use" | "release" | "fail" | "advance" | "stale" | "reset";
export function leaseStep(state: LeaseState, action: LeaseAction): LeaseState {
  switch (action) {
    case "reset": return { ...INITIAL_LEASE };
    case "acquire": return { ...state, acquired: state.acquired + 1, active: state.active + 1,
      idleAt: null, generation: state.generation + 1, event: "acquired" };
    case "use": return state.active ? { ...state, resident: true, event: "used" } : state;
    case "release": {
      if (!state.active) return state;
      const active = state.active - 1;
      return { ...state, released: state.released + 1, active,
        idleAt: active ? null : state.clock + IDLE_SECONDS, generation: state.generation + 1, event: "released" };
    }
    case "fail": return { ...leaseStep(leaseStep(leaseStep(state, "acquire"), "use"), "release"), event: "failed" };
    case "advance": {
      const clock = state.clock + 150;
      const unload = state.active === 0 && state.idleAt !== null && clock >= state.idleAt;
      return { ...state, clock, resident: unload ? false : state.resident,
        idleAt: unload ? null : state.idleAt, event: unload ? state.resident ? "unloaded" : "idle-expired" : "waiting" };
    }
    // A callback carrying an old generation token cannot touch the current model.
    case "stale": return { ...state, event: "stale" };
  }
}

export const INSPECTION_SECONDS = 3.8;
/** Step to the middle of a discrete phase, never an unstable floating boundary. */
export const nextInspectionTime = (phase: number) => (((phase + 1) % 4) + 0.5) * INSPECTION_SECONDS;
export const INSPECTION_STAGES: Record<MechanismMode, Array<[string, string]>> = {
  scores: [["全库算余弦", "Score all corpus rows"], ["减去模态均值", "Subtract modality means"], ["统一尺度，得到 z", "Use global spread to obtain z"], ["检查目标读数", "Inspect the target reading"]],
  pruning: [["全部向量照算", "Score every vector"], ["全库归一化照做", "Normalize the complete corpus"], ["只建立可能候选对象", "Build possible-candidate records"], ["交给原来的准入与排序", "Keep original admission and ranking"]],
  postings: [["收集候选 C", "Collect candidates C"], ["先判断候选是否为空", "Check whether C is empty"], ["非空才读取倒排 P", "Read P only for nonempty C"], ["返回同样的交集", "Return the same intersection"]],
  snapshot: [["固定当前请求读视图", "Pin the request read view"], ["发布新版本，旧请求保持原视图", "Publish a version; preserve the old request"], ["显式维护只准备后续缓存", "Explicit maintenance prepares a future cache"], ["新请求验证并加载新版本", "Next request validates and loads its version"]],
  leases: [["借用：先登记持有者", "Acquire: register a holder"], ["真正调用才懒启动", "Start lazily on actual use"], ["finally 归还", "Release through finally"], ["最后归还后检查空闲期", "Check idle time after the last release"]],
};

/** Causal inspection phases, not an independent decorative animation. */
export function inspectionFrame(mode: MechanismMode, elapsed: number, context: {
  optimized: boolean; candidates: number; retained: number; total: number;
}) {
  const unit = Math.max(0, elapsed) / INSPECTION_SECONDS;
  const index = Math.floor(unit) % 4;
  const t = unit - Math.floor(unit);
  const ease = (value: number) => { const x = Math.max(0, Math.min(1, value)); return x * x * (3 - 2 * x); };
  const objectReach = context.optimized ? 142 * context.retained / Math.max(1, context.total) : 142;
  const reach = mode === "pruning" ? objectReach : 142;
  const emptySkip = mode === "postings" && context.optimized && context.candidates === 0;
  const x = emptySkip ? 0 : index === 0 ? (t < 0.2 ? -142 * ease(t / 0.2) : -142 + 284 * ease((t - 0.2) / 0.8)) :
    index === 1 ? 142 * (1 - ease(t)) : index === 2 ? (emptySkip ? 0 : reach * ease(t)) :
      (emptySkip ? 0 : reach * (1 - ease(t)));
  return { index, x, label: INSPECTION_STAGES[mode][index], skipped: emptySkip && index === 2 };
}

export const LESSON_MODES: Record<string, MechanismMode> = {
  cosine: "scores", "z-score": "scores", "per-modality-mean": "scores", "mean-std": "scores",
  "vector-space": "scores", "distance-vs-angle": "scores", "vector-matrix": "pruning",
  "exact-candidate-pruning": "pruning", "admission-gate": "pruning", "witness-rule": "pruning",
  "exact-promote": "pruning", "hybrid-fusion": "pruning",
  "empty-posting-intersection": "postings", "inverted-index": "postings", "fts5-bm25": "postings",
  "read-snapshot": "snapshot", "vector-cache": "snapshot", "sqlite-wal": "snapshot",
  "one-transaction": "snapshot", "fp16-vector-store": "snapshot",
  "reranker-lease": "leases", "backend-lifecycle": "leases", "local-rerank": "leases",
  "rank-window": "leases", "serve-linger": "leases", "mcp-queue": "leases",
};
