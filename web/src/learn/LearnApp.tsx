import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Nav } from "../components/Nav";
import { Footer } from "../components/Footer";
import { ConceptCard } from "./ConceptCard";
import { LessonDrawer } from "./LessonDrawer";
import { useLang, pick } from "../lib/lang";
import {
  clusterColor,
  layerBadge,
  loadVisited,
  saveVisited,
  type LearnData,
} from "../lib/learn-data";
import { IconSearch, IconClose } from "../components/Icons";

type Ok = Extract<LearnData, { ok: true }>;

export function LearnApp({ data }: { data: Ok }) {
  const lang = useLang();
  const zh = lang === "zh";
  const { graph, byId } = data;

  const [query, setQuery] = useState("");
  const [cluster, setCluster] = useState<string>("all");
  const [layer, setLayer] = useState<number | "all">("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // which card the drawer expanded from — its slot stays reserved (invisible,
  // exact height) until the drawer closes, so the morph has a stable anchor
  const [openedId, setOpenedId] = useState<string | null>(null);
  const [visited, setVisited] = useState<Set<string>>(() => loadVisited());
  const searchRef = useRef<HTMLInputElement>(null);

  const tokenOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of graph.clusters) m.set(c.id, c.token);
    return m;
  }, [graph.clusters]);

  const sortedClusters = useMemo(
    () => [...graph.clusters].sort((a, b) => a.token.localeCompare(b.token)),
    [graph.clusters],
  );

  const total = graph.nodes.length;

  const toggleVisited = useCallback((id: string) => {
    setVisited((prevSet) => {
      const nextSet = new Set(prevSet);
      if (nextSet.has(id)) nextSet.delete(id);
      else nextSet.add(id);
      saveVisited(nextSet);
      return nextSet;
    });
  }, []);

  const handleOpen = useCallback((id: string) => {
    setOpenedId(id);
    setSelectedId(id);
  }, []);
  const handleClose = useCallback(() => {
    setSelectedId(null);
    setOpenedId(null);
  }, []);
  const handleNavigate = useCallback((id: string) => setSelectedId(id), []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = graph.nodes.filter((n) => {
      if (cluster !== "all" && n.cluster !== cluster) return false;
      if (layer !== "all" && n.layer !== layer) return false;
      if (q) {
        const cl = graph.clusters.find((c) => c.id === n.cluster);
        const hay = `${n.title_zh} ${n.title_en} ${n.one_zh} ${n.one_en} ${cl?.label_zh ?? ""} ${cl?.label_en ?? ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    out.sort((a, b) =>
      (tokenOf.get(a.cluster) ?? "Z").localeCompare(tokenOf.get(b.cluster) ?? "Z") || a.layer - b.layer || a.id.localeCompare(b.id),
    );
    return out;
  }, [graph, query, cluster, layer, tokenOf]);

  const selected = selectedId ? byId.get(selectedId) : undefined;
  const selectedIdx = selected ? filtered.findIndex((n) => n.id === selected.id) : -1;
  const prev = selectedIdx > 0 ? filtered[selectedIdx - 1] : undefined;
  const next = selectedIdx >= 0 && selectedIdx < filtered.length - 1 ? filtered[selectedIdx + 1] : undefined;
  const handlePrev = useCallback(() => {
    setSelectedId((cur) => {
      const idx = cur ? filtered.findIndex((n) => n.id === cur) : -1;
      return idx > 0 ? filtered[idx - 1].id : cur;
    });
  }, [filtered]);
  const handleNext = useCallback(() => {
    setSelectedId((cur) => {
      const idx = cur ? filtered.findIndex((n) => n.id === cur) : -1;
      return idx >= 0 && idx < filtered.length - 1 ? filtered[idx + 1].id : cur;
    });
  }, [filtered]);

  // "/" focuses search (when no drawer is open)
  useEffect(() => {
    // healthy boot: the baked sr-only <h1> (no-JS/crawler path) would duplicate ours
    document.querySelector("h1[data-zh]")?.remove();
    const onKey = (e: KeyboardEvent) => {
      if (selectedId) return;
      const t = e.target as HTMLElement;
      if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey && t.tagName !== "INPUT" && t.tagName !== "TEXTAREA" && !t.isContentEditable) {
        e.preventDefault();
        searchRef.current?.focus();
      }
      if (e.key === "Escape" && t === searchRef.current) {
        setQuery("");
        searchRef.current?.blur();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selectedId]);

  const visitedCount = useMemo(() => graph.nodes.filter((n) => visited.has(n.id)).length, [graph.nodes, visited]);

  return (
    <>
      <a
        href="#grid"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:bg-bg3 focus:px-4 focus:py-2 focus:text-ink"
      >
        {zh ? "跳到概念网格" : "Skip to the grid"}
      </a>
      <Nav page="learn" />

      <header className="mx-auto max-w-6xl px-5 pt-28 pb-8">
        <p className="kicker">LEARN · CONCEPT MAP</p>
        <h1 className="mt-3 max-w-[14em] font-display text-display leading-[1.08] text-ink">
          {zh ? `${total} 个真实概念，四层逐层下探` : `${total} real concepts, four layers deep`}
        </h1>
        <p className="mt-4 max-w-[46em] text-[15px] leading-relaxed text-ink2">
          {zh
            ? "从机制层到基础层：每张卡都有定义、大白话、实例和真实代码出处。按簇筛选、按层进阶；读过的概念会被记住，进度条就是你自己的。"
            : "From mechanisms to fundamentals: every card carries a definition, plain words, an example and real code refs. Filter by cluster, advance by layer; what you read is remembered — the progress bar is yours."}
        </p>

        <div className="mt-6 flex items-center gap-4">
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-bg3" role="progressbar" aria-valuenow={visitedCount} aria-valuemin={0} aria-valuemax={total} aria-label={zh ? "学习进度" : "study progress"}>
            <motion.div
              className="h-full rounded-full bg-gradient-to-r from-gold to-goldbright"
              initial={false}
              animate={{ width: `${(visitedCount / total) * 100}%` }}
              transition={{ type: "spring", stiffness: 120, damping: 20 }}
            />
          </div>
          <span className="font-mono text-[11px] whitespace-nowrap text-sub">
            <span className="text-gold">{visitedCount}</span> / {total} {zh ? "已读" : "read"}
          </span>
        </div>
      </header>

      {/* sticky filter toolbar */}
      <div className="sticky top-14 z-40 glass">
        <div className="mx-auto max-w-6xl px-5 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative min-w-[14rem] flex-1 sm:max-w-md">
              <IconSearch size={14} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sub" />
              <input
                ref={searchRef}
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={zh ? "搜概念、别名、簇……（按 / 聚焦）" : "Search concepts, aliases, clusters… ( / to focus )"}
                aria-label={zh ? "搜索概念" : "Search concepts"}
                className="w-full rounded-full border border-line bg-bg1/80 py-2 pr-9 pl-9 font-mono text-[12px] text-ink placeholder:text-sub focus:border-gold/60"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery("")}
                  aria-label={zh ? "清除搜索" : "clear search"}
                  className="absolute top-1/2 right-3 -translate-y-1/2 text-sub hover:text-gold"
                >
                  <IconClose size={12} />
                </button>
              )}
            </div>
            <p className="font-mono text-[11px] text-sub" aria-live="polite">
              <span className="sr-only">{zh ? "当前在列：" : "concepts shown: "}</span>
              {filtered.length} / {total}
            </p>
          </div>

          <div
            className="no-scrollbar hscroll mt-2.5 flex items-center gap-2 overflow-x-auto pb-1"
            role="group"
            aria-label={zh ? "按簇筛选" : "filter by cluster"}
          >
            <Pill active={cluster === "all"} onClick={() => setCluster("all")} layoutId="cluster-pill" label={zh ? "全部" : "All"} />
            {sortedClusters.map((c) => (
              <Pill
                key={c.id}
                active={cluster === c.id}
                onClick={() => setCluster(cluster === c.id ? "all" : c.id)}
                layoutId="cluster-pill"
                label={`${c.token} · ${pick(lang, c.label_zh, c.label_en)}`}
                dot={clusterColor(c.token)}
              />
            ))}
          </div>

          <div
            className="no-scrollbar hscroll mt-1.5 flex items-center gap-2 overflow-x-auto pb-1"
            role="group"
            aria-label={zh ? "按层筛选" : "filter by layer"}
          >
            <Pill small active={layer === "all"} onClick={() => setLayer("all")} layoutId="layer-pill" label={zh ? "所有层" : "All layers"} />
            {graph.layers.map((l) => (
              <Pill
                key={l.n}
                small
                active={layer === l.n}
                onClick={() => setLayer(layer === l.n ? "all" : l.n)}
                layoutId="layer-pill"
                label={layerBadge(l.n, lang)}
                title={pick(lang, l.blurb_zh, l.blurb_en)}
              />
            ))}
          </div>
        </div>
      </div>

      <main id="grid" className="mx-auto max-w-6xl px-5 py-10">
        {filtered.length === 0 ? (
          <div className="rounded-2xl border border-line bg-bg1/60 p-12 text-center">
            <p className="font-display text-2xl text-ink">{zh ? "这个组合没有概念" : "No concepts match this combination"}</p>
            <p className="mt-2 font-mono text-[12px] text-sub">
              {zh ? "换一个词，或者" : "Try another term, or "}
              <button
                type="button"
                className="text-gold underline underline-offset-4"
                onClick={() => {
                  setQuery("");
                  setCluster("all");
                  setLayer("all");
                }}
              >
                {zh ? "清空筛选" : "clear filters"}
              </button>
            </p>
          </div>
        ) : (
          <motion.div layout className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <AnimatePresence mode="popLayout" initial={false}>
              {filtered.map((c) => (
                <ConceptCard
                  key={c.id}
                  c={c}
                  cluster={graph.clusters.find((cl) => cl.id === c.cluster)}
                  lang={lang}
                  visited={visited.has(c.id)}
                  dimmed={openedId === c.id}
                  onOpen={handleOpen}
                />
              ))}
            </AnimatePresence>
          </motion.div>
        )}
      </main>

      <Footer />

      <AnimatePresence>
        {selected && (
          <LessonDrawer
            key="lesson"
            c={selected}
            cluster={graph.clusters.find((cl) => cl.id === selected.cluster)}
            lang={lang}
            data={data}
            visited={visited.has(selected.id)}
            prev={prev}
            next={next}
            onToggleVisited={toggleVisited}
            onClose={handleClose}
            onNavigate={handleNavigate}
            onPrev={handlePrev}
            onNext={handleNext}
          />
        )}
      </AnimatePresence>
    </>
  );
}

function Pill({
  active,
  onClick,
  label,
  layoutId,
  dot,
  title,
  small,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  layoutId: string;
  dot?: string;
  title?: string;
  small?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      title={title}
      className={`relative shrink-0 rounded-full border transition-colors ${
        small ? "px-2.5 py-0.5 text-[10px]" : "px-3.5 py-1 text-[11px]"
      } font-mono tracking-[0.06em] ${active ? "text-bg0" : "border-line text-ink2 hover:border-gold/40 hover:text-ink"}`}
    >
      {active && (
        <motion.span
          layoutId={layoutId}
          className="absolute inset-0 rounded-full bg-gold"
          transition={{ type: "spring", stiffness: 400, damping: 34 }}
        />
      )}
      <span className={`relative z-10 inline-flex items-center gap-1.5 ${!active && "text-inherit"}`}>
        {dot && <span className="h-1.5 w-1.5 rounded-full" style={{ background: dot }} />}
        {label}
      </span>
      {!active && <span className="pointer-events-none absolute inset-0 rounded-full border border-line" />}
    </button>
  );
}
