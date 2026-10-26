import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  clusterColor,
  layerBadge,
  readingMinutes,
  type Concept,
  type ClusterMeta,
  type LearnData,
} from "../lib/learn-data";
import { pick, type Lang } from "../lib/lang";
import { CopyButton } from "../components/CopyButton";
import { IconClose } from "../components/Icons";

type Props = {
  c: Concept;
  cluster?: ClusterMeta;
  lang: Lang;
  data: Extract<LearnData, { ok: true }>;
  visited: boolean;
  prev?: Concept;
  next?: Concept;
  onToggleVisited: (id: string) => void;
  onClose: () => void;
  onNavigate: (id: string) => void;
  onPrev: () => void;
  onNext: () => void;
};

type SecMeta = { id: string; zh: string; en: string };

export function LessonDrawer({
  c,
  cluster,
  lang,
  data,
  visited,
  prev,
  next,
  onToggleVisited,
  onClose,
  onNavigate,
  onPrev,
  onNext,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  // The morph id is frozen for the drawer's whole open session: navigating to
  // another concept must not hand the layoutId stack back to a remounting card
  // (that crossfaded the drawer to invisible — the bug this ref fixes).
  const morphIdRef = useRef(`c-${c.id}`);
  const [active, setActive] = useState(0);
  const readMarked = useRef(false);

  const title = pick(lang, c.title_zh, c.title_en);
  const color = cluster ? clusterColor(cluster.token) : "var(--color-gold)";

  const sections = useMemo<SecMeta[]>(() => {
    const s: SecMeta[] = [{ id: "ov", zh: "概览", en: "Overview" }];
    if (c.def_zh || c.def_en) s.push({ id: "def", zh: "定义", en: "Definition" });
    if (c.plain_zh || c.plain_en) s.push({ id: "plain", zh: "大白话", en: "In plain words" });
    if (c.example_zh || c.example_en) s.push({ id: "ex", zh: "实例", en: "Example" });
    if (c.refs.length) s.push({ id: "refs", zh: "真实代码", en: "Real code" });
    return s;
  }, [c]);

  const { children, related } = useMemo(() => {
    const layerOf = (id: string) => data.byId.get(id)?.layer;
    const kids: Concept[] = [];
    const rel: Concept[] = [];
    for (const d of c.deep) {
      const target = data.byId.get(d.to);
      if (!target) continue;
      if (layerOf(d.to) !== undefined && layerOf(d.to)! < c.layer) kids.push(target);
      else rel.push(target);
    }
    if (!kids.length && c.parent) {
      const p = data.childrenOf.get(c.parent);
      if (p) for (const id of p) if (id !== c.id) kids.push(data.byId.get(id)!);
    }
    return { children: kids, related: rel };
  }, [c, data]);

  // mount-only: capture opener, initial focus, scroll lock
  useEffect(() => {
    openerRef.current = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const html = document.documentElement;
    const prevOverflow = html.style.overflow;
    html.style.overflow = "hidden";
    return () => {
      html.style.overflow = prevOverflow;
      openerRef.current?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // keyboard: Esc, prev/next, focus trap (repairs focus that leaves the panel)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "ArrowLeft" && prev) onPrev();
      if (e.key === "ArrowRight" && next) onNext();
      if (e.key === "Tab") {
        const panel = panelRef.current;
        if (!panel) return;
        const focusables = panel.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (!panel.contains(document.activeElement)) {
          e.preventDefault();
          (e.shiftKey ? last : first).focus();
          return;
        }
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, onPrev, onNext, prev, next]);

  // scroll-spy + auto read-mark on the lesson scroller
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    let raf = 0;
    const onScroll = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const elTop = el.getBoundingClientRect().top;
        let idx = 0;
        sections.forEach((s, i) => {
          const target = el.querySelector<HTMLElement>(`[data-sec="${s.id}"]`);
          if (target && target.getBoundingClientRect().top - elTop <= 120) idx = i;
        });
        setActive(idx);
        const ratio = el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight);
        if (ratio > 0.65 && !readMarked.current && !visited) {
          readMarked.current = true;
          onToggleVisited(c.id);
        }
      });
    };
    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      el.removeEventListener("scroll", onScroll);
    };
  }, [c.id, sections, visited, onToggleVisited]);

  // reset per-concept state when navigating inside an open drawer
  useEffect(() => {
    readMarked.current = false;
    scrollRef.current?.scrollTo({ top: 0 });
    setActive(0);
  }, [c.id]);

  const renderSec = (meta: SecMeta, body: React.ReactNode) => (
    <section data-sec={meta.id} className="scroll-mt-6">
      <h3 className="mb-3 flex items-center gap-3 font-mono text-[10px] tracking-[0.16em] text-gold uppercase">
        {pick(lang, meta.zh, meta.en)}
        <span className="h-px flex-1 bg-line" />
      </h3>
      {body}
    </section>
  );

  return (
    // data-lenis-prevent: the lesson scroller keeps native wheel while Lenis
    // owns the page behind the backdrop
    <div className="fixed inset-0 z-[95]" role="dialog" aria-modal="true" aria-label={title} data-lesson={c.id} data-lenis-prevent="">
      <motion.div
        className="absolute inset-0 bg-bg0/75 backdrop-blur-sm"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
      />
      <motion.div
        ref={panelRef}
        layoutId={morphIdRef.current}
        tabIndex={-1}
        transition={{ type: "spring", stiffness: 300, damping: 34 }}
        className="absolute inset-2 flex flex-col overflow-hidden rounded-2xl border border-line-strong bg-bg1 shadow-[0_24px_80px_rgba(0,0,0,0.6)] outline-none lg:inset-x-[max(1.5rem,calc(50%-36rem))] lg:inset-y-[4vh]"
      >
        {/* header */}
        <div className="hairline-b flex items-center justify-between gap-4 px-5 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <span
              className="inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[10px] tracking-[0.1em] uppercase"
              style={{ color, borderColor: color }}
            >
              {cluster ? `${cluster.token} · ${pick(lang, cluster.label_zh, cluster.label_en)}` : c.cluster}
            </span>
            <span className="truncate font-mono text-[10px] tracking-[0.12em] text-sub uppercase">
              {layerBadge(c.layer, lang)} · ≈{readingMinutes(c, lang)} min
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => onToggleVisited(c.id)}
              aria-pressed={visited}
              className={`rounded-full border px-3 py-1 font-mono text-[10px] tracking-[0.1em] uppercase transition-colors ${
                visited ? "border-gold bg-gold/10 text-gold" : "border-line text-sub hover:text-ink"
              }`}
            >
              {visited ? (lang === "zh" ? "✓ 已读" : "✓ READ") : lang === "zh" ? "标记已读" : "mark read"}
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label={lang === "zh" ? "关闭" : "close"}
              className="rounded-full border border-line p-1.5 text-ink2 transition-colors hover:border-gold/50 hover:text-gold"
            >
              <IconClose size={14} />
            </button>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          {/* lesson scroller */}
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { delay: 0.12, duration: 0.3 } }}
              className="max-w-[46em]"
            >
              {c.parent && data.byId.get(c.parent) && (
                <button
                  type="button"
                  onClick={() => onNavigate(c.parent!)}
                  className="mb-4 inline-flex items-center gap-1.5 font-mono text-[11px] text-sub transition-colors hover:text-gold"
                >
                  ↑ {pick(lang, data.byId.get(c.parent)!.title_zh, data.byId.get(c.parent)!.title_en)}
                </button>
              )}
              <h2 className="font-display text-3xl leading-tight text-ink sm:text-4xl">{title}</h2>
              <p className="mt-1 font-sans text-[12px] tracking-wide text-sub">
                {pick(lang, c.title_en, c.title_zh)}
              </p>

              <div className="mt-7 space-y-8">
                {renderSec(
                  sections[0],
                  <>
                    <p className="text-[15px] leading-relaxed text-ink2">{pick(lang, c.one_zh, c.one_en)}</p>
                    {(c.analogy_zh || c.analogy_en) && (
                      <p className="mt-3 rounded-xl border border-line bg-bg2/60 p-4 text-[13px] leading-relaxed text-ink2">
                        <span className="mr-2 font-mono text-[10px] tracking-[0.14em] text-teal uppercase">
                          {lang === "zh" ? "比喻" : "ANALOGY"}
                        </span>
                        {pick(lang, c.analogy_zh, c.analogy_en)}
                      </p>
                    )}
                  </>,
                )}

                {(c.def_zh || c.def_en) &&
                  renderSec(
                    sections[1],
                    <p className="text-[15px] leading-relaxed text-ink">{pick(lang, c.def_zh, c.def_en)}</p>,
                  )}

                {(c.plain_zh || c.plain_en) &&
                  renderSec(
                    sections[2],
                    <p className="text-[15px] leading-relaxed text-ink2">{pick(lang, c.plain_zh, c.plain_en)}</p>,
                  )}

                {(c.example_zh || c.example_en) &&
                  renderSec(
                    sections[3],
                    <p className="text-[15px] leading-relaxed text-ink2">{pick(lang, c.example_zh, c.example_en)}</p>,
                  )}

                {c.refs.length > 0 &&
                  renderSec(
                    sections[4],
                    <ul className="space-y-2">
                      {c.refs.slice(0, 5).map((r, i) => (
                        <li
                          key={`${r.path}-${i}`}
                          className="flex items-center justify-between gap-3 rounded-lg border border-line bg-bg2/60 px-3 py-2"
                        >
                          <code className="min-w-0 truncate font-mono text-[11px] text-teal">
                            {r.path}
                            {r.lines ? `:${r.lines}` : ""}
                            {r.note ? <span className="ml-2 text-sub">— {r.note}</span> : null}
                          </code>
                          <CopyButton text={`${r.path}${r.lines ? `:${r.lines}` : ""}`} label={{ zh: "复制", en: "copy" }} />
                        </li>
                      ))}
                    </ul>,
                  )}
              </div>

              {/* pager */}
              <div className="mt-10 flex items-center justify-between gap-3 border-t border-line pt-5">
                <button
                  type="button"
                  onClick={onPrev}
                  disabled={!prev}
                  className="max-w-[45%] truncate rounded-lg border border-line px-3 py-2 text-left font-mono text-[11px] text-ink2 transition-colors enabled:hover:border-gold/50 enabled:hover:text-gold disabled:opacity-30"
                >
                  ← {prev ? pick(lang, prev.title_zh, prev.title_en) : lang === "zh" ? "没有上一个" : "none"}
                </button>
                <button
                  type="button"
                  onClick={onNext}
                  disabled={!next}
                  className="max-w-[45%] truncate rounded-lg border border-line px-3 py-2 text-right font-mono text-[11px] text-ink2 transition-colors enabled:hover:border-gold/50 enabled:hover:text-gold disabled:opacity-30"
                >
                  {next ? pick(lang, next.title_zh, next.title_en) : lang === "zh" ? "没有下一个" : "none"} →
                </button>
              </div>
            </motion.div>
          </div>

          {/* sidebar: TOC + scroll-spy rail + graph links */}
          <aside className="hairline-t max-h-[38%] shrink-0 overflow-y-auto border-line px-5 py-6 lg:max-h-none lg:w-80 lg:border-t-0 lg:border-l">
            <div className="mb-6 flex gap-4">
              <div className="relative w-px shrink-0 bg-line" aria-hidden="true">
                <span
                  className="absolute left-0 top-0 w-px bg-gold transition-[height] duration-500"
                  style={{ height: `${((active + 1) / sections.length) * 100}%` }}
                />
              </div>
              <nav aria-label={lang === "zh" ? "本页目录" : "on this page"}>
                <p className="mb-2 font-mono text-[10px] tracking-[0.16em] text-sub uppercase">
                  {lang === "zh" ? "目录" : "CONTENTS"}
                </p>
                <ul className="space-y-1.5">
                  {sections.map((s, i) => (
                    <li key={s.id}>
                      <button
                        type="button"
                        onClick={() => {
                          const el = scrollRef.current?.querySelector<HTMLElement>(`[data-sec="${s.id}"]`);
                          el?.scrollIntoView({ behavior: "smooth", block: "start" });
                        }}
                        aria-current={active === i ? "true" : undefined}
                        className={`font-mono text-[11px] transition-colors ${
                          active === i ? "text-gold" : "text-sub hover:text-ink"
                        }`}
                      >
                        {String(i + 1).padStart(2, "0")} · {pick(lang, s.zh, s.en)}
                      </button>
                    </li>
                  ))}
                </ul>
              </nav>
            </div>

            {children.length > 0 && (
              <div className="mb-6">
                <p className="mb-2 font-mono text-[10px] tracking-[0.16em] text-sub uppercase">
                  {lang === "zh" ? "深入下探" : "DRILL DEEPER"}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {children.map((k) => (
                    <button
                      key={k.id}
                      type="button"
                      onClick={() => onNavigate(k.id)}
                      className="rounded-full border border-line px-2.5 py-1 font-mono text-[10px] text-ink2 transition-colors hover:border-gold/50 hover:text-gold"
                    >
                      ↓ {pick(lang, k.title_zh, k.title_en)} · L{k.layer}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {related.length > 0 && (
              <div className="mb-6">
                <p className="mb-2 font-mono text-[10px] tracking-[0.16em] text-sub uppercase">
                  {lang === "zh" ? "相关概念" : "RELATED"}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {related.map((k) => (
                    <button
                      key={k.id}
                      type="button"
                      onClick={() => onNavigate(k.id)}
                      className="rounded-full border border-line px-2.5 py-1 font-mono text-[10px] text-ink2 transition-colors hover:border-teal/60 hover:text-teal"
                    >
                      ↔ {pick(lang, k.title_zh, k.title_en)}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <p className="font-mono text-[10px] leading-relaxed text-sub">
              {lang === "zh" ? "内容为项目真实概念体系 · 出处可审计" : "Real concept system · auditable sources"}
            </p>
          </aside>
        </div>
      </motion.div>
    </div>
  );
}
