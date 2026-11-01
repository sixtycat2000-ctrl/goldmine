/*
 * The machine stage (learn-machine-v2-001): first tier of /learn.
 *
 * One integrated machine replaces the four-pod grid (amendments-001 A5 is
 * repealed): disk → belt → L1 gate → chunk/embed → INDEX, queries entering
 * only at the right end, QA as a ruler strip. teardown grammar: a region
 * isolates while the rest steps aside (framer springs on the SVG groups),
 * every part is a button, and a self-driving tour walks one query's life —
 * any input takes over, 25s of quiet hands it back (D5 lands here).
 *
 * Frozen invariants: ONE timebase (beat × BEAT_MS) IO-gated and now also
 * tab-visibility-gated (amendments-004 T3); files terminate at the index and
 * queries enter at search only (A3); QA values keep their shortest notes
 * (C3); locality caption visible (C7); zero visible text inside the machine
 * beyond region names/counts and the terminus caption (E1/V2).
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { pick, type Lang } from "../lib/lang";
import { STATIONS, stationMeta, type StationId } from "../lib/learn-data";
import { SUITE } from "../lib/metrics";
import { DemoTag, DemoFootnote, IdentityDemo, SearchDemo } from "./station-visuals";
import { MachineSVG, BELT_TOP, BELT_MID } from "./machine-drawing";

export type StageProps = {
  lang: Lang;
  counts: Record<StationId, number>;
  visitedCounts: Record<StationId, number>;
  l0Count: number;
  total: number;
  totalVisited: number;
  /* isolate state lives in LearnApp so #stage=<id> deep links land here (G9) */
  isolated: StationId | null;
  onOpen: (id: StationId | "l0") => void;
  onCloseIsolate: () => void;
  onOpenPart: (conceptId: string) => void;
  /* the station tray (role, notes, pills, cards) rendered under the machine */
  tray?: ReactNode;
};

const BEATS = 5;
const BEAT_MS = 1400;
const AMBIENT_FILES = ["notes/2026-04.md", "receipts/q3.xlsx", "scans/manual.pdf", "meetings/amex-call.m4a", "img/whiteboard.png"];
/* protagonist lane positions per beat (machine v2 region geometry) */
const BELT_LEFT = ["9%", "31%", "53%", "70%"];

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

/* tour script: one query's life, as teardown operator moves — targets are
   real machine parts; the cursor only walks, the loops stay honest */
const TOUR: Array<{ part: string }> = [
  { part: "walk-gates" },
  { part: "fingerprint-gate" },
  { part: "chunking" },
  { part: "one-transaction" },
  { part: "admission-gate" },
  { part: "evidence-selection" },
];
const TOUR_FIRST_DELAY = 6000;
const TOUR_IDLE_RETURN = 25000;

export function Stage(props: StageProps) {
  const { lang, counts, visitedCounts, l0Count, total, totalVisited, isolated, onOpen, onCloseIsolate, onOpenPart, tray } = props;
  const zh = lang === "zh";
  const reduced = useReducedMotion();
  const hostRef = useRef<HTMLDivElement>(null);
  const machineRef = useRef<HTMLDivElement>(null);
  const [beat, setBeat] = useState(0);
  const [cycle, setCycle] = useState(0);
  const [inView, setInView] = useState(false);
  const [tabVisible, setTabVisible] = useState(true);
  const [desktop, setDesktop] = useState(false);
  const [tourOn, setTourOn] = useState(false);
  const [tourStep, setTourStep] = useState(0);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const running = inView && tabVisible;

  /* one timebase, IO-gated + tab-gated */
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

  /* tour eligibility: desktop, motion allowed, whole machine on stage (D5) */
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia === "undefined") return;
    const mq = window.matchMedia("(min-width: 1024px)");
    const on = () => setDesktop(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const tourEligible = !reduced && desktop && isolated === null;

  /* any input takes over; quiet hands it back */
  useEffect(() => {
    if (!tourEligible) {
      setTourOn(false);
      return;
    }
    let timer = window.setTimeout(() => setTourOn(true), TOUR_FIRST_DELAY);
    const take = () => {
      setTourOn(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setTourOn(true), TOUR_IDLE_RETURN);
    };
    const root = hostRef.current;
    root?.addEventListener("pointerdown", take);
    root?.addEventListener("wheel", take, { passive: true });
    root?.addEventListener("touchstart", take, { passive: true });
    window.addEventListener("keydown", take);
    return () => {
      window.clearTimeout(timer);
      root?.removeEventListener("pointerdown", take);
      root?.removeEventListener("wheel", take);
      root?.removeEventListener("touchstart", take);
      window.removeEventListener("keydown", take);
    };
  }, [tourEligible]);

  /* the cursor walks one step per two beats — same single timebase */
  useEffect(() => {
    if (!tourOn) {
      setCursor(null);
      return;
    }
    const step = TOUR[tourStep % TOUR.length];
    // every other cycle the honest answer is empty — walk to the zero circle
    const zeroCycle = cycle % 2 === 1;
    const sel = zeroCycle && step.part === "evidence-selection" ? '[data-part="zero-hit-honesty"]' : `[data-part="${step.part}"]`;
    const host = machineRef.current;
    const el = host?.querySelector(sel);
    if (!host || !el) return;
    const h = host.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    setCursor({ x: r.left - h.left + r.width / 2 - 11, y: r.top - h.top + r.height / 2 - 11 });
  }, [tourOn, tourStep, beat, cycle]);

  useEffect(() => {
    if (!tourOn || !running) return;
    if (beat % 2 === 1) setTourStep((s) => s + 1);
  }, [beat, tourOn, running]);

  /* Esc reassembles the machine */
  useEffect(() => {
    if (!isolated) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseIsolate();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isolated, onCloseIsolate]);

  const liveBeat = reduced ? 3 : beat;
  const liveCycle = reduced ? 0 : cycle;

  return (
    <div ref={hostRef} className={`relative ${running ? "" : "stage-paused"}`}>
      {/* machine caption — slim (A1 + E4) */}
      <header className="mx-auto max-w-6xl px-5 pt-24 pb-6">
        <p className="kicker">LEARN · THE MACHINE</p>
        <h1 className="mt-3 max-w-[16em] font-display text-hero leading-[1.06] text-ink">
          {zh ? "一台可以拆开看的机器" : "A machine you can open up"}
        </h1>
        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3">
          <DepthRail lang={lang} at={isolated ? "station" : "stage"} />
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

      {/* ---------- the machine (desktop, one integrated drawing) ---------- */}
      <section aria-label={zh ? "管线机器" : "the machine"} className="mx-auto hidden max-w-6xl px-5 lg:block">
        <div
          ref={machineRef}
          className="machine relative rounded-2xl border border-line bg-bg1/40 p-4 pb-2"
          style={{ contain: "layout" }}
          onClick={() => {
            if (isolated) onCloseIsolate();
          }}
        >
          <p className="font-mono text-[12px] tracking-[0.08em] text-ink2">
            {zh ? "起点 · L1 指纹门" : "start · L1 fingerprint gate"}
          </p>
          {/* overlays live in a box that contains ONLY the svg, so %-positions
              resolve against the drawing itself */}
          <div className="relative">
            <MachineSVG lang={lang} beat={liveBeat} cycle={liveCycle} counts={counts} visitedCounts={visitedCounts} isolated={isolated} onOpen={onOpen} onOpenPart={onOpenPart} />

            {/* the protagonist rides its own lane above the belt (A2): filename →
                fingerprint → vector grid → parked in the index */}
            <div className={`pointer-events-none absolute inset-0 transition-opacity duration-500 ${isolated ? "opacity-20" : "opacity-100"}`} aria-hidden="true">
              <motion.span
                animate={{ left: BELT_LEFT[liveBeat >= 3 ? 3 : liveBeat] }}
                transition={{ type: "spring", stiffness: 210, damping: 28 }}
                className="absolute z-10"
                style={{ top: `${(BELT_MID / 470) * 100}%` }}
              >
                <span className={`flex -translate-y-1/2 items-center gap-2 rounded border px-2 py-0.5 font-mono text-[12px] transition-colors duration-500 ${liveBeat <= 2 ? "border-gold/70 bg-gold/15 text-ink" : "border-line-strong bg-bg2 text-sub"}`}>
                  <ProtagonistGlyph beat={liveBeat} />
                </span>
              </motion.span>
              {liveBeat >= 3 && (
                <span className="absolute right-2 -translate-y-1/2 rounded border border-teal/50 bg-cl-b/10 px-2 py-0.5 font-mono text-[12px] text-teal" style={{ top: `${(BELT_MID / 470) * 100}%` }}>
                  {zh ? "› 查询从这进" : "› queries enter here"}
                </span>
              )}
            </div>

            {/* belt chips ride just above the SVG lane (HTML overlay, % of drawing) */}
            <div className={`pointer-events-none absolute inset-x-0 transition-opacity duration-500 ${isolated ? "opacity-20" : "opacity-100"}`} style={{ top: `calc(${(BELT_TOP / 470) * 100}% - 34px)` }} aria-hidden="true">
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

            <TourCursor pos={cursor} on={tourOn} lang={lang} />
          </div>

          <MachineMeta lang={lang} />
        </div>

        <QARuler lang={lang} count={counts.rail} read={visitedCounts.rail} onOpen={onOpen} />
      </section>

      {/* ---------- mobile: vertical subway + teaching demos + tray ---------- */}
      <section aria-label={zh ? "管线机器" : "the machine"} className="mx-auto max-w-6xl px-5 lg:hidden">
        <div className="rounded-2xl border border-line bg-bg1/40 p-5">
          <p className="font-mono text-[12px] tracking-[0.08em] text-ink2">{zh ? "起点 · L1 指纹门" : "start · L1 gate"}</p>
          <ul className="mt-3 space-y-1 border-l border-line-strong pl-4">
            {STATIONS.filter((s) => s.id !== "rail").map((s) => (
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
          <QARuler lang={lang} count={counts.rail} read={visitedCounts.rail} onOpen={onOpen} />
        </div>
      </section>

      {/* the tray mounts ONCE (duplicate layoutIds crossfade to opacity 0) */}
      {tray}
    </div>
  );
}

/* demo tag + footnote, shortest honest form, pinned to the machine footer */
function MachineMeta({ lang }: { lang: Lang }) {
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 px-1 pb-2">
      <DemoTag lang={lang} />
      <DemoFootnote lang={lang} />
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

/* teardown tour cursor: a gold ring that walks the machine by itself */
function TourCursor({ pos, on, lang }: { pos: { x: number; y: number } | null; on: boolean; lang: Lang }) {
  return (
    <>
      {on && (
        <span className="tourchip absolute top-2 right-3 z-10" aria-hidden="true">
          <i />
        </span>
      )}
      {on && <span className="sr-only" aria-live="polite">{lang === "zh" ? "自动导览中，任意操作即可接管" : "auto tour — any input takes over"}</span>}
      {pos && on && (
        <motion.span
          className="tour-cursor pointer-events-none absolute z-10"
          initial={{ opacity: 0 }}
          animate={{ x: pos.x, y: pos.y, opacity: 1 }}
          transition={{ type: "spring", stiffness: 120, damping: 20 }}
          aria-hidden="true"
        />
      )}
    </>
  );
}

/* QA as a ruler under the machine (values + shortest notes, C3) */
function QARuler({
  lang, count, read, onOpen,
}: { lang: Lang; count: number; read: number; onOpen: (id: StationId | "l0") => void }) {
  const zh = lang === "zh";
  return (
    <div className="qa-ruler mt-3 rounded-xl border border-cl-b/40 bg-cl-b/5">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3">
        <button type="button" onClick={() => onOpen("rail")} className="group text-left">
          <span className="font-display text-lg leading-tight text-ink group-hover:text-gold">{zh ? "质检线" : "QA Line"}</span>
        </button>
        <p className="font-mono text-[12px] text-sub">
          {count} · <span className="text-gold">{read}</span> →
        </p>
      </div>
      <div className="grid grid-cols-1 gap-px overflow-hidden px-4 pb-3 pt-2 sm:grid-cols-3">
        <p className="rounded bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-xl text-gold">{SUITE.gatesPassed}/{SUITE.gatesTotal}</span>
          <span className="mt-0.5 block font-mono text-[12px] text-sub">{zh ? "质量门 · 两门已归档 → 首页" : "gates · fails archived → home"}</span>
        </p>
        <p className="rounded bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-xl text-gold">{SUITE.tests}</span>
          <span className="mt-0.5 block font-mono text-[12px] text-sub">{zh ? "测试 · 全量 8 分钟" : "tests · 8-min suite"}</span>
        </p>
        <p className="rounded bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-xl text-gold">{SUITE.bytesUploaded} B</span>
          <span className="mt-0.5 block font-mono text-[12px] text-sub">{zh ? "上传 · 无此代码路径" : "uploaded · no such path"}</span>
        </p>
      </div>
    </div>
  );
}

/* the station tray: role, teaching notes, pills, cards — under the machine */
export function StationTrayHeader({ lang, id, count }: { lang: Lang; id: StationId; count: number }) {
  const zh = lang === "zh";
  const meta = stationMeta(id);
  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
      <h2 className="font-display text-3xl leading-[1.1] text-ink">
        {pick(lang, meta.label_zh, meta.label_en)}
        <span className="ml-3 align-middle font-mono text-[13px] text-sub">
          {id} × {count}
        </span>
      </h2>
      <DepthRail lang={lang} at="station" />
      <p className="w-full text-[14px] text-ink2" style={{ maxWidth: "52ch" }}>
        {pick(lang, meta.role_zh, meta.role_en)}
      </p>
      {id === "understand" && (
        <p className="w-full font-mono text-[12px] leading-relaxed text-sub" style={{ maxWidth: "52ch" }}>
          {zh ? "有转录的音频走文字向量；无转录才走原始音频边车（逐行标记 asr_status）" : "audio with a transcript rides text vectors; raw-audio sidecar only without one (asr_status per row)"}
        </p>
      )}
      {id === "walk" && (
        <p className="w-full font-mono text-[12px] leading-relaxed text-sub" style={{ maxWidth: "52ch" }}>
          {zh ? "walk 门：iCloud dataless 跳过 · inode 移动检测" : "walk gates: iCloud dataless skip · inode move detection"}
        </p>
      )}
    </div>
  );
}
