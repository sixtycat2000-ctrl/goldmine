/*
 * The machine stage (learn-stage-plan-001): first tier of /learn.
 *
 * Shape decisions frozen in amendments-001: asymmetric pods ∝ concept counts
 * (A5), one chassis line (no pod→arrow→pod), neutral file chips that earn gold
 * only while processed (A2), files terminate at the index block and queries
 * enter at search only (A3), a Layer-1 gate marker at the belt start (C1),
 * ONE timebase — a 7s wave whose `beat` drives every demo (A4) — paused when
 * off-screen or under reduced motion (IO + CSS), names only, no numbering
 * (A8). The QA rail carries values WITH their notes (C3); the locality caption
 * is visible (C7).
 */
import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { pick, type Lang } from "../lib/lang";
import { STATIONS, type StationId } from "../lib/learn-data";
import { SUITE } from "../lib/metrics";
import { IdentityDemo, SearchDemo, WalkQuiet, UnderstandQuiet, DemoTag, DemoFootnote } from "./station-visuals";

export type StageProps = {
  lang: Lang;
  counts: Record<StationId, number>;
  visitedCounts: Record<StationId, number>;
  keystones: Record<StationId, string[]>;
  l0Count: number;
  total: number;
  totalVisited: number;
  onOpen: (id: StationId | "l0") => void;
};

const BEATS = 5;
const BEAT_MS = 1400;
const GRID_COLS = "grid-cols-[31fr_16fr_33fr_5.5rem_31fr]";

const AMBIENT_FILES = ["notes/2026-04.md", "receipts/q3.xlsx", "scans/manual.pdf", "meetings/amex-call.m4a", "img/whiteboard.png"];

/* protagonist lane positions per station (beat 0..2 walk/identity/understand,
   beat>=3 parked at the index) — % of the belt width, matching the pod grid */
const BELT_LEFT = ["2%", "29%", "43%", "66%"];

export function Stage(props: StageProps) {
  const { lang, counts, visitedCounts, keystones, l0Count, total, totalVisited, onOpen } = props;
  const zh = lang === "zh";
  const reduced = useReducedMotion();
  const hostRef = useRef<HTMLDivElement>(null);
  const [beat, setBeat] = useState(0);
  const [cycle, setCycle] = useState(0);
  const [running, setRunning] = useState(false);

  // one timebase, IO-gated: the wave only ticks while the stage is on screen
  useEffect(() => {
    const el = hostRef.current;
    if (!el || reduced || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((es) => es.forEach((e) => setRunning(e.isIntersecting)), { threshold: 0.15 });
    io.observe(el);
    return () => io.disconnect();
  }, [reduced]);

  useEffect(() => {
    if (reduced || !running) return;
    const t = window.setInterval(() => {
      setBeat((b) => {
        if (b + 1 >= BEATS) {
          setCycle((c) => c + 1);
          return 0;
        }
        return b + 1;
      });
    }, BEAT_MS);
    return () => window.clearInterval(t);
  }, [reduced, running]);

  const pods = STATIONS.filter((s) => s.id !== "rail");
  const liveBeat = reduced ? 3 : beat;
  const liveCycle = reduced ? 0 : cycle;

  return (
    <div ref={hostRef} className={`relative ${running ? "" : "stage-paused"}`}>
      {/* machine caption — replaces the old marketing header (A1) */}
      <header className="mx-auto max-w-6xl px-5 pt-24 pb-6">
        <h1 className="mt-3 max-w-[16em] font-display text-display leading-[1.08] text-ink">
          {zh ? "一台可以拆开看的机器" : "A machine you can open up"}
        </h1>
        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3">
          <p className="font-mono text-[13px] text-sub">
            <span className="text-gold">{totalVisited}</span> / {total} {zh ? "已读 · 进度在工位上" : "read · progress lives on the stations"}
          </p>
          <button
            type="button"
            onClick={() => onOpen("l0")}
            className="rounded-full border border-line px-4 py-1.5 font-mono text-[13px] tracking-[0.06em] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
          >
            {zh ? `基础层 L0 · ${l0Count} 概念 →` : `Layer L0 · ${l0Count} concepts →`}
          </button>
          <p className="font-mono text-[13px] text-sub">{zh ? "全部在本机完成 · 无网络传输" : "everything runs on this machine · no network transfer"}</p>
        </div>
        <p className="sr-only">{zh ? "舞台动画为机制示意演示，处理全部发生在本机" : "the stage animation is a stylized mechanism demo; all processing happens locally"}</p>
      </header>

      {/* desktop stage: belt + asymmetric pods + index terminus (lg+) */}
      <section aria-label={zh ? "管线舞台" : "pipeline stage"} className="mx-auto hidden max-w-6xl px-5 lg:block">
        <div className="relative rounded-2xl border border-line bg-bg1/40 p-6 pb-4">
          {/* L1 gate marker at the belt start (C1/A3) */}
          <p className="font-mono text-[13px] tracking-[0.08em] text-ink2">
            {zh ? "起点 · L1 索引指纹门：不符即拒（exit 3）" : "belt start · L1 index fingerprint gate: mismatch refuses (exit 3)"}
          </p>

          <div className={`mt-4 grid ${GRID_COLS} items-end gap-x-3`}>
            {/* row 1 spans all: the belt. Protagonist rides its own lane above
                the ambient ticker (never overlapping), positioned by station
                percentage — the single animated element (B4-safe). */}
            <div className="col-span-full" aria-hidden="true">
              <div className="relative h-8 border-b border-line">
                <motion.span
                  animate={{ left: BELT_LEFT[liveBeat >= 3 ? 3 : liveBeat] }}
                  transition={{ type: "spring", stiffness: 210, damping: 28 }}
                  className="absolute top-0 z-10"
                >
                  <span className={`flex items-center gap-2 rounded border px-2 py-0.5 font-mono text-[13px] transition-colors duration-500 ${liveBeat <= 2 ? "border-gold/70 bg-gold/15 text-ink" : "border-line-strong bg-bg2 text-sub"}`}>
                    <ProtagonistGlyph beat={liveBeat} />
                  </span>
                </motion.span>
                {liveBeat >= 3 && (
                  <span className="absolute top-0 right-0 z-10 rounded border border-teal/50 bg-cl-b/10 px-2 py-0.5 font-mono text-[13px] text-teal">
                    {zh ? "› 查询从这进" : "› queries enter here"}
                  </span>
                )}
              </div>
              {/* ambient ticker with edge fades so the loop reads as a belt */}
              <div
                className="h-7 overflow-hidden"
                style={{ maskImage: "linear-gradient(90deg, transparent, #000 28px, #000 calc(100% - 28px), transparent)", WebkitMaskImage: "linear-gradient(90deg, transparent, #000 28px, #000 calc(100% - 28px), transparent)" }}
              >
                <div className="belt-track flex w-max gap-8 pr-8 pt-1">
                  {[0, 1].map((copy) => (
                    <div key={copy} className="flex gap-8">
                      {AMBIENT_FILES.map((f) => (
                        <span key={f} className="belt-chip rounded border border-line bg-bg1/80 px-2 py-0.5 font-mono text-[13px] text-sub">{f}</span>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* row 2: pods at A5 proportions */}
            {pods.slice(0, 3).map((s) => (
              <StationPod key={s.id} s={s} lang={lang} count={counts[s.id]} read={visitedCounts[s.id]} keystones={keystones[s.id]} beat={liveBeat} onOpen={onOpen} />
            ))}
            <IndexBlock lang={lang} />
            <StationPod s={pods[3]} lang={lang} count={counts.search} read={visitedCounts.search} keystones={keystones.search} beat={liveBeat} cycle={liveCycle} onOpen={onOpen} />
          </div>

          <div className="mt-4"><DemoTag lang={lang} /><DemoFootnote lang={lang} /></div>
        </div>

        <QARail lang={lang} count={counts.rail} read={visitedCounts.rail} keystones={keystones.rail} onOpen={onOpen} />
      </section>

      {/* mobile: vertical subway map + the two teaching demos (A6) */}
      <section aria-label={zh ? "管线舞台" : "pipeline stage"} className="mx-auto max-w-6xl px-5 lg:hidden">
        <div className="rounded-2xl border border-line bg-bg1/40 p-5">
          <p className="font-mono text-[13px] tracking-[0.08em] text-ink2">{zh ? "起点 · L1 索引指纹门：不符即拒（exit 3）" : "start · L1 fingerprint gate: mismatch refuses (exit 3)"}</p>
          <ul className="mt-3 space-y-1 border-l border-line-strong pl-4">
            {pods.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => onOpen(s.id)}
                  className="flex w-full items-baseline justify-between gap-3 rounded-lg px-2 py-2 text-left transition-colors hover:bg-bg2/60"
                >
                  <span className="font-display text-xl text-ink">{pick(lang, s.label_zh, s.label_en)}</span>
                  <span className="font-mono text-[13px] text-sub">
                    {counts[s.id]} · {visitedCounts[s.id]} {zh ? "已读" : "read"} →
                  </span>
                </button>
              </li>
            ))}
            <li className="px-2 py-2 font-mono text-[13px] text-sub">1 × lib.sqlite {zh ? "（文件止于库；查询从检索进）" : "(files end here; queries enter at search)"}</li>
          </ul>
          <button
            type="button"
            onClick={() => onOpen("l0")}
            className="mt-3 w-full rounded-lg border border-line px-3 py-2 text-left font-mono text-[13px] text-ink2 hover:border-gold/60 hover:text-gold"
          >
            {zh ? `基础层 L0 · ${l0Count} 概念 →` : `Layer L0 · ${l0Count} concepts →`}
          </button>
          <p className="mt-3 font-mono text-[13px] text-sub">{zh ? "全部在本机完成 · 无网络传输" : "everything runs on this machine · no network transfer"}</p>
          <div className="mt-3"><DemoTag lang={lang} /><DemoFootnote lang={lang} /></div>
        </div>
        <div className="mt-3 rounded-2xl border border-line bg-bg1/40 p-4">
          <p className="text-[13px] text-sub">{zh ? "身份裁决" : "Identity"}</p>
          <IdentityDemo beat={reduced ? -1 : beat} lang={lang} />
        </div>
        <div className="mt-3 rounded-2xl border border-line bg-bg1/40 p-4">
          <p className="text-[13px] text-sub">{zh ? "检索" : "Search"}</p>
          <SearchDemo beat={liveBeat} cycle={liveCycle} lang={lang} />
        </div>
        <div className="mt-3">
          <QARail lang={lang} count={counts.rail} read={visitedCounts.rail} keystones={keystones.rail} onOpen={onOpen} />
        </div>
      </section>
    </div>
  );
}

/* protagonist representation changes per station (A2): filename → four-layer
   fingerprint → vector grid → parked transaction */
function ProtagonistGlyph({ beat }: { beat: number }) {
  if (beat === 0) return <span>notes/2026-04.md</span>;
  if (beat === 1)
    return (
      <span className="flex items-center gap-1">
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className="h-2 w-2 rounded-[2px] bg-gold/80" />
        ))}
        <span className="ml-1 text-sub">sha·model·extractor·baseline</span>
      </span>
    );
  if (beat === 2)
    return (
      <span className="flex items-center gap-1">
        {[1, 0, 1, 1, 0, 1].map((d, i) => (
          <span key={i} className={`h-2 w-2 rounded-[2px] ${d ? "bg-gold/80" : "border border-line"}`} />
        ))}
      </span>
    );
  return <span className="text-sub">{beat >= 4 ? "入库 · 事务提交" : "入库 · 事务提交"}</span>;
}

function StationPod({
  s, lang, count, read, keystones, beat, cycle = 0, onOpen,
}: {
  s: (typeof STATIONS)[number];
  lang: Lang;
  count: number;
  read: number;
  keystones: string[];
  beat: number;
  cycle?: number;
  onOpen: (id: StationId | "l0") => void;
}) {
  const zh = lang === "zh";
  const active =
    (beat === 1 && s.id === "identity") ||
    (beat >= 3 && s.id === "search") ||
    (beat === 0 && s.id === "walk") ||
    (beat === 2 && s.id === "understand");
  return (
    <div data-station={s.id} className={`flex h-full flex-col rounded-xl border p-4 transition-colors duration-500 ${active ? "border-gold/50 bg-bg2/60" : "border-line bg-bg1/40"}`}>
      <button type="button" onClick={() => onOpen(s.id)} className="group text-left">
        <h2 className="font-display text-2xl leading-tight text-ink group-hover:text-gold">
          {pick(lang, s.label_zh, s.label_en)}
        </h2>
        <p className="mt-1 max-w-[44ch] text-[14px] leading-relaxed text-ink2">{pick(lang, s.role_zh, s.role_en)}</p>
        <p className="mt-2 max-w-[60ch] font-mono text-[13px] leading-relaxed text-sub">
          {count} {zh ? "概念" : "concepts"} · <span className="text-gold">{read}</span> {zh ? "已读" : "read"} →
        </p>
      </button>
      {keystones.length > 0 && (
        <p className="mt-2 max-w-[60ch] font-mono text-[13px] leading-relaxed text-sub">
          {zh ? "基石" : "keystone"}: {keystones.slice(0, 2).join(" · ")}
        </p>
      )}
      <div className="mt-auto pt-3">
        {s.id === "walk" && <WalkQuiet lang={lang} />}
        {s.id === "identity" && <IdentityDemo beat={beat} lang={lang} />}
        {s.id === "understand" && <UnderstandQuiet lang={lang} />}
        {s.id === "search" && <SearchDemo beat={beat} cycle={cycle} lang={lang} />}
      </div>
    </div>
  );
}

function IndexBlock({ lang }: { lang: Lang }) {
  const zh = lang === "zh";
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 rounded-xl border border-line-strong bg-bg2/50 p-3 text-center">
      <p className="font-mono text-[13px] tracking-[0.14em] text-sub uppercase">{zh ? "库" : "index"}</p>
      <p className="font-mono text-[13px] leading-relaxed text-ink2">1 ×<br />lib.sqlite</p>
      <p className="font-mono text-[13px] leading-relaxed text-sub">{zh ? "原文 + FTS5 + 向量" : "text + FTS5 + vectors"}</p>
    </div>
  );
}

/* QA rail: values carry their notes (C3); the 1.4s head-to-head figure is
   deliberately absent from /learn (C3) */
function QARail({
  lang, count, read, keystones, onOpen,
}: { lang: Lang; count: number; read: number; keystones: string[]; onOpen: (id: StationId | "l0") => void }) {
  const zh = lang === "zh";
  return (
    <div className="mt-3 rounded-2xl border border-cl-b/40 bg-cl-b/5 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <button type="button" onClick={() => onOpen("rail")} className="group text-left">
          <h2 className="font-display text-2xl leading-tight text-ink group-hover:text-gold">{zh ? "质检线" : "QA Line"}</h2>
          <p className="mt-1 max-w-[52ch] text-[14px] text-ink2">{zh ? "冻结评测、质量门、诚实契约、三态判定——贯穿全程，不是末端工序" : "Frozen evals, quality gates, the honesty contract — across the whole line, not a last step"}</p>
        </button>
        <p className="font-mono text-[13px] text-sub">
          {count} {zh ? "概念" : "concepts"} · <span className="text-gold">{read}</span> {zh ? "已读" : "read"} →
        </p>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        <p className="rounded-lg bg-bg1/70 px-3 py-2 font-mono text-[13px] text-ink2">
          <span className="text-gold">{SUITE.gatesPassed}/{SUITE.gatesTotal}</span> {zh ? "质量门" : "quality gates"}
          <span className="block text-sub">{zh ? "两门未过已归档为硬上限 → 首页「实测数字」" : "both fails archived as hard limits → home “measured numbers”"}</span>
        </p>
        <p className="rounded-lg bg-bg1/70 px-3 py-2 font-mono text-[13px] text-ink2">
          <span className="text-gold">{SUITE.tests}</span> {zh ? "测试" : "tests"}
          <span className="block text-sub">{pick(lang, SUITE.testsNote.zh, SUITE.testsNote.en)}</span>
        </p>
        <p className="rounded-lg bg-bg1/70 px-3 py-2 font-mono text-[13px] text-ink2">
          <span className="text-gold">{SUITE.bytesUploaded} B</span> {zh ? "上传" : "uploaded"}
          <span className="block text-sub">{zh ? "代码里没有这条路径" : "no such code path"}</span>
        </p>
      </div>
      {keystones.length > 0 && (
        <p className="mt-3 max-w-[60ch] font-mono text-[13px] leading-relaxed text-sub">
          {zh ? "基石" : "keystone"}: {keystones.slice(0, 4).join(" · ")}
        </p>
      )}
    </div>
  );
}
