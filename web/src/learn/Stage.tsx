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
 * (C3); zero visible text inside the machine beyond region names/counts and
 * the terminus caption (E1/V2).
 *
 * learn-copy-cut-001 (owner rule: cut taglines, helper text, secondary
 * labels) repeals C7: the locality claim lives in the QA tile (0 B · no such
 * code path, G7-required) and the site footer. The visible isolate
 * REGION_DESC paragraph is repealed too — the SVG <desc> keeps the narration
 * for screen readers.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion, useScroll, useSpring, useTransform } from "framer-motion";
import { useMotionPreference } from "../lib/use-motion-preference";
import { pick, type Lang } from "../lib/lang";
import { STATIONS, stationMeta, type StationId } from "../lib/learn-data";
import { SUITE } from "../lib/metrics";
import { SearchDemo } from "./station-visuals";
import { MachineSVG, BELT_MID } from "./machine-drawing";
import { REAL_RUN_META, REAL_RUN_LINES } from "./real-run";

export type StageProps = {
  lang: Lang;
  counts: Record<StationId, number>;
  visitedCounts: Record<StationId, number>;
  l0Count: number;
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
/* protagonist lane positions per beat (machine v2 region geometry) */
const BELT_LEFT = ["9%", "31%", "53%", "70%"];

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
  const { lang, counts, visitedCounts, l0Count, isolated, onOpen, onCloseIsolate, onOpenPart, tray } = props;
  const zh = lang === "zh";
  const reduced = useMotionPreference();
  const hostRef = useRef<HTMLDivElement>(null);
  const machineRef = useRef<HTMLDivElement>(null);
  const [beat, setBeat] = useState(0);
  const [cycle, setCycle] = useState(0);
  const [inView, setInView] = useState(false);
  const [tabVisible, setTabVisible] = useState(true);
  const [desktop, setDesktop] = useState(false);
  const [tourOn, setTourOn] = useState(false);
  const [userPaused, setUserPaused] = useState(false);
  const [tourStep, setTourStep] = useState(0);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const running = inView && tabVisible && !userPaused && !reduced;
  /* signature move (amendments-006 R3): the chassis sits up as it enters the view */
  const tiltRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({ target: tiltRef, offset: ["start end", "start 0.35"] });
  const tiltRaw = useTransform(scrollYProgress, [0, 1], [6, 0]);
  const tilt = useSpring(tiltRaw, { stiffness: 120, damping: 26 });

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
  const tourEligible = !reduced && desktop && isolated === null && !userPaused;

  /* any input takes over; quiet hands it back */
  useEffect(() => {
    if (!tourEligible) {
      setTourOn(false);
      return;
    }
    let timer = window.setTimeout(() => setTourOn(true), TOUR_FIRST_DELAY);
    const take = (e?: Event) => {
      if (e && (e.target as HTMLElement)?.closest?.('[data-tour-btn]')) return;
      setTourOn(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setTourOn(true), TOUR_IDLE_RETURN);
    };
    const root = hostRef.current;
    root?.addEventListener("pointerdown", take);
    root?.addEventListener("wheel", take, { passive: true });
    root?.addEventListener("touchstart", take, { passive: true });
    const onKey = (e: KeyboardEvent) => { if (!e.metaKey && e.key !== "Escape") take(e); };
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      root?.removeEventListener("pointerdown", take);
      root?.removeEventListener("wheel", take);
      root?.removeEventListener("touchstart", take);
      window.removeEventListener("keydown", onKey);
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
      {/* machine caption — the heading names it; the L0 button is the only control */}
      <header className="mx-auto max-w-6xl px-5 pt-24 pb-6">
        <h1 className="max-w-[16em] font-display text-hero leading-[1.06] text-ink">
          {zh ? "一台可以拆开看的机器" : "A machine you can open up"}
        </h1>
        <div className="mt-4">
          <button
            type="button"
            onClick={() => onOpen("l0")}
            className="rounded-full border border-line px-4 py-1.5 font-mono text-[14px] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
          >
            L0 · {l0Count} →
          </button>
        </div>
      </header>

      {/* ---------- the machine (desktop, one integrated drawing) ---------- */}
      <section aria-label={zh ? "管线机器" : "the machine"} className="mx-auto hidden max-w-6xl px-5 lg:block">
        <div ref={tiltRef} style={{ perspective: 1400 }}>
        <motion.div
          ref={machineRef}
          className={`machine relative rounded-2xl bg-bg1/40 p-4 pb-2 ${tourOn ? "touring" : ""}`}
          style={{ contain: "layout", rotateX: reduced ? 0 : tilt, transformOrigin: "top center", transformPerspective: 1400 }}
          onClick={() => {
            if (isolated) onCloseIsolate();
          }}
        >
          <div className="flex items-center justify-end gap-3">
            <button
              type="button"
              data-tour-btn
              aria-pressed={userPaused}
              disabled={!!reduced}
              onClick={(e) => { e.stopPropagation(); setUserPaused((p) => !p); }}
              className="rounded-full border border-line px-3 py-1 font-mono text-[13px] text-ink2 transition-colors hover:border-gold/60 hover:text-gold disabled:opacity-40"
            >
              {reduced ? (zh ? "静态模式" : "reduced motion") : userPaused ? (zh ? "▶ 播放" : "▶ play") : (zh ? "Ⅱ 暂停" : "Ⅱ pause")}
            </button>
            <button
              type="button"
              data-tour-btn
              onClick={() => {
                setUserPaused(false);
                setTourOn(true);
                setTourStep(0);
              }}
              className="rounded-full border border-line px-3 py-1 font-mono text-[13px] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
            >
              {zh ? "▶ 看它自己走一遍" : "▶ take the tour"}
            </button>
          </div>
          {/* overlays live in a box that contains ONLY the svg, so %-positions
              resolve against the drawing itself */}
          <div className="relative">
            <MachineSVG lang={lang} beat={liveBeat} cycle={liveCycle} counts={counts} visitedCounts={visitedCounts} isolated={isolated} onOpen={onOpen} onOpenPart={onOpenPart} />

            {/* the protagonist rides its own lane above the belt (A2): filename →
                fingerprint → vector grid → parked in the index */}
            <div className={`pointer-events-none absolute inset-0 transition-opacity duration-500 ${isolated ? "opacity-20" : "opacity-100"}`} aria-hidden="true">
              <motion.span
                animate={{ left: BELT_LEFT[liveBeat >= 3 ? 3 : liveBeat] }}
                transition={reduced ? { duration: 0 } : { type: "spring", stiffness: 210, damping: 28 }}
                className="absolute z-10"
                style={{ top: `${(BELT_MID / 470) * 100}%` }}
              >
                <span className={`flex -translate-y-1/2 items-center gap-2 rounded border px-2 py-0.5 font-mono text-[13px] transition-colors duration-500 ${liveBeat <= 2 ? "border-gold/70 bg-gold/15 text-ink" : "border-line-strong bg-bg2 text-sub"}`}>
                  <ProtagonistGlyph beat={liveBeat} />
                </span>
              </motion.span>
            </div>


            <TourCursor pos={cursor} on={tourOn} lang={lang} />
          </div>

          {isolated && isolated !== "rail" && (
            <div className="flex justify-end px-1 pb-2">
              <button
                type="button"
                onClick={onCloseIsolate}
                className="rounded-full border border-gold/50 px-3 py-1 font-mono text-[13px] text-gold transition-colors hover:border-gold"
              >
                {zh ? "× 返回整台机器" : "× back to the machine"}
              </button>
            </div>
          )}

          <MachineMeta lang={lang} />
        </motion.div>
        </div>

        <RealRunStrip lang={lang} />

        <QARuler lang={lang} onOpen={onOpen} />
      </section>

      {/* ---------- mobile: vertical subway + teaching demos + tray ---------- */}
      <section aria-label={zh ? "管线机器" : "the machine"} className="mx-auto max-w-6xl px-5 lg:hidden">
        <div className="rounded-2xl bg-bg1/40 p-5">
          <ul className="space-y-1 border-l border-line-strong pl-4">
            {STATIONS.filter((s) => s.id !== "rail").map((s) => (
              <li key={s.id} className="relative">
                <span aria-hidden="true" className="absolute top-1/2 -left-[21px] h-2.5 w-2.5 -translate-y-1/2 rounded-full border border-line-strong bg-bg1" />
                <button
                  type="button"
                  onClick={() => onOpen(s.id)}
                  className="flex w-full items-baseline rounded-lg px-2 py-2 text-left transition-colors hover:bg-bg2/60"
                >
                  <span className="font-display text-xl text-ink">{pick(lang, s.label_zh, s.label_en)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div className="mt-3 rounded-2xl bg-bg1/40 p-3">
          <SearchDemo beat={liveBeat} cycle={liveCycle} lang={lang} onPart={onOpenPart} />
        </div>
        <div className="mt-3">
          <QARuler lang={lang} onOpen={onOpen} />
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
    <p className="px-1 pb-2 font-mono text-[14px] text-sub">
      {pick(lang, "示意：固定示例数据，非实时运行", "demo: fixed sample data, not a live run")}
    </p>
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
function QARuler({ lang, onOpen }: { lang: Lang; onOpen: (id: StationId | "l0") => void }) {
  const zh = lang === "zh";
  return (
    <div className="qa-ruler mt-3 rounded-xl bg-cl-b/5">
      <div className="flex flex-wrap items-center px-4 pt-3">
        <button type="button" onClick={() => onOpen("rail")} className="group text-left">
          <span className="font-display text-lg leading-tight text-ink group-hover:text-gold">{zh ? "质检线" : "QA Line"}</span>
        </button>
      </div>
      <div className="grid grid-cols-1 gap-px overflow-hidden px-4 pb-3 pt-2 sm:grid-cols-3">
        <p className="rounded bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-xl text-ink">{SUITE.gatesPassed} / {SUITE.gatesTotal}</span>
          <span className="mt-0.5 block font-mono text-[14px] leading-snug text-sub">{zh ? "2026-10-26开发集 · 2项未过" : "2026-10-26 development · 2 failed"}</span>
        </p>
        <p className="rounded bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-xl text-ink">{SUITE.tests}</span>
          <span className="mt-0.5 block font-mono text-[14px] leading-snug text-sub">{pick(lang, SUITE.testsNote.zh, SUITE.testsNote.en)}</span>
        </p>
        <p className="rounded bg-bg1/70 px-3 py-2 text-center">
          <span className="font-display text-xl text-ink">{SUITE.bytesUploaded} B</span>
          <span className="mt-0.5 block font-mono text-[14px] leading-snug text-sub">{zh ? "本机建库与检索的文件上传；MCP另向调用方返回证据" : "Local indexing/search file uploads; MCP returns evidence to its caller"}</span>
        </p>
      </div>
    </div>
  );
}

/* amendments-006 R1: the machine, really running — verbatim CLI output from
   the repo's own frozen eval library (read-only); provenance in real-run.ts */
function RealRunStrip({ lang }: { lang: Lang }) {
  const [l0, l1, l2] = REAL_RUN_LINES;
  const parts = l1.split(/(score \S+|z \S+|cov \S+)/g).filter(Boolean);
  return (
    <div className="real-run mt-3 rounded-xl bg-bg0/70 p-4">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <p className="font-mono text-[14px] text-ink">
          <span className="text-gold" aria-hidden="true">▌</span> {pick(lang, REAL_RUN_META.note_zh, REAL_RUN_META.note_en)}
        </p>
        <p className="font-mono text-[13px] text-sub" title={lang === "zh" ? "本页数字来自这条真实命令" : "the numbers on this page come from this real command"}>
          {REAL_RUN_META.command}
        </p>
      </div>
      <div className="mt-2 space-y-0.5 font-mono text-[13.5px] leading-relaxed">
        <p className="text-sub">{l0}</p>
        <p className="text-ink2">
          {parts.map((p, i) =>
            /^(score|z|cov)/.test(p) ? (
              <span key={i} className="text-teal">{p}</span>
            ) : (
              <span key={i}>{p}</span>
            ),
          )}
        </p>
        <p className="whitespace-pre-wrap break-all text-sub">{l2}</p>
      </div>
    </div>
  );
}

/* the station tray header: name + station pager, pills and cards live below */
export function StationTrayHeader({ lang, id, onOpen }: { lang: Lang; id: StationId; onOpen: (id: StationId | "l0") => void }) {
  const meta = stationMeta(id);
  const order: StationId[] = ["walk", "identity", "understand", "search", "rail"];
  const i = order.indexOf(id);
  const prevId = i > 0 ? order[i - 1] : null;
  const nextId = i < order.length - 1 ? order[i + 1] : null;
  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
      <h2 className="font-display text-3xl leading-[1.1] text-ink">
        {pick(lang, meta.label_zh, meta.label_en)}
      </h2>
      <span className="flex items-center gap-3 font-mono text-[14px]">
        {prevId && (
          <button type="button" onClick={() => onOpen(prevId)} className="text-sub transition-colors hover:text-gold">
            ‹ {pick(lang, stationMeta(prevId).label_zh, stationMeta(prevId).label_en)}
          </button>
        )}
        {nextId && (
          <button type="button" onClick={() => onOpen(nextId)} className="text-sub transition-colors hover:text-gold">
            {pick(lang, stationMeta(nextId).label_zh, stationMeta(nextId).label_en)} ›
          </button>
        )}
      </span>
    </div>
  );
}
