import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Nav } from "../components/Nav";
import { Footer } from "../components/Footer";
import { ConceptCard } from "./ConceptCard";
import { LessonDrawer } from "./LessonDrawer";
import { MechanismLab } from "./MechanismLab";
import { Stage, StationTrayHeader } from "./Stage";
import { useLang, pick } from "../lib/lang";
import {
  STATIONS,
  clusterColor,
  loadVisited,
  parseLearnHash,
  saveVisited,
  stationMeta,
  stationOf,
  type Concept,
  type LearnData,
  type StationId,
} from "../lib/learn-data";
import { IconSearch, IconClose } from "../components/Icons";

type Ok = Extract<LearnData, { ok: true }>;

/* bottom-up path (pedagogy review): four natural groups, reading order,
   and what each group holds up above — rendered as the L0 entry */
const L0_GROUPS: Array<{ id: string; zh: string; en: string; ids: string[] }> = [
  { id: "repr", zh: "表示与比较", en: "Representation & comparison", ids: ["vector-space", "distance-vs-angle", "inverted-index"] },
  { id: "stats", zh: "读数与推断", en: "Readings & inference", ids: ["mean-std", "percentile", "significance-basics"] },
  { id: "integrity", zh: "一致与完整", en: "Consistency & integrity", ids: ["hash-function", "unicode", "fixed-point", "transaction"] },
  { id: "system", zh: "进程与磁盘", en: "Processes & disk", ids: ["process-pipe", "file-descriptor", "sigpipe-signals", "file-metadata", "race-condition"] },
];
const L0_ORDER: string[] = L0_GROUPS.flatMap((g) => g.ids);
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
  // isolate state — rendered by Stage as the machine tray (machine-v2)
  const [station, setStation] = useState<StationId | null>(null);
  const [l0open, setL0open] = useState(false);
  // sub-cluster filter, only meaningful inside understand / rail trays (B1)
  const [cluster, setCluster] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // which card the drawer expanded from — its slot stays reserved (invisible,
  // exact height) until the drawer closes, so the morph has a stable anchor
  const [openedId, setOpenedId] = useState<string | null>(null);
  const [visited, setVisited] = useState<Set<string>>(() => loadVisited());
  const searchRef = useRef<HTMLInputElement>(null);
  // scroll position across the l0 page swap (the machine never scrolls away)
  const stageScrollRef = useRef(0);
  // cold-boot guard: syncHash must not clobber a deep-linked hash with the
  // pre-boot (empty) state under StrictMode's double mount
  const bootedRef = useRef(false);
  const routeApplying = useRef(false);
  const [routeRevision, setRouteRevision] = useState(0);

  const view: View = query.trim()
    ? { kind: "search" }
    : station
      ? { kind: "station", id: station }
      : l0open
        ? { kind: "l0" }
        : { kind: "stage" };

  /* ---------------- hash routes: #stage= / #l0 / #q= / #c= (B3) ---------------- */

  const applyHash = useCallback(() => {
    const route = parseLearnHash(location.hash, (id) => byId.has(id));
    routeApplying.current = true;
    // Force a route-sync pass even when the requested concept is already open.
    setRouteRevision((revision) => revision + 1);
    setOpenedId(null);
    if (route.kind === "concept") {
      setSelectedId(route.id);
      return; // A valid same-document lesson link preserves its station/background.
    }
    setSelectedId(null);
    setStation(route.kind === "station" ? route.id : null);
    setL0open(route.kind === "l0");
    setQuery(route.kind === "search" ? route.query : "");
  }, [byId]);

  useEffect(() => {
    // cold boot: restore deep-linked state (G9)
    applyHash();
    bootedRef.current = true;
    const onPop = () => applyHash();
    window.addEventListener("popstate", onPop);
    window.addEventListener("hashchange", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("hashchange", onPop);
    };
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
    if (!bootedRef.current) return;
    // URL-driven navigation has already selected its history entry. Do not
    // push another entry while applying Back, hashchange or an invalid hash.
    if (routeApplying.current) { routeApplying.current = false; return; }
    if (selectedId && byId.has(selectedId)) pushHash(`#c=${selectedId}`);
    else syncHash(view, null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.kind, station, l0open, query, selectedId, routeRevision]);

  /* ---------------- station aggregates for the machine ---------------- */

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
  const l0Count = useMemo(() => graph.nodes.filter((n) => n.layer === 0).length, [graph.nodes]);
  const total = graph.nodes.length;

  /* ---------------- open / close: isolate + tray (machine-v2) ---------------- */

  const scrollTo = useCallback((top: number) => {
    const instant = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top, behavior: instant ? "instant" : "smooth" });
  }, []);

  const openStation = useCallback(
    (id: StationId | "l0") => {
      if (id === "l0") {
        stageScrollRef.current = window.scrollY;
        // view precedence puts station above l0open — an isolate must step down
        setStation(null);
        setL0open(true);
        return;
      }
      // entering a tray resets the sub-filter unless it belongs to the station
      setCluster((c) => {
        if (c !== "all" && !stationMeta(id).clusters.includes(c)) return "all";
        return c;
      });
      // the machine stays put; the tray appears just below it (teardown: the
      // body never leaves the view while a part opens)
      setStation(id);
    },
    [],
  );

  const backToStage = useCallback(() => {
    const fromL0 = l0open;
    setStation(null);
    setL0open(false);
    setCluster("all");
    requestAnimationFrame(() => {
      if (fromL0) {
        scrollTo(stageScrollRef.current);
        return;
      }
      // focus returns to the region we came from (G5)
      const label = document.querySelector<SVGGElement>(`[data-station="${station ?? ""}"] .region-label`);
      label?.focus?.();
    });
  }, [scrollTo, l0open, station]);

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
      // keystones lead the tray, then deepest mechanisms first (design §工位面板)
      list.sort((a, b) => Number(b.keystone) - Number(a.keystone) || b.layer - a.layer || a.id.localeCompare(b.id));
    } else if (view.kind === "l0") {
      list = graph.nodes.filter((n) => n.layer === 0);
      const pos = (id: string) => { const i = L0_ORDER.indexOf(id); return i === -1 ? 999 : i; };
      list.sort((a, b) => pos(a.id) - pos(b.id) || a.id.localeCompare(b.id));
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
  const subClusters =
    view.kind === "station" && (view.id === "understand" || view.id === "rail")
      ? graph.clusters.filter((c) => stationMeta(view.id).clusters.includes(c.id))
      : [];

  /* ---------------- the station tray (under the machine, never a page swap) ---------------- */

  const tray =
    view.kind === "station" ? (
      <div id="station-tray" className="mt-4 rounded-2xl bg-bg1/40 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <button
            type="button"
            onClick={backToStage}
            className="rounded-full border border-line px-4 py-1.5 text-[13px] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
          >
            ← {zh ? "返回整台机器" : "back to the machine"}
          </button>
        </div>
        <div className="mt-4">
          <StationTrayHeader lang={lang} id={view.id} onOpen={openStation} />
        </div>
        {subClusters.length > 0 && (
          <div className="no-scrollbar hscroll mt-4 flex items-center gap-2 overflow-x-auto pb-1" role="group" aria-label={zh ? "按子簇筛选" : "filter by sub-cluster"}>
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
        {filtered.length > 0 && (
          <div key={gridModeKey} className="mt-5 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {filtered.map((c, i) => (
              <div key={c.id} className={i === 0 && filtered[0].keystone ? "xl:col-span-2" : undefined}>
                <ConceptCard
                  c={c}
                  cluster={graph.clusters.find((cl) => cl.id === c.cluster)}
                  lang={lang}
                  visited={visited.has(c.id)}
                  dimmed={openedId === c.id}
                  onOpen={handleOpen}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    ) : null;

  return (
    <>
      <a
        href="#grid"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:bg-bg3 focus:px-4 focus:py-2 focus:text-ink"
      >
        {zh ? "跳到概念网格" : "Skip to the grid"}
      </a>
      <Nav page="learn" />

      {(view.kind === "stage" || view.kind === "station") && (
        <Stage
          lang={lang}
          counts={counts}
          visitedCounts={visitedCounts}
          l0Count={l0Count}
          isolated={view.kind === "station" ? view.id : null}
          onOpen={openStation}
          onCloseIsolate={backToStage}
          onOpenPart={handleOpen}
          tray={tray}
        />
      )}

      {view.kind === "stage" && (
        <div className="mx-auto max-w-6xl px-5 pt-8 pb-12">
          <MechanismLab lang={lang} suspended={!!selected} onOpenLesson={handleOpen} />
        </div>
      )}

      {(view.kind === "l0" || view.kind === "search") && (
        <header className="mx-auto max-w-6xl px-5 pt-24 pb-4">
          <button
            type="button"
            onClick={backToStage}
            className="rounded-full border border-line px-4 py-1.5 text-[13px] text-ink2 transition-colors hover:border-gold/60 hover:text-gold"
          >
            ← {zh ? "返回舞台" : "back to the machine"}
          </button>
          {view.kind === "l0" && (
            <div className="mt-4">
              <h1 className="font-display text-display leading-[1.1] text-ink">
                {zh ? "基础层 L0" : "Layer L0"}
              </h1>
            </div>
          )}
          {view.kind === "search" && (
            <div className="mt-4">
              <h1 className="font-display text-display leading-[1.1] text-ink">
                {zh ? "搜索结果" : "Search"}
              </h1>
            </div>
          )}
        </header>
      )}

      {/* sticky toolbar — search always; sub-cluster pills live in the tray now */}
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
                placeholder={zh ? "搜概念、别名、簇……" : "Search concepts, aliases, clusters…"}
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
              <p className="sr-only" aria-live="polite">
                {zh ? "当前在列：" : "concepts shown: "}
                {view.kind === "stage" ? total : `${filtered.length} / ${total}`}
              </p>
            )}
          </div>
        </div>
      </div>

      <main id="grid" className="mx-auto max-w-6xl px-5 py-10">
        {view.kind === "stage" || view.kind === "station" ? null : filtered.length === 0 ? (
          <div className="rounded-2xl border border-line bg-bg1/60 p-12 text-center">
            <p className="font-display text-2xl text-ink">{zh ? "这个组合没有概念" : "No concepts match this combination"}</p>
            <button
              type="button"
              className="mt-4 rounded-full border border-line px-4 py-1.5 text-[14px] text-gold transition-colors hover:border-gold/60"
              onClick={() => {
                setQuery("");
                setCluster("all");
              }}
            >
              {zh ? "清空筛选" : "clear filters"}
            </button>
          </div>
        ) : (
          /* keyed by mode: l0 ↔ search remounts the list instead of projecting
             cards through a layout change (B4) */
          view.kind === "l0" ? (
            /* bottom-up entry: four groups in path order (learn-content-001);
               learn-copy-cut-001 strips the rationale prose — the cards carry it */
            <div className="space-y-10">
              {L0_GROUPS.map((g) => {
                const cards = g.ids
                  .map((id) => byId.get(id))
                  .filter((c): c is Concept => !!c);
                return (
                  <section key={g.id}>
                    <h2 className="font-display text-2xl text-ink">{pick(lang, g.zh, g.en)}</h2>
                    <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                      {cards.map((c) => (
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
                    </div>
                  </section>
                );
              })}
            </div>
          ) : (
          <div key={gridModeKey} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
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
          </div>
          )
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
        small ? "px-2.5 py-0.5 text-[14px]" : "px-3.5 py-1 text-[14px]"
      } text-[14px] ${active ? "text-bg0" : "border-line text-ink2 hover:border-gold/40 hover:text-ink"}`}
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
