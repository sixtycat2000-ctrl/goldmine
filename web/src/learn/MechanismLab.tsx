import { useEffect, useId, useMemo, useReducer, useRef, useState } from "react";
import { useMotionPreference } from "../lib/use-motion-preference";
import { MechanismDrawing } from "./MechanismDrawing";
import {
  DEMO_ROWS, IDLE_SECONDS, INITIAL_LEASE, INITIAL_SNAPSHOT, INSPECTION_STAGES, MODE_LABELS, MODE_ORDER,
  inspectionFrame, leaseStep, nextInspectionTime, postingScenario, pruningScenario, scoreScenario, snapshotStep, snapshotView,
  type CellTone, type MechanismMode, type ScorePreset,
} from "./mechanism-model";
import type { Lang } from "../lib/lang";

type Words = [string, string];
const LESSON: Record<MechanismMode, string> = {
  scores: "z-score", pruning: "exact-candidate-pruning", postings: "empty-posting-intersection",
  snapshot: "read-snapshot", leases: "reranker-lease",
};
const STORY: Record<MechanismMode, { what: Words; why: Words; how: Words; try: Words }> = {
  scores: {
    what: ["余弦量方向，z 量它在这一库里有多突出。", "Cosine measures direction. z measures contrast within this corpus."],
    why: ["同样的余弦，在一群相似文档中可能平平无奇。z 不是相关概率。", "The same cosine can be ordinary among similar documents. z is not a relevance probability."],
    how: ["先算全库余弦，再减模态均值；小于 20 行的模态用全局均值。统一居中，除以全局标准差（下限 0.02）。", "Score the whole corpus, subtract modality means (global mean below 20 rows), recenter, then divide by global spread with a 0.02 floor."],
    try: ["保持目标夹角不变，切换「相似邻居」。目标余弦没变，z 为什么变了？", "Keep the target angle fixed. Switch to similar neighbors. Why does z change while its cosine stays the same?"],
  },
  pruning: {
    what: ["全库照算，只为可能入选的文件创建对象。", "Score every row; materialize objects only for files that could qualify."],
    why: ["大量时间可能花在为最终落选的文件建字典，而不是点积本身。", "Building dictionaries for files that cannot qualify can cost more than the dot products."],
    how: ["保留 max(z) ≥ 3 的文件，再并入词法、覆盖、精确与名字路线。此处演示 RRF 候选；最后的准入与排序照旧。", "Keep max(z) ≥ 3 plus lexical, coverage, exact and name routes. This shows a toy RRF pool; final admission and ranking remain separate."],
    try: ["关掉文件 3 的词法证据。为什么它可以消失，但不能提前跳过 26 行向量？", "Remove file 3's lexical evidence. Why may that file disappear while all 26 vector rows still need scoring?"],
  },
  postings: {
    what: ["正文见证是倒排命中 P 与候选 C 的交集。", "Body verification intersects posting hits P with candidates C."],
    why: ["候选为空时，结果已知为空；再读整条倒排表不会改变答案。", "With no candidates, the answer is already empty. Reading a posting list cannot change it."],
    how: ["先检查请求死线并收集候选。C = ∅ 时直接返回；非空时仍做原来的完整核验。", "Check the deadline and collect candidates first. Return immediately for C = ∅; otherwise retain the full verification."],
    try: ["把候选数降到 0。结果是否相同？少了几次查询？", "Set the candidate count to 0. Is the result unchanged? How many lookups disappear?"],
  },
  snapshot: {
    what: ["快照决定这次请求读哪一版；缓存只是可重建的向量副本。", "The snapshot selects this request's data version. A cache is a rebuildable vector copy."],
    why: ["排名、原文和引用必须来自同一版，不能在摄取写入时各读各的。", "Ranking, original text and citations must agree even while ingest publishes changes."],
    how: ["请求开始时固定读视图与已加载向量路径。之后发布、重建或开关缓存只影响下一次请求的验证；失配时选 SQLite，不在搜索里重建。", "Pin the read view and loaded vector path at request start. Later publication, rebuild or cache settings affect the next request’s validation. A mismatch selects SQLite without rebuilding during search."],
    try: ["先发布下一版，再开始新请求。观察旧请求的三枚版本号何时一起改变。", "Publish a version, then begin the next request. When do all three version stamps change together?"],
  },
  leases: {
    what: ["借一次，归还一次；最后一位归还后才开始空闲回收。", "One acquisition, one release. Idle cleanup starts after the last holder returns."],
    why: ["漏一次归还，长驻服务就可能一直以为模型仍有人用。", "A missing release can leave a resident service believing the model is still in use."],
    how: ["借用先登记，真正调用才懒启动。finally 归还；重新借用取消旧空闲期，失效回调不能关闭正在使用的模型。", "Acquisition registers a holder; actual use starts lazily. Release through finally. Reacquisition cancels old idle time, and stale callbacks cannot close an active model."],
    try: ["借用、调用、归还、推进 150 秒，再借用并触发旧回调。模型为什么应当继续亮着？", "Acquire, use, release, advance 150 seconds, reacquire, then fire an old callback. Why should the model stay lit?"],
  },
};

const number = (n: number) => (Math.abs(n) < 0.0005 ? 0 : n).toFixed(3);
const setText = (xs: number[]) => xs.length ? `{${xs.join(", ")}}` : "∅";
const LEASE_EVENT: Record<string, Words> = {
  ready: ["模型未启动", "Model not started"], acquired: ["已借用；第一次真正调用才启动模型", "Acquired; first actual use starts the model"], used: ["模型正在使用中", "The model is in use"],
  released: ["已归还，按剩余持有者判断", "Returned: check remaining holders"], failed: ["新请求借用并调用后失败，finally 已归还它的那一份", "A new request acquired, used and failed; finally returned its own lease"],
  waiting: ["演示时钟已推进", "Demonstration clock advanced"], unloaded: ["空闲到期，模型已回收", "Idle period elapsed; model unloaded"],
  "idle-expired": ["空闲期已到；当前无驻留模型可回收", "Idle period elapsed; no model is currently resident to release"],
  stale: ["旧代回调被忽略", "Stale-generation callback ignored"],
};

export function MechanismLab({ lang, initialMode = "scores", compact = false, suspended = false, onOpenLesson }: {
  lang: Lang; initialMode?: MechanismMode; compact?: boolean; suspended?: boolean;
  onOpenLesson?: (id: string) => void;
}) {
  const tr = (words: Words) => words[lang === "en" ? 1 : 0];
  const uid = useId().replace(/:/g, "");
  const [mode, setMode] = useState(initialMode);
  const [angle, setAngle] = useState(0);
  const [preset, setPreset] = useState<ScorePreset>("quiet");
  const [lexical, setLexical] = useState(true);
  const [optimized, setOptimized] = useState(true);
  const [candidateCount, setCandidateCount] = useState(0);
  const [snapshot, changeSnapshot] = useReducer(snapshotStep, INITIAL_SNAPSHOT);
  const [lease, changeLease] = useReducer(leaseStep, INITIAL_LEASE);
  const [paused, setPaused] = useState(false);
  const [visible, setVisible] = useState(false);
  const [tabVisible, setTabVisible] = useState(true);
  const [announcement, setAnnouncement] = useState("");
  const [phase, setPhase] = useState(0);
  const phaseRef = useRef(0);
  const interacted = useRef(false);
  const host = useRef<HTMLElement>(null);
  const carriage = useRef<SVGGElement>(null);
  const clock = useRef(0);
  const reduced = useMotionPreference();
  const stateful = mode === "snapshot" || mode === "leases";
  const running = visible && tabVisible && !paused && !reduced && !suspended && !stateful;
  const scores = useMemo(() => scoreScenario(angle, preset), [angle, preset]);
  const pruning = useMemo(() => pruningScenario(lexical), [lexical]);
  const postings = useMemo(() => postingScenario(candidateCount), [candidateCount]);
  const view = snapshotView(snapshot);
  const story = STORY[mode];

  useEffect(() => { setMode(initialMode); }, [initialMode]);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver((entries) => {
      setVisible(entries[entries.length - 1].isIntersecting);
    }, { rootMargin: "80px 0px" });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const update = () => setTabVisible(document.visibilityState === "visible");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  const drawCarriage = (elapsed: number) => {
    const frame = inspectionFrame(mode, elapsed, { optimized, candidates: candidateCount, retained: pruning.reducedObjects, total: pruning.fullObjects });
    carriage.current?.setAttribute("transform", `translate(${frame.x * 0.866} ${frame.x * 0.45})`);
    if (phaseRef.current !== frame.index) { phaseRef.current = frame.index; setPhase(frame.index); }
  };
  useEffect(() => { clock.current = 0; phaseRef.current = 0; setPhase(0); carriage.current?.setAttribute("transform", "translate(0 0)"); }, [mode]);
  useEffect(() => {
    if (reduced) { drawCarriage(0); return; }
    if (!running) return;
    let frame = 0, last = 0;
    const tick = (now: number) => {
      const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 0;
      last = now;
      clock.current += dt;
      drawCarriage(clock.current);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [running, reduced, mode, optimized, candidateCount, pruning.reducedObjects]);

  const result = useMemo(() => {
    let cells: CellTone[] = Array.from({ length: DEMO_ROWS }, () => "quiet");
    let needle = 0.5, energized = true, equation = "", outcome = "";
    let metrics: Array<[Words, string]> = [];
    if (mode === "scores") {
      cells = scores.z.map((z, i) => i === 0 ? (z >= 0 ? "keep" : "stale") : z >= 3 ? "keep" : "quiet");
      needle = (scores.z[0] + 5) / 10;
      equation = `cos θ = ${number(scores.target)}   ·   z = ${number(scores.z[0])}`;
      outcome = tr(["z 描述相对突出程度，不是相关概率。", "z describes relative contrast, not relevance probability."]);
      metrics = [[["目标余弦", "Target cosine"], number(scores.target)], [["目标所用均值", "Target's mean"], number(scores.modalityMeans.text)],
        [["全局标准差", "Global spread"], number(scores.spread)], [["目标 z", "Target z"], number(scores.z[0])]];
    } else if (mode === "pruning") {
      cells = pruning.fileIds.map((id) => pruning.kept.includes(id) ? pruning.lexicalIds.includes(id) ? "lexical" : "keep" : optimized ? "quiet" : "matched");
      needle = pruning.reducedObjects / pruning.fullObjects;
      equation = `R = {f : max(zf) ≥ 3} ∪ L   =   ${setText(pruning.kept)}`;
      outcome = tr(["可能候选池相同；最终准入与排名继续执行。", "Same possible-candidate pool; final admission and ranking still follow."]);
      metrics = [[["向量行照算", "Vector rows scored"], String(pruning.vectorRows)], [["原路径文件对象", "Before: file objects"], String(pruning.fullObjects)],
        [["新路径文件对象", "After: file objects"], String(pruning.reducedObjects)], [["可能候选文件", "Possible file IDs"], setText(pruning.kept)]];
    } else if (mode === "postings") {
      cells = cells.map((_, i) => postings.intersection.includes(i + 1) ? "keep" : postings.candidates.includes(i + 1) ? "lexical" : "quiet");
      energized = !optimized || postings.candidates.length > 0;
      needle = postings.candidates.length / 8;
      equation = `P = ${setText(postings.postings)}   ·   P ∩ C = ${setText(postings.intersection)}`;
      outcome = tr(["结果相同；空候选时省下整次 posting 查询。", "Same result; an empty candidate set saves the entire posting lookup."]);
      metrics = [[["候选行", "Candidate rows"], setText(postings.candidates)], [["交集结果", "Intersection"], setText(postings.intersection)],
        [["原路径查询次数", "Before: lookups"], String(postings.oldLookups)], [["新路径查询次数", "After: lookups"], String(postings.newLookups)]];
    } else if (mode === "snapshot") {
      cells = cells.map((_, i) => i < 24 ? "keep" : view.cacheValid ? "lexical" : "stale");
      energized = view.cacheValid;
      needle = view.request / Math.max(view.published, view.request);
      equation = `rank v${view.rankVersion} = text v${view.evidenceVersion} = citation v${view.citationVersion}`;
      outcome = view.cacheValid ? tr([`当前请求持有已加载的 v${view.loadedCache} mmap；新缓存不会替换它。`, `This request holds loaded mmap v${view.loadedCache}; a newer cache does not replace it.`]) :
        tr(["当前请求已选择只读 SQLite；之后的新缓存供下一次请求验证。", "This request selected read-only SQLite; later cache changes are validated by the next request."]);
      metrics = [[["当前请求快照", "Request snapshot"], `v${view.request}`], [["最新发布", "Latest publication"], `v${view.published}`],
        [["本次已加载缓存", "Loaded cache"], view.loadedCache === null ? "—" : `v${view.loadedCache}`], [["本次读取路径", "Held read path"], view.requestPath]];
    } else {
      cells = cells.map((_, i) => i < lease.active ? "keep" : lease.resident ? "matched" : "quiet");
      energized = lease.resident;
      needle = lease.active ? 1 : lease.idleAt === null ? 0 : Math.max(0, (lease.idleAt - lease.clock) / IDLE_SECONDS);
      equation = `${lease.acquired} − ${lease.released} = ${lease.active}   ·   ${tr(["持有数", "active leases"])}`;
      outcome = tr(LEASE_EVENT[lease.event]);
      metrics = [[["已借 / 已还", "Acquired / returned"], `${lease.acquired} / ${lease.released}`], [["仍在使用", "Active holders"], String(lease.active)],
        [["虚拟时钟", "Virtual clock"], `${lease.clock}s`], [["空闲倒计时", "Idle countdown"], lease.idleAt === null ? "—" : `${Math.max(0, lease.idleAt - lease.clock)}s`]];
    }
    return { cells, needle, energized, metrics, equation, outcome };
  // tr is a render-local helper; language is the complete localization input.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, scores, pruning, postings, snapshot, lease, optimized, lang]);

  useEffect(() => {
    if (!interacted.current) return;
    const timer = window.setTimeout(() => setAnnouncement(result.outcome), 600);
    return () => window.clearTimeout(timer);
  }, [result.outcome, mode]);

  const manual = () => { interacted.current = true; setPaused(true); };
  const chooseMode = (next: MechanismMode) => { manual(); setMode(next); };
  const reset = () => {
    manual(); setAngle(0); setPreset("quiet"); setLexical(true); setCandidateCount(0); setOptimized(true);
    changeSnapshot("reset"); changeLease("reset"); clock.current = 0; drawCarriage(0);
  };
  const step = () => {
    manual();
    if (mode === "leases") {
      const action = !lease.active && !lease.resident ? "acquire" : lease.active && !lease.resident ? "use" : lease.active ? "release" : "advance";
      changeLease(action);
      setPhase(action === "acquire" ? 0 : action === "use" ? 1 : action === "release" ? 2 : 3);
    } else if (mode === "snapshot") {
      const next = (phase + 1) % 4;
      if (next === 1) changeSnapshot("publish");
      if (next === 2) changeSnapshot("maintain");
      if (next === 3) changeSnapshot("begin");
      setPhase(next);
    } else {
      clock.current = nextInspectionTime(phaseRef.current);
      drawCarriage(clock.current);
    }
  };
  const button = (label: Words, action: () => void, disabled = false) => (
    <button type="button" disabled={disabled} onClick={() => { manual(); action(); }}>{tr(label)}</button>
  );

  return (
    <section ref={host} className={`mechanism-lab${compact ? " mechanism-lab-compact" : ""}`} data-learning-control="" data-lab-mode={mode} data-paused={paused || !!reduced || suspended || stateful} aria-label={tr(["检索实验台", "Retrieval workbench"])}>
      {!compact && <header className="mechanism-heading">
        <div><span className="mechanism-kicker">01 — 05</span><h2>{tr(["检索实验台", "Retrieval workbench"])}</h2></div>
        <p>{tr(["动一个输入，看清结果与工作量。", "Change one input. Watch the result and the work."])}</p>
      </header>}
      <div className="mechanism-tabs" role="tablist" aria-label={tr(["选择原理", "Choose a mechanism"])}>
        {MODE_ORDER.map((m, i) => <button key={m} id={`tab-${uid}-${m}`} type="button" role="tab" aria-selected={mode === m}
          aria-controls={`panel-${uid}`} tabIndex={mode === m ? 0 : -1} onClick={() => chooseMode(m)}
          onKeyDown={(e) => {
            let index: number | null = null;
            if (e.key === "ArrowRight") index = (i + 1) % MODE_ORDER.length;
            if (e.key === "ArrowLeft") index = (i + MODE_ORDER.length - 1) % MODE_ORDER.length;
            if (e.key === "Home") index = 0;
            if (e.key === "End") index = MODE_ORDER.length - 1;
            if (index !== null) { e.preventDefault(); e.stopPropagation(); chooseMode(MODE_ORDER[index]); document.getElementById(`tab-${uid}-${MODE_ORDER[index]}`)?.focus(); }
          }}><span className="mechanism-tab-number">0{i + 1}</span>{tr(MODE_LABELS[m])}</button>)}
      </div>
      <div id={`panel-${uid}`} role="tabpanel" aria-labelledby={`tab-${uid}-${mode}`} className="mechanism-panel">
        <div className="mechanism-visual">
          <div className="mechanism-figure-caption"><span>{tr(MODE_LABELS[mode])}</span><span>{tr(["确定性演示 · 非实测耗时", "Deterministic demo · not a timing benchmark"])}</span></div>
          <MechanismDrawing mode={mode} cells={result.cells} needle={result.needle} energized={result.energized} carriageRef={carriage} lang={lang} />
          <div className="mechanism-transport">
            <div>
              <button type="button" onClick={() => { interacted.current = true; setPaused((p) => !p); }} aria-pressed={paused} disabled={!!reduced || stateful} data-lab-pause="">
                {stateful ? tr(["手动步骤", "Manual steps"]) : reduced ? tr(["静态模式", "Reduced motion"]) : paused ? tr(["▶ 播放", "▶ Play"]) : tr(["Ⅱ 暂停", "Ⅱ Pause"])}</button>
              {button(["步进", "Step"], step)}
              {button(["重置", "Reset"], reset)}
            </div>
            <span>{mode === "leases" ? tr(["虚拟时钟，不会启动模型", "Virtual clock; no model is started"]) : tr(["图形与读数同源", "One model drives drawing and readout"])}</span>
          </div>
          <div className="mechanism-phase" data-lab-phase={phase}>
            <span>0{phase + 1} / 04</span><strong>{mode === "postings" && phase === 2 && optimized && candidateCount === 0 ? tr(["C 为空，跳过倒排访问", "C is empty: skip the posting lookup"]) : tr(INSPECTION_STAGES[mode][phase])}</strong>
          </div>
          <div className="mechanism-controls" onFocusCapture={() => { interacted.current = true; }}>
            {mode === "scores" && <>
              <label className="mechanism-range-label" htmlFor={`angle-${uid}`}><span>{tr(["目标向量夹角", "Target vector angle"])}</span><output>{angle}°</output></label>
              <input id={`angle-${uid}`} type="range" min="0" max="180" value={angle} step="1" onChange={(e) => { manual(); setAngle(Number(e.target.value)); }} />
              <label className="mechanism-select-label" htmlFor={`preset-${uid}`}>{tr(["语料分布", "Corpus preset"])}</label>
              <select id={`preset-${uid}`} value={preset} onChange={(e) => { manual(); setPreset(e.target.value as ScorePreset); }}>
                <option value="quiet">{tr(["正交邻居：余弦为 0", "Orthogonal neighbors: cosine 0"])}</option>
                <option value="crowded">{tr(["相似邻居：余弦接近目标", "Similar neighbors: close cosines"])}</option>
                <option value="mixed">{tr(["20 文本行 + 6 图片行", "20 text rows + 6 image rows"])}</option>
              </select>
            </>}
            {(mode === "pruning" || mode === "postings") && <div className="mechanism-switch" role="group" aria-label={tr(["查看优化前后", "View before and after"])}>
              <button type="button" aria-pressed={!optimized} onClick={() => { manual(); setOptimized(false); }}>{tr(["原路径", "Before"])}</button>
              <button type="button" aria-pressed={optimized} onClick={() => { manual(); setOptimized(true); }}>{tr(["优化后", "After"])}</button>
            </div>}
            {mode === "pruning" && <label className="mechanism-checkbox"><input type="checkbox" checked={lexical} onChange={(e) => { manual(); setLexical(e.target.checked); }} />{tr(["文件 3 有完整词法证据", "File 3 has full lexical evidence"])}</label>}
            {mode === "postings" && <>
              <label className="mechanism-range-label" htmlFor={`candidates-${uid}`}><span>{tr(["候选行数", "Candidate count"])}</span><output>{candidateCount}</output></label>
              <input id={`candidates-${uid}`} type="range" min="0" max="8" step="1" value={candidateCount} onChange={(e) => { manual(); setCandidateCount(Number(e.target.value)); }} />
            </>}
            {mode === "snapshot" && <div className="mechanism-actions">
              {button(["发布下一版", "Publish next version"], () => { changeSnapshot("publish"); setPhase(1); })}
              {button(["开始新请求", "Begin next request"], () => { changeSnapshot("begin"); setPhase(3); })}
              {button(["模拟显式缓存重建", "Simulate explicit cache rebuild"], () => { changeSnapshot("maintain"); setPhase(2); })}
              <label className="mechanism-checkbox"><input type="checkbox" checked={snapshot.enabled} onChange={() => { manual(); changeSnapshot("cache-toggle"); }} />{tr(["下次请求启用缓存", "Cache for the next request"])}</label>
              <p className="text-[12px] text-sub">{tr([`供后续请求验证的缓存：v${snapshot.cache}。设置不改本次已加载路径。`, `Cache for future validation: v${snapshot.cache}. Settings do not alter this request’s loaded path.`])}</p>
            </div>}
            {mode === "leases" && <div className="mechanism-actions">
              {button(["借用", "Acquire"], () => { changeLease("acquire"); setPhase(0); })}
              {button(["调用", "Use"], () => { changeLease("use"); setPhase(1); }, lease.active === 0)}
              {button(["归还", "Release"], () => { changeLease("release"); setPhase(2); }, lease.active === 0)}
              {button(["新请求失败", "New request fails"], () => { changeLease("fail"); setPhase(2); })}
              {button(["时钟 +150s", "Clock +150s"], () => { changeLease("advance"); setPhase(3); })}
              {button(["触发旧代回调", "Fire stale callback"], () => { changeLease("stale"); setPhase(3); })}
            </div>}
          </div>
          <dl className="mechanism-metrics">{result.metrics.map(([label, value]) => <div key={label[1]}><dt>{tr(label)}</dt><dd>{value}</dd></div>)}</dl>
          <p className="mechanism-equation" data-lab-equation="">{result.equation}</p>
          <p className="mechanism-outcome" data-lab-outcome="">{result.outcome}</p>
          <span className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</span>
        </div>
        {!compact && <div className="mechanism-story">
          <div className="mechanism-explanation"><span>WHAT</span><h3>{tr(["是什么", "What it does"])}</h3><p>{tr(story.what)}</p></div>
          <div className="mechanism-explanation"><span>WHY</span><h3>{tr(["为什么", "Why it matters"])}</h3><p>{tr(story.why)}</p></div>
          <div className="mechanism-explanation"><span>HOW</span><h3>{tr(["怎么做", "How it works"])}</h3><p>{tr(story.how)}</p></div>
          <div className="mechanism-predict"><span>{tr(["先猜，再动手", "Predict, then try"])}</span><p>{tr(story.try)}</p></div>
          {onOpenLesson && <button className="mechanism-lesson-link" type="button" onClick={() => onOpenLesson(LESSON[mode])}>{tr(["读这个机制", "Read this mechanism"])} →</button>}
        </div>}
      </div>
    </section>
  );
}
