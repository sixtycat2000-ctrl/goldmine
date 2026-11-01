/*
 * The machine stage (learn-stage-plan-001): first tier of /learn.
 *
 * amendments-002 (user-directed): the stage is VISUAL FIRST — station pods
 * carry name + count + the demo graphic only; every sentence of explanation
 * lives one level down (station panel), every concept two levels down (card),
 * the full lesson three levels down (drawer). The DepthRail makes the level
 * down explicit: 舞台 · 工位 · 概念 · 详解.
 *
 * Frozen mechanics (amendments-001, unchanged): asymmetric pods ∝ counts (A5),
 * neutral file chips earning gold only while processed (A2), files terminate
 * at the index and queries enter at search only (A3), ONE timebase (A4) paused
 * off-screen / reduced-motion, names only (A8), QA rail values carry notes
 * in their shortest visible form (C3), locality caption visible (C7).
 */
import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { pick, type Lang } from "../lib/lang";
import { STATIONS, type StationId } from "../lib/learn-data";
import { SUITE } from "../lib/metrics";
import { IdentityDemo, SearchDemo, WalkDemo, UnderstandDemo, DemoTag, DemoFootnote } from "./station-visuals";

export type StageProps = {
  lang: Lang;
  counts: Record<StationId, number>;
  visitedCounts: Record<StationId, number>;
  l0Count: number;
  total: number;
  totalVisited: number;
  onOpen: (id: StationId | "l0") => void;
  /* amendments-004 T1/T2: a machine part opens the concept it belongs to */
  onOpenPart: (conceptId: string) => void;
};

const BEATS = 5;
const BEAT_MS = 1400;
const GRID_COLS = "grid-cols-[31fr_16fr_33fr_5.5rem_31fr]";
const AMBIENT_FILES = ["notes/2026-04.md", "receipts/q3.xlsx", "scans/manual.pdf", "meetings/amex-call.m4a", "img/whiteboard.png"];
/* protagonist lane positions per station (beat 0..2 walk/identity/understand,
   beat>=3 parked at the index) — % of the belt width, matching the pod grid */
const BELT_LEFT = ["2%", "29%", "43%", "66%"];

/* the four depths of /learn — names only, no numbers (A8) */
export function DepthRail({ lang, at }: { lang: Lang; at: "stage" | "station" | "concept" | "lesson" }) {
  const zh = lang === "zh";
  const levels: Array<[string, typeof at]> = [
    [zh ? "舞台" : "stage", "stage"],
    [zh ? "工位" : "station", "station"],
    [zh ? "概念" : "concept", "concept"],
    [zh ? "详解" : "lesson", "lesson"],
  ];
  const idx = levels.findIndex(([, k]) => k === at);
  return (
    <p className="flex items-center gap-2 font-mono text-[12px] tracking-[0.1em] text-sub uppercase" aria-label={zh ? "下钻层级" : "drill depth"}>
      {levels.map(([label, k], i) => (
        <span key={k} className="flex items-center gap-2">
          {i > 0 && <span aria-hidden="true">→</span>}
          <span className={k === at ? "text-gold" : i < idx ? "text-ink2" : ""}>{label}</span>
        </span>
      ))}
    </p>
  );
}

export function Stage(props: StageProps) {
  const { lang, counts, visitedCounts, l0Count, total, totalVisited, onOpen, onOpenPart } = props;
  const zh = lang === "zh";
  const reduced = useReducedMotion();
  const hostRef = useRef<HTMLDivElement>(null);
  const [beat, setBeat] = useState(0);
  const [cycle, setCycle] = useState(0);
  const [inView, setInView] = useState(false);
  // amendments-004 T3: a hidden tab must not run the machine either
  const [tabVisible, setTabVisible] = useState(true);
  const running = inView && tabVisible;

  // one timebase, IO-gated: the wave only ticks while the stage is on screen
  useEffect(() => {
    const el = hostRef.current;
    if (!el || reduced || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((es) => es.forEach((e) => setInView(e.isIntersecting)), { threshold: 0.15 });
    io.observe(el);
    return () => io.disconnect();
  }, [reduced]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVis = () => setTabVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

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
      {/* machine caption — slim (A1 + E4): kicker, h1, depth rail, progress, L0, locality */}
      <header className="mx-auto max-w-6xl px-5 pt-24 pb-6">
        <p className="kicker">LEARN · THE MACHINE</p>
        <h1 className="mt-3 max-w-[16em] font-display text-hero leading-[1.06] text-ink">
          {zh ? "一台可以拆开看的机器" : "A machine you can open up"}
        </h1>
        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3">
          <DepthRail lang={lang} at="stage" />
          <p className="font-mono text-[12px] text-sub">
            <span className="text-gold">{totalVisited}</span> / {total}
          </p>
          <button
            type="button"
            onClick={() => onOpen("l0")}
            className="rounded-full border border-line px-4 py-1.5 font-mono text-[12px] tracking-[0.06em] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
          >
            L0 · {l0Count} →
          </button>
          <p className="font-mono text-[12px] text-sub">{zh ? "本机运行 · 无网络传输" : "runs locally · no network"}</p>
        </div>
        <p className="sr-only">{zh ? "舞台动画为机制示意演示，处理全部发生在本机" : "the stage animation is a stylized mechanism demo; all processing happens locally"}</p>
      </header>

      {/* desktop stage: belt + asymmetric pods + index terminus (lg+) */}
      <section aria-label={zh ? "管线舞台" : "pipeline stage"} className="mx-auto hidden max-w-6xl px-5 lg:block">
        <div className="relative rounded-2xl border border-line bg-bg1/40 p-6 pb-4">
          <p className="font-mono text-[12px] tracking-[0.08em] text-ink2">
            {zh ? "起点 · L1 指纹门" : "start · L1 fingerprint gate"}
          </p>

          <div className={`mt-4 grid ${GRID_COLS} items-end gap-x-3`}>
            {/* row 1 spans all: the belt. Protagonist rides its own lane above
                the ambient ticker (never overlapping). */}
            <div className="col-span-full" aria-hidden="true">
              <div className="relative h-8 border-b border-line">
                <motion.span
                  animate={{ left: BELT_LEFT[liveBeat >= 3 ? 3 : liveBeat] }}
                  transition={{ type: "spring", stiffness: 210, damping: 28 }}
                  className="absolute top-0 z-10"
                >
                  <span className={`flex items-center gap-2 rounded border px-2 py-0.5 font-mono text-[12px] transition-colors duration-500 ${liveBeat <= 2 ? "border-gold/70 bg-gold/15 text-ink" : "border-line-strong bg-bg2 text-sub"}`}>
                    <ProtagonistGlyph beat={liveBeat} />
                  </span>
                </motion.span>
                {liveBeat >= 3 && (
                  <span className="absolute top-0 right-0 z-10 rounded border border-teal/50 bg-cl-b/10 px-2 py-0.5 font-mono text-[12px] text-teal">
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
                        <span key={f} className="belt-chip rounded border border-line bg-bg1/80 px-2 py-0.5 font-mono text-[12px] text-sub">{f}</span>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* row 2: pods at A5 proportions — name, count, demo. No prose (E1). */}
            {pods.slice(0, 3).map((s) => (
              <StationPod key={s.id} s={s} lang={lang} count={counts[s.id]} read={visitedCounts[s.id]} beat={liveBeat} onOpen={onOpen} onOpenPart={onOpenPart} />
            ))}
            <IndexBlock lang={lang} onOpenPart={onOpenPart} />
            <StationPod s={pods[3]} lang={lang} count={counts.search} read={visitedCounts.search} beat={liveBeat} cycle={liveCycle} onOpen={onOpen} onOpenPart={onOpenPart} />
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-1">
            <DemoTag lang={lang} />
            <DemoFootnote lang={lang} />
          </div>
        </div>

        <QARail lang={lang} count={counts.rail} read={visitedCounts.rail} onOpen={onOpen} />
      </section>

      {/* mobile: vertical subway map + the two teaching demos (A6) */}
      <section aria-label={zh ? "管线舞台" : "pipeline stage"} className="mx-auto max-w-6xl px-5 lg:hidden">
        <div className="rounded-2xl border border-line bg-bg1/40 p-5">
          <p className="font-mono text-[12px] tracking-[0.08em] text-ink2">{zh ? "起点 · L1 指纹门" : "start · L1 gate"}</p>
          <ul className="mt-3 space-y-1 border-l border-line-strong pl-4">
            {pods.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => onOpen(s.id)}
                  className="flex w-full items-baseline justify-between gap-3 rounded-lg px-2 py-2 text-left transition-colors hover:bg-bg2/60"
                >
                  <span className="font-display text-xl text-ink">{pick(lang, s.label_zh, s.label_en)}</span>
                  <span className="font-mono text-[12px] text-sub">
                    {counts[s.id]} · <span className="text-gold">{visitedCounts[s.id]}</span> →
                  </span>
                </button>
              </li>
            ))}
            <li>
              <button
                type="button"
                onClick={() => onOpenPart("sqlite-wal")}
                className="flex w-full items-baseline px-2 py-2 text-left font-mono text-[12px] text-sub transition-colors hover:text-gold"
              >
                1 × lib.sqlite
              </button>
            </li>
          </ul>
          <button
            type="button"
            onClick={() => onOpen("l0")}
            className="mt-3 w-full rounded-lg border border-line px-3 py-2 text-left font-mono text-[12px] text-ink2 hover:border-gold/60 hover:text-gold"
          >
            L0 · {l0Count} →
          </button>
          <p className="mt-3 font-mono text-[12px] text-sub">{zh ? "本机运行 · 无网络传输" : "runs locally · no network"}</p>
          <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1">
            <DemoTag lang={lang} />
            <DemoFootnote lang={lang} />
          </div>
        </div>
        <div className="mt-3 rounded-2xl border border-line bg-bg1/40 p-4">
          <IdentityDemo beat={reduced ? -1 : beat} lang={lang} onPart={onOpenPart} />
        </div>
        <div className="mt-3 rounded-2xl border border-line bg-bg1/40 p-4">
          <SearchDemo beat={liveBeat} cycle={liveCycle} lang={lang} onPart={onOpenPart} />
        </div>
        <div className="mt-3">
          <QARail lang={lang} count={counts.rail} read={visitedCounts.rail} onOpen={onOpen} />
        </div>
      </section>
    </div>
  );
}

/* protagonist representation changes per station (A2): filename → four-layer
   fingerprint → vector grid → parked in the index */
function ProtagonistGlyph({ beat }: { beat: number }) {
  if (beat === 0) return <span>notes/2026-04.md</span>;
  if (beat === 1)
    return (
      <span className="flex items-center gap-1">
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className="h-2 w-2 rounded-[2px] bg-gold/80" />
        ))}
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
  return <span className="text-sub">lib.sqlite</span>;
}

/* visual-first pod (E1): name, count, demo — nothing else.
   amendments-004 T1: parts inside the demo open their concept. */
function StationPod({
  s, lang, count, read, beat, cycle = 0, onOpen, onOpenPart,
}: {
  s: (typeof STATIONS)[number];
  lang: Lang;
  count: number;
  read: number;
  beat: number;
  cycle?: number;
  onOpen: (id: StationId | "l0") => void;
  onOpenPart: (conceptId: string) => void;
}) {
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
        <p className="mt-2 font-mono text-[12px] text-sub">
          {count} · <span className="text-gold">{read}</span> →
        </p>
      </button>
      <div className="mt-auto flex flex-1 items-center pt-2">
        {s.id === "walk" && <WalkDemo beat={beat} lang={lang} onPart={onOpenPart} />}
        {s.id === "identity" && <IdentityDemo beat={beat} lang={lang} onPart={onOpenPart} />}
        {s.id === "understand" && <UnderstandDemo beat={beat} lang={lang} onPart={onOpenPart} />}
        {s.id === "search" && <SearchDemo beat={beat} cycle={cycle} lang={lang} onPart={onOpenPart} />}
      </div>
    </div>
  );
}

/* amendments-004 T2: the terminus opens too — cut the index open */
function IndexBlock({ lang, onOpenPart }: { lang: Lang; onOpenPart: (conceptId: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onOpenPart("sqlite-wal")}
      aria-label={pick(lang, "打开概念：SQLite 与 WAL", "open concept: SQLite and WAL")}
      className="flex h-full cursor-pointer flex-col items-center justify-center gap-1 rounded-xl border border-line-strong bg-bg2/50 p-3 text-center transition-colors hover:border-gold/60"
    >
      <span className="font-mono text-[12px] tracking-[0.14em] text-sub uppercase">index</span>
      <span className="font-mono text-[12px] leading-relaxed text-ink2">1 ×<br />lib.sqlite</span>
    </button>
  );
}

/* QA rail (E1): three gauges, notes in their shortest visible form (C3) */
function QARail({
  lang, count, read, onOpen,
}: { lang: Lang; count: number; read: number; onOpen: (id: StationId | "l0") => void }) {
  const zh = lang === "zh";
  return (
    <div className="mt-3 rounded-2xl border border-cl-b/40 bg-cl-b/5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <button type="button" onClick={() => onOpen("rail")} className="group text-left">
          <h2 className="font-display text-2xl leading-tight text-ink group-hover:text-gold">{zh ? "质检线" : "QA Line"}</h2>
        </button>
        <p className="font-mono text-[12px] text-sub">
          {count} · <span className="text-gold">{read}</span> →
        </p>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <p className="rounded-lg bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-3xl text-gold">{SUITE.gatesPassed}/{SUITE.gatesTotal}</span>
          <span className="mt-0.5 block font-mono text-[12px] text-sub">{zh ? "质量门 · 两门已归档 → 首页" : "gates · fails archived → home"}</span>
        </p>
        <p className="rounded-lg bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-3xl text-gold">{SUITE.tests}</span>
          <span className="mt-0.5 block font-mono text-[12px] text-sub">{zh ? "测试 · 全量 8 分钟" : "tests · 8-min suite"}</span>
        </p>
        <p className="rounded-lg bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-3xl text-gold">{SUITE.bytesUploaded} B</span>
          <span className="mt-0.5 block font-mono text-[12px] text-sub">{zh ? "上传 · 无此代码路径" : "uploaded · no such path"}</span>
        </p>
      </div>
    </div>
  );
}
