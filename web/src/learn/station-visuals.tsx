/*
 * Station visuals: shared palette + the Part button (amendments-004 teardown
 * borrow) + the mobile SearchDemo. The per-station schematics that used to
 * live here (Walk/Identity/Understand demos, demo tags/footnotes) were
 * superseded by the one-chassis machine drawing and are gone since
 * learn-copy-cut-001 (owner rule: cut dead helper chrome). Part ids are real
 * graph nodes, checked by gate G11; labels are sr-only aria-labels, so the
 * visible face stays text-free (V2).
 */
import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { pick, type Lang } from "../lib/lang";

export const GOLD = "var(--color-gold)";
export const TEAL = "var(--color-cl-b)";
export const SUB = "var(--color-sub)";
export const LINE = "var(--color-line-strong)";
export const FAIL = "var(--color-fail)";
export const INK2 = "var(--color-ink2)";

const FRAME = { width: "100%", height: "auto", display: "block" } as const;

/* ---------- parts are buttons (amendments-004 T1) ---------- */

export type PartHandler = (conceptId: string) => void;

export function Part({
  id, zh, en, lang, onPart, className, children,
}: {
  id: string;
  zh: string;
  en: string;
  lang: Lang;
  onPart?: PartHandler;
  className?: string;
  children: ReactNode;
}) {
  if (!onPart) return <>{children}</>;
  const open = (e: MouseEvent<SVGGElement>) => {
    e.stopPropagation();
    onPart(id);
  };
  const key = (e: KeyboardEvent<SVGGElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onPart(id);
    }
  };
  return (
    <g
      className={`pv-part${className ? ` ${className}` : ""}`}
      data-part={id}
      role="button"
      tabIndex={0}
      aria-label={pick(lang, `打开概念：${zh}`, `open concept: ${en}`)}
      onClick={open}
      onKeyDown={key}
    >
      {children}
    </g>
  );
}

/* ---------- search: query pulse → rays → score bars → evidence | zero ---------- */

export function SearchDemo({ beat, cycle, lang, onPart }: { beat: number; lang: Lang; cycle: number; onPart?: PartHandler }) {
  const active = beat >= 3;
  const zero = cycle % 2 === 1;
  return (
    <svg viewBox="0 0 220 150" style={FRAME} aria-hidden={onPart ? undefined : "true"} className="podviz">
      {zero ? (
        <Part id="zero-hit-honesty" zh="零命中诚实" en="zero-hit honesty" lang={lang} onPart={onPart} className="pv-zero">
          <circle cx="110" cy="72" r="20" fill="none" stroke={SUB} strokeWidth="1.5" strokeDasharray="5 6" />
          <line x1="96" y1="86" x2="124" y2="58" stroke={SUB} strokeWidth="1.5" />
        </Part>
      ) : (
        <g>
          <Part id="admission-gate" zh="准入门槛" en="admission gate" lang={lang} onPart={onPart} className="pv-query">
            <circle cx="206" cy="72" r="6" fill={TEAL} />
          </Part>
          <Part id="hybrid-fusion" zh="混合融合" en="hybrid fusion" lang={lang} onPart={onPart}>
            {[44, 72, 100].map((y, i) => (
              <line
                key={y}
                className="pv-ray"
                style={{ animationDelay: `${0.5 + i * 0.25}s` }}
                x1="200"
                y1="72"
                x2={52 + i * 6}
                y2={y}
                stroke={i === 0 ? GOLD : SUB}
                strokeWidth={i === 0 ? 1.6 : 1}
                opacity="0.7"
              />
            ))}
          </Part>
          <Part id="local-rerank" zh="本地重排" en="local rerank" lang={lang} onPart={onPart}>
            {[110, 58, 16].map((w, i) => (
              <rect
                key={i}
                className="pv-bar"
                style={{ animationDelay: `${1.3 + i * 0.25}s` }}
                x="44"
                y={38 + i * 28}
                height="7"
                rx="3"
                width={w}
                fill={i === 0 ? GOLD : SUB}
                opacity={i === 0 ? 0.9 : 0.45}
              />
            ))}
          </Part>
          <Part id="evidence-selection" zh="证据选择" en="evidence selection" lang={lang} onPart={onPart}>
            <rect className="pv-evidence" x="44" y="116" width="110" height="24" rx="5" fill="none" stroke={active ? GOLD : SUB} strokeWidth="1.6" />
            <line x1="99" y1="45" x2="99" y2="114" stroke={GOLD} strokeWidth="1.2" strokeDasharray="3 4" opacity="0.7" />
          </Part>
        </g>
      )}
      <desc>{pick(lang, "一个查询脉冲从右缘进入（查询只从检索进），射线打到三个候选按分亮条，最长一条连到证据卡；隔一轮全空——零命中也是合法答案（exit 0）", "a query pulse enters from the right (queries only enter at search), rays hit three candidates scored as bars, the top links to an evidence card; every other round is empty — zero-hit is a legal answer (exit 0)")}</desc>
    </svg>
  );
}
