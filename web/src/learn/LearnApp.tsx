import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Nav } from "../components/Nav";
import { Footer } from "../components/Footer";
import { ConceptCard } from "./ConceptCard";
import { LessonDrawer } from "./LessonDrawer";
import { Stage } from "./Stage";
import { useLang, pick } from "../lib/lang";
import {
  STATIONS,
  clusterColor,
  layerBadge,
  loadVisited,
  saveVisited,
  stationMeta,
  stationOf,
  type Concept,
  type LearnData,
  type StationId,
} from "../lib/learn-data";
import { IconSearch, IconClose } from "../components/Icons";

type Ok = Extract<LearnData, { ok: true }>;
type View =
  | { kind: "stage" }
  | { kind: "station"; id: StationId }
  | { kind: "l0" }
  | { kind: "search" };

export function LearnApp({ data }: { data: Ok }) {
  const lang = useLang();
  const zh = lang === "zh";
  const { graph, byId } = data;

  const [query, setQuery] = useState("");
  const [station, setStation] = useState<StationId | null>(null);
  const [l0open, setL0open] = useState(false);
  // sub-cluster filter, only meaningful inside understand / rail panels (B1)
  const [cluster, setCluster] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // which card the drawer expanded from — its slot stays reserved (invisible,
  // exact height) until the drawer closes, so the morph has a stable anchor
  const [openedId, setOpenedId] = useState<string | null>(null);
  const [visited, setVisited] = useState<Set<string>>(() => loadVisited());
  const searchRef = useRef<HTMLInputElement>(null);
  // stage scroll position, saved on entering a panel and restored on exit (B5)
  const stageScrollRef = useRef(0);

  const view: View = query.trim()
    ? { kind: "search" }
    : station
      ? { kind: "station", id: station }
      : l0open
        ? { kind: "l0" }
        : { kind: "stage" };

  /* ---------------- hash routes: #stage= / #l0 / #q= / #c= (B3) ---------------- */

  const applyHash = useCallback(() => {
    const h = decodeURIComponent(location.hash || "");
    let m: RegExpMatchArray | null;
    if ((m = h.match(/^#stage=([a-z]+)/))) {
      const id = m[1] as StationId;
      if (STATIONS.some((s) => s.id === id)) {
        setQuery("");
        setL0open(false);
        setStation(id);
        return;
      }
    }
    if (h.startsWith("#l0")) {
      setQuery("");
      setStation(null);
      setL0open(true);
      return;
    }
    if ((m = h.match(/^#q=([^&]*)/))) {
      setStation(null);
      setL0open(false);
      setQuery(m[1]);
      return;
    }
    setStation(null);
    setL0open(false);
    setQuery("");
  }, []);

  useEffect(() => {
    // cold boot: restore deep-linked state (G9)
    if (location.hash) applyHash();
    const c = location.hash.match(/^#c=([a-z0-9_-]+)/);
    if (c && byId.has(c[1])) setSelectedId(c[1]);
    const onPop = () => applyHash();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pushHash = useCallback((h: string) => {
    if (location.hash !== h) history.pushState(null, "", h || location.pathname);
  }, []);

  const syncHash = useCallback(
    (v: View, cardId: string | null) => {
      if (cardId) pushHash(`#c=${cardId}`);
      else if (v.kind === "station") pushHash(`#stage=${v.id}`);
      else if (v.kind === "l0") pushHash("#l0");
      else if (v.kind === "search") pushHash(`#q=${encodeURIComponent(query.trim())}`);
      else pushHash("");
    },
    [pushHash, query],
  );

  useEffect(() => {
    if (selectedId && byId.has(selectedId)) pushHash(`#c=${selectedId}`);
    else syncHash(view, null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.kind, station, l0open, query, selectedId]);

  /* ---------------- station aggregates for the stage ---------------- */

  const tokenOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of graph.clusters) m.set(c.id, c.token);
    return m;
  }, [graph.clusters]);

  const stationNodes = useMemo(() => {
    const by: Record<StationId, Concept[]> = { walk: [], identity: [], understand: [], search: [], rail: [] };
    for (const n of graph.nodes) by[stationOf(n.cluster)].push(n);
    return by;
  }, [graph.nodes]);

  const counts = useMemo(
    () => Object.fromEntries(STATIONS.map((s) => [s.id, stationNodes[s.id].length])) as Record<StationId, number>,
    [stationNodes],
  );
  const visitedCounts = useMemo(() => {
    const by: Record<StationId, number> = { walk: 0, identity: 0, understand: 0, search: 0, rail: 0 };
    for (const n of graph.nodes) if (visited.has(n.id)) by[stationOf(n.cluster)]++;
    return by;
  }, [graph.nodes, visited]);
  const keystones = useMemo(() => {
    const by = Object.fromEntries(STATIONS.map((s) => [s.id, [] as string[]])) as Record<StationId, string[]>;
    for (const n of graph.nodes)
      if (n.keystone) by[stationOf(n.cluster)].push(pick(lang, n.title_zh, n.title_en));
    return by;
  }, [graph.nodes, lang]);
  const l0Count = useMemo(() => graph.nodes.filter((n) => n.layer === 0).length, [graph.nodes]);
  const total = graph.nodes.length;
  const totalVisited = useMemo(() => graph.nodes.filter((n) => visited.has(n.id)).length, [graph.nodes, visited]);

  /* ---------------- open/close with scroll save/restore (B5) ---------------- */

  const scrollTo = useCallback((top: number) => {
    const instant = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top, behavior: instant ? "instant" : "smooth" });
  }, []);

  const openStation = useCallback(
    (id: StationId | "l0") => {
      stageScrollRef.current = window.scrollY;
      if (id === "l0") setL0open(true);
      else {
        // entering a panel resets the sub-filter unless it belongs to the station
        setCluster((c) => {
          if (c !== "all" && !stationMeta(id).clusters.includes(c)) return "all";
          return c;
        });
        setStation(id);
      }
      requestAnimationFrame(() => {
        const grid = document.getElementById("grid");
        if (grid) scrollTo(grid.getBoundingClientRect().top + window.scrollY - 80);
      });
    },
    [scrollTo],
  );

  const backToStage = useCallback(() => {
    setStation(null);
    setL0open(false);
    setCluster("all");
    requestAnimationFrame(() => {
      scrollTo(stageScrollRef.current);
      // focus returns to the pod we came from (G5)
      const btn = document.querySelector<HTMLElement>(`[data-station="${station ?? ""}"] button`);
      btn?.focus?.();
    });
  }, [scrollTo, station]);

  /* ---------------- card open / visited ---------------- */

  const handleOpen = useCallback((id: string) => {
    setOpenedId(id);
    setSelectedId(id);
  }, []);
  const handleClose = useCallback(() => {
    setSelectedId(null);
    setOpenedId(null);
  }, []);
  const handleNavigate = useCallback((id: string) => setSelectedId(id), []);
  const toggleVisited = useCallback(
    (id: string) => {
      setVisited((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        saveVisited(next);
        return next;
      });
    },
    [],
  );

  /* ---------------- the one filtered list, per view (B4: keyed remount) ---------------- */

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list: Concept[];
    if (view.kind === "search") {
      list = graph.nodes.filter((n) => {
        const cl = graph.clusters.find((c) => c.id === n.cluster);
        const hay = `${n.title_zh} ${n.title_en} ${n.one_zh} ${n.one_en} ${cl?.label_zh ?? ""} ${cl?.label_en ?? ""}`.toLowerCase();
        return hay.includes(q);
      });
      list.sort((a, b) =>
        (tokenOf.get(a.cluster) ?? "Z").localeCompare(tokenOf.get(b.cluster) ?? "Z") || a.layer - b.layer || a.id.localeCompare(b.id),
      );
    } else if (view.kind === "station") {
      list = graph.nodes.filter(
        (n) => stationOf(n.cluster) === view.id && (cluster === "all" || n.cluster === cluster),
      );
      // keystones lead the panel, then deepest mechanisms first (design §工位面板)
      list.sort((a, b) => Number(b.keystone) - Number(a.keystone) || b.layer - a.layer || a.id.localeCompare(b.id));
    } else if (view.kind === "l0") {
      list = graph.nodes.filter((n) => n.layer === 0);
      const order = (c: string) => STATIONS.findIndex((s) => s.id === stationOf(c));
      list.sort((a, b) => order(a.cluster) - order(b.cluster) || a.id.localeCompare(b.id));
    } else {
      list = [];
    }
    return list;
  }, [view, query, cluster, graph, tokenOf]);

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

  const gridModeKey = view.kind === "station" ? `station-${view.id}-${cluster}` : view.kind === "l0" ? "l0" : view.kind === "search" ? "search" : "none";
  const showBadge = view.kind === "search" || view.kind === "l0";
  const subClusters =
    view.kind === "station" && (view.id === "understand" || view.id === "rail")
      ? graph.clusters.filter((c) => stationMeta(view.id).clusters.includes(c.id))
      : [];

  return (
    <>
      <a
        href="#grid"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:bg-bg3 focus:px-4 focus:py-2 focus:text-ink"
      >
        {zh ? "跳到概念网格" : "Skip to the grid"}
      </a>
      <Nav page="learn" />

      {view.kind === "stage" ? (
        <Stage
          lang={lang}
          counts={counts}
          visitedCounts={visitedCounts}
          keystones={keystones}
          l0Count={l0Count}
          total={total}
          totalVisited={totalVisited}
          onOpen={openStation}
        />
      ) : (
        <header className="mx-auto max-w-6xl px-5 pt-24 pb-4">
          <button
            type="button"
            onClick={backToStage}
            className="rounded-full border border-line px-4 py-1.5 text-[13px] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
          >
            ← {zh ? "返回舞台" : "back to the machine"}
          </button>
          {view.kind === "station" && (
            <div className="mt-4 flex flex-wrap items-baseline gap-x-4 gap-y-2">
              <h1 className="font-display text-display leading-[1.1] text-ink">
                {pick(lang, stationMeta(view.id).label_zh, stationMeta(view.id).label_en)}
                <span className="ml-3 align-middle text-[13px] text-sub">
                  {view.id} × {counts[view.id]}
                </span>
              </h1>
              <p className="w-full text-[14px] text-ink2">{pick(lang, stationMeta(view.id).role_zh, stationMeta(view.id).role_en)}</p>
            </div>
          )}
          {view.kind === "l0" && (
            <div className="mt-4">
              <h1 className="font-display text-display leading-[1.1] text-ink">
                {zh ? "基础层 L0" : "Layer L0"}
                <span className="ml-3 align-middle text-[13px] text-sub">{l0Count} · {zh ? "跨工位" : "across stations"}</span>
              </h1>
              <p className="mt-1 max-w-[34em] text-[14px] text-ink2">
                {zh ? "机器底下的地基：每张卡标注它属于哪个工位。" : "The ground under the machine: each card carries its home station."}
              </p>
            </div>
          )}
          {view.kind === "search" && (
            <div className="mt-4">
              <h1 className="font-display text-display leading-[1.1] text-ink">
                {zh ? "搜索结果" : "Search"}
                <span className="ml-3 align-middle text-[13px] text-sub">
                  {filtered.length} / {total}
                </span>
              </h1>
            </div>
          )}
        </header>
      )}

      {/* sticky toolbar — search always; sub-cluster pills only where a panel
          actually merges clusters (understand / rail). The global A–G and
          L0–L3 pill rows are retired (amendments B1). */}
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
                className="w-full rounded-full border border-line bg-bg1/80 py-2 pr-9 pl-9 font-mono text-[13px] text-ink placeholder:text-sub focus:border-gold/60"
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
            {view.kind !== "search" && (
              <p className="text-[13px] text-sub" aria-live="polite">
                <span className="sr-only">{zh ? "当前在列：" : "concepts shown: "}</span>
                {view.kind === "stage" ? total : `${filtered.length} / ${total}`}
              </p>
            )}
          </div>

          {subClusters.length > 0 && (
            <div className="no-scrollbar hscroll mt-2.5 flex items-center gap-2 overflow-x-auto pb-1" role="group" aria-label={zh ? "按子簇筛选" : "filter by sub-cluster"}>
              <Pill active={cluster === "all"} onClick={() => setCluster("all")} layoutId="cluster-pill" label={zh ? "全部" : "All"} />
              {subClusters.map((c) => (
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
          )}
        </div>
      </div>

      <main id="grid" className="mx-auto max-w-6xl px-5 py-10">
        {view.kind === "stage" ? (
          <p className="text-[13px] text-sub">
            {zh ? "点一个工位，拆开看它的机制与概念。" : "Open a station to see its mechanisms and concepts."}
          </p>
        ) : filtered.length === 0 ? (
          <div className="rounded-2xl border border-line bg-bg1/60 p-12 text-center">
            <p className="font-display text-2xl text-ink">{zh ? "这个组合没有概念" : "No concepts match this combination"}</p>
            <p className="mt-2 text-[13px] text-sub">
              {zh ? "换一个词，或者" : "Try another term, or "}
              <button
                type="button"
                className="text-gold underline underline-offset-4"
                onClick={() => {
                  setQuery("");
                  setCluster("all");
                }}
              >
                {zh ? "清空筛选" : "clear filters"}
              </button>
            </p>
          </div>
        ) : (
          /* keyed by mode: station↔panel remounts the list instead of
             projecting 34 cards through a layout change (B4) */
          <div key={gridModeKey} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {filtered.map((c, i) => {
              const card = (
                <ConceptCard
                  c={c}
                  cluster={graph.clusters.find((cl) => cl.id === c.cluster)}
                  lang={lang}
                  visited={visited.has(c.id)}
                  dimmed={openedId === c.id}
                  onOpen={handleOpen}
                />
              );
              if (!showBadge) {
                return (
                  <div key={c.id} className={i === 0 && filtered[0].keystone ? "lg:col-span-2" : undefined}>
                    {card}
                  </div>
                );
              }
              const st = stationMeta(stationOf(c.cluster));
              return (
                <div key={c.id}>
                  <p className="mb-1 text-[13px] text-sub">
                    <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle" style={{ background: clusterColor(tokenOf.get(c.cluster) ?? "A") }} />
                    {pick(lang, st.label_zh, st.label_en)} · {layerBadge(c.layer, lang)}
                  </p>
                  {card}
                </div>
              );
            })}
          </div>
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
        small ? "px-2.5 py-0.5 text-[13px]" : "px-3.5 py-1 text-[13px]"
      } text-[13px] ${active ? "text-bg0" : "border-line text-ink2 hover:border-gold/40 hover:text-ink"}`}
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
