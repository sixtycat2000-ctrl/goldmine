import { motion } from "framer-motion";
import { memo } from "react";
import { clusterColor, layerBadge, layerDepth, readingMinutes, type Concept, type ClusterMeta } from "../lib/learn-data";
import { pick, type Lang } from "../lib/lang";
import { IconYes } from "../components/Icons";

type Props = {
  c: Concept;
  cluster: ClusterMeta | undefined;
  lang: Lang;
  visited: boolean;
  /** this card's drawer is open — render an invisible same-height placeholder */
  dimmed: boolean;
  onOpen: (id: string) => void;
};

function CardImpl({ c, cluster, lang, visited, dimmed, onOpen }: Props) {
  const title = pick(lang, c.title_zh, c.title_en);
  const otherTitle = pick(lang, c.title_en, c.title_zh);
  const one = pick(lang, c.one_zh, c.one_en);
  const color = cluster ? clusterColor(cluster.token) : "var(--color-gold)";

  const inner = (interactive: boolean) => (
    <div
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? () => onOpen(c.id) : undefined}
      onKeyDown={
        interactive
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen(c.id);
              }
            }
          : undefined
      }
      className={`group relative flex h-full cursor-pointer flex-col overflow-hidden rounded-2xl bg-bg1 p-5 transition-colors duration-300 hover:bg-bg2 ${
        interactive ? "focus-visible:outline-2 focus-visible:outline-gold" : ""
      }`}
    >
      {/* layer depth: honest progress = how deep into the stack this concept sits */}
      <span
        aria-hidden="true"
        className="absolute inset-x-0 bottom-0 h-[2px] bg-gold/70"
        style={{
          width: `${layerDepth(c.layer) * 100}%`,
          transform: visited ? undefined : "scaleX(0.35)",
          transformOrigin: "left",
          opacity: visited ? 1 : 0.4,
        }}
      />
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-[14px]" style={{ color }}>
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
          {cluster ? `${cluster.token} · ${pick(lang, cluster.label_zh, cluster.label_en)}` : c.cluster}
        </span>
        <span className="text-[14px] text-sub">
          {visited ? (
            <>
              <IconYes size={12} className="text-gold" label={lang === "zh" ? "已读" : "read"} />
            </>
          ) : (
            `≈${readingMinutes(c, lang)} min`
          )}
        </span>
      </div>
      <h2 className="mt-3 font-display text-[1.35rem] leading-snug text-ink">
        {title}
        <span className="mt-0.5 block text-[13px] text-sub">{otherTitle}</span>
      </h2>
      <p className="mt-2 line-clamp-3 text-[14px] leading-relaxed text-ink2">{one}</p>
      <div className="mt-5 flex items-center justify-between">
        <span className="text-[14px] text-sub">{layerBadge(c.layer, lang)}</span>
        <span
          className="text-[14px] text-sub opacity-0 transition-opacity duration-300 group-hover:opacity-100 group-focus-visible:opacity-100"
          aria-hidden="true"
        >
          {lang === "zh" ? "打开 →" : "open →"}
        </span>
      </div>
    </div>
  );

  // Drawer open for this card: keep the exact same content but invisible, so
  // the grid row never reflows. The drawer holds the layoutId and morphs.
  if (dimmed) {
    return (
      <div aria-hidden="true" className="pointer-events-none [&>*]:invisible">
        {inner(false)}
      </div>
    );
  }

  return (
    <motion.div layoutId={`c-${c.id}`} initial={false} exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.18 } }} data-card={c.id}>
      {inner(true)}
    </motion.div>
  );
}

export const ConceptCard = memo(CardImpl);
