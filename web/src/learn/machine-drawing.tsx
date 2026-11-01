/*
 * The machine (learn-machine-v2-001, amendments-005): ONE chassis — not five
 * cards. A file's whole journey on a single belt: disk → L1 gate (one refused
 * into the tray, one through, lifting the crossbar and lighting the four
 * layers) → chunk/embed → INDEX; queries enter only at the intake port on the
 * right edge, ray into the index, score as bars and leave as an evidence
 * card — or, every other round, nothing (exit 0).
 *
 * amendments-005 (visual rework): regions are seams on one filled chassis
 * (≥35% solid silhouettes, no gutters); one lane, two live movers at most;
 * gold budget = protagonist + active region + QA values + focus; red only
 * where something is refused; type floor 13px; the opened region's honest
 * semantics become readable while isolated.
 */
import { motion } from "framer-motion";
import { pick, type Lang } from "../lib/lang";
import { Part, GOLD, TEAL, SUB, LINE, FAIL, INK2, type PartHandler } from "./station-visuals";
import type { StationId } from "../lib/learn-data";

export const MACHINE_VB = "0 0 1040 470";
/* belt lane geometry (SVG y) and its position as % of machine height for the
   HTML protagonist overlay riding above it */
export const BELT_TOP = 222;
export const BELT_MID = 236;
export const BELT_Y_PCT = (BELT_MID / 470) * 100;

type RegionProps = {
  id: StationId;
  x: number;
  w: number;
  zh: string;
  en: string;
  lang: Lang;
  count: number;
  visited: number;
  active: boolean;
  isolated: StationId | null;
  onOpen: (id: StationId) => void;
  children: React.ReactNode;
};

/* isolate geometry: the chosen region comes forward to center while siblings
   step aside toward their nearest edge (motion, not dim-in-place) */
const ISOLATE: Record<StationId, { dx: number; dy: number }> = {
  walk: { dx: 380, dy: 12 },
  identity: { dx: 164, dy: 12 },
  understand: { dx: -64, dy: 12 },
  search: { dx: -420, dy: 12 },
  rail: { dx: 0, dy: 0 },
};
const SLIDE: Partial<Record<StationId, Partial<Record<StationId, number>>>> = {
  walk: { identity: -70, understand: -90, search: -110 },
  identity: { walk: -60, understand: 80, search: 100 },
  understand: { walk: -70, identity: -60, search: 90 },
  search: { walk: -90, identity: -80, understand: -70 },
};

const SPRING = { type: "spring", stiffness: 210, damping: 28 } as const;

/* visible one-line semantics, shown only while a region is isolated
   (the closed machine keeps them sr-only — V2 zero visible text) */
export const REGION_DESC: Record<Exclude<StationId, "rail">, { zh: string; en: string }> = {
  walk: {
    zh: "文件从磁盘上带：确定性走盘、波次抽取，跳过件不上带；每件落地盖事务章——中断不剩半个文件。",
    en: "Files leave the disk on the belt: deterministic walk, wave extraction; skipped files never board. Each arrival is stamped — an interrupt leaves no half-file.",
  },
  identity: {
    zh: "两道指纹门：一件在第一层被拒（整库拒收 exit 3），一件四层全过——拒收、逐行重嵌、重抽文本、评测分母各不相同。",
    en: "Two fingerprint gates: one refused at layer 1 (whole index refused, exit 3), one passes all four — refusal, row re-embed, re-extract, eval denominator all differ.",
  },
  understand: {
    zh: "文档切块、逐块嵌入向量点阵；空心格=纯词法页不生成向量；音频有转录走文字向量。",
    en: "Documents chunk and embed into the vector grid; hollow cells = lexical-only pages get no vector; audio with a transcript rides text vectors.",
  },
  search: {
    zh: "查询只从这里进：准入、混合融合、本地重排、证据选择；隔轮零命中——exit 0 也是合法答案。",
    en: "Queries only enter here: admission, hybrid fusion, local rerank, evidence selection; every other round is zero-hit — exit 0 is a legal answer.",
  },
};

function Region({ id, x, w, zh, en, lang, count, visited, active, isolated, onOpen, children }: RegionProps) {
  const me = isolated === id;
  const aside = isolated !== null && !me;
  const off = ISOLATE[id];
  const slide = aside ? (SLIDE[isolated as StationId]?.[id] ?? 0) : 0;
  return (
    <motion.g
      data-station={id}
      className={me ? "region-isolated" : aside ? "step-aside" : ""}
      animate={{
        x: me ? off.dx : slide,
        y: me ? off.dy : 0,
        scale: me ? 1.18 : aside ? 0.96 : 1,
        opacity: aside ? 0.42 : 1,
      }}
      transition={SPRING}
      style={{ transformBox: "fill-box", transformOrigin: "center" }}
      onClick={() => onOpen(id)}
    >
      {/* active bracket on the chassis top edge */}
      {active && !me && <line className="region-active" x1={x + 14} y1={65} x2={x + w - 14} y2={65} stroke={GOLD} strokeWidth={2.5} />}
      <g
        className="region-label"
        role="button"
        tabIndex={0}
        aria-expanded={me}
        aria-label={pick(lang, `隔离查看：${zh} · ${count} 个概念`, `isolate: ${en} · ${count} concepts`)}
        onClick={(e) => {
          e.stopPropagation();
          onOpen(id);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            e.stopPropagation();
            onOpen(id);
          }
        }}
      >
        <rect x={x + 10} y={78} width={w - 20} height={34} rx={8} fill="transparent" />
        <text x={x + 16} y={102} className="machine-name">
          <tspan fill={active ? GOLD : "var(--color-ink)"}>{pick(lang, zh, en)}</tspan>
          <tspan dx={10} className="machine-count" fill={SUB}>
            {count} ·{" "}
          </tspan>
          <tspan className="machine-count" fill={visited > 0 ? GOLD : SUB}>
            {visited} →
          </tspan>
        </text>
      </g>
      {children}
    </motion.g>
  );
}

/* the disk, drawn as a filled drum: body, top face, highlight, spindle, spin */
function Disk({ dim }: { dim: boolean }) {
  const stroke = dim ? LINE : INK2;
  return (
    <g>
      <path d="M18 202 v56 a42 13 0 0 0 84 0 v-56" fill="rgba(255,255,255,0.05)" stroke={stroke} strokeWidth="1.6" />
      <ellipse cx="60" cy="202" rx="42" ry="13" fill="rgba(255,255,255,0.07)" stroke={stroke} strokeWidth="1.6" />
      <path d="M28 197 a32 9 0 0 1 52 -4" fill="none" stroke="rgba(255,255,255,0.35)" strokeWidth="1.4" />
      <circle cx="60" cy="202" r="3.5" fill={stroke} />
      <ellipse className="pv-spin" cx="60" cy="202" rx="30" ry="8.5" fill="none" stroke={TEAL} strokeWidth="1.1" strokeDasharray="6 10" opacity="0.85" />
    </g>
  );
}

const fileGlyph = (x: number, y: number, stroke: string, cls?: string) => (
  <g className={cls} transform={`translate(${x} ${y})`}>
    <rect width="22" height="26" rx="3" fill="rgba(255,255,255,0.04)" stroke={stroke} strokeWidth="1.5" />
    <path d="M12 0 h6 l6 6 h-12 z" fill="none" stroke={stroke} strokeWidth="1.2" />
    <line x1="4" y1="13" x2="16" y2="13" stroke={stroke} strokeWidth="1" opacity="0.7" />
    <line x1="4" y1="18" x2="12" y2="18" stroke={stroke} strokeWidth="1" opacity="0.7" />
  </g>
);

export function MachineSVG({
  lang, beat, cycle, counts, visitedCounts, isolated, onOpen, onOpenPart,
}: {
  lang: Lang;
  beat: number;
  cycle: number;
  counts: Record<StationId, number>;
  visitedCounts: Record<StationId, number>;
  isolated: StationId | null;
  onOpen: (id: StationId) => void;
  onOpenPart: PartHandler;
}) {
  const activeRegion: StationId = beat >= 3 ? "search" : beat === 2 ? "understand" : beat === 1 ? "identity" : "walk";
  const zero = cycle % 2 === 1;
  const dim = isolated !== null;
  const regionWalk = (
          <Region id="walk" x={8} w={234} zh="走盘" en="Walk" lang={lang} count={counts.walk} visited={visitedCounts.walk} active={activeRegion === "walk"} isolated={isolated} onOpen={onOpen}>
        <Part id="walk-gates" zh="行走门禁" en="walk gates" lang={lang} onPart={onOpenPart}>
          <Disk dim={dim} />
          <path d="M148 214 l7 6 -7 6 M160 214 l7 6 -7 6" fill="none" stroke={SUB} strokeWidth="1.3" />
          <path d="M188 214 l7 6 -7 6 M200 214 l7 6 -7 6" fill="none" stroke={SUB} strokeWidth="1.3" />
          <g opacity="0.7">
            {[[118, 300], [166, 342]].map(([x, y]) => (
              <g key={x} transform={`translate(${x} ${y})`}>
                <rect width="20" height="24" rx="3" fill="none" stroke={SUB} strokeWidth="1.2" strokeDasharray="4 3" />
                <line x1="-3" y1="27" x2="23" y2="-3" stroke={SUB} strokeWidth="1.2" />
              </g>
            ))}
          </g>
        </Part>
        <Part id="wave-extraction" zh="波次抽取" en="wave extraction" lang={lang} onPart={onOpenPart}>
          {[128, 148, 168].map((y, i) => (
            <path key={y} className="pv-wavearc" style={{ animationDelay: `${i * 0.7}s` }} d={`M112 ${y} q30 -11 60 0 t60 0`} fill="none" stroke={SUB} strokeWidth="1" strokeDasharray="3 6" opacity="0.55" />
          ))}
        </Part>
      </Region>
  );
  const regionIdentity = (
          <Region id="identity" x={242} w={228} zh="身份裁决" en="Identity" lang={lang} count={counts.identity} visited={visitedCounts.identity} active={activeRegion === "identity"} isolated={isolated} onOpen={onOpen}>
        <Part id="fingerprint-gate" zh="指纹门" en="fingerprint gate" lang={lang} onPart={onOpenPart}>
          {[300, 396].map((gx) => (
            <g key={gx}>
              <rect x={gx - 9} y="140" width="18" height="190" rx="4" fill="rgba(255,255,255,0.05)" stroke={INK2} strokeWidth="1.4" />
              {[168, 202, 262, 296].map((y, i) => (
                <rect key={y} className="pv-layer" style={{ animationDelay: `${2.7 + i * 0.35}s` }} x={gx - 6} y={y - 5} width="12" height="10" rx="2" fill="none" stroke={beat === 1 ? "rgba(255,255,255,0.85)" : INK2} strokeWidth="1.4" />
              ))}
            </g>
          ))}
          <rect className="pv-lift" x="318" y="218" width="60" height="7" rx="3" fill="rgba(255,255,255,0.14)" stroke={INK2} strokeWidth="1" />
        </Part>
        <g className="pv-fall">{fileGlyph(262, 209, INK2)}</g>
        <circle className="pv-refuse" cx="300" cy="236" r="10" fill="none" stroke={FAIL} strokeWidth="1.6" />
        <g className="pv-through">{fileGlyph(310, 209, TEAL)}</g>
        <Part id="four-layer-identity" zh="四层身份模型" en="four-layer identity" lang={lang} onPart={onOpenPart}>
          <rect x="254" y="330" width="82" height="88" rx="8" fill="rgba(255,107,107,0.05)" stroke={FAIL} strokeWidth="1.3" strokeDasharray="5 5" opacity="0.9" />
          <path d="M262 348 h34 M262 362 h46 M262 376 h28" stroke={FAIL} strokeWidth="1.1" opacity="0.6" />
        </Part>
      </Region>
  );
  const regionUnderstand = (
          <Region id="understand" x={470} w={228} zh="理解嵌入" en="Understand" lang={lang} count={counts.understand} visited={visitedCounts.understand} active={activeRegion === "understand"} isolated={isolated} onOpen={onOpen}>
        <Part id="chunking" zh="分块" en="chunking" lang={lang} onPart={onOpenPart}>
          <rect x="500" y="118" width="60" height="84" rx="6" fill="rgba(255,255,255,0.05)" stroke={beat === 2 ? GOLD : "rgba(255,255,255,0.55)"} strokeWidth="1.6" />
          <line x1="500" y1="146" x2="560" y2="146" stroke={SUB} strokeWidth="1" strokeDasharray="4 4" />
          <line x1="500" y1="174" x2="560" y2="174" stroke={SUB} strokeWidth="1" strokeDasharray="4 4" />
          {[0, 1, 2].map((i) => (
            <rect key={i} className="pv-drop" style={{ animationDelay: `${i * 0.9}s` }} x={506 + i * 18} y={126} width="26" height="18" rx="3" fill="rgba(255,255,255,0.06)" stroke={INK2} strokeWidth="1.3" />
          ))}
        </Part>
        <Part id="vector-space" zh="向量与坐标" en="vectors and coordinates" lang={lang} onPart={onOpenPart}>
          <rect x="592" y="108" width="106" height="88" rx="8" fill="rgba(255,255,255,0.03)" />
          {Array.from({ length: 12 }, (_, k) => {
            const r = Math.floor(k / 4), c = k % 4;
            const hollow = k % 5 === 4;
            return (
              <rect key={k} className={hollow ? "" : "pv-cell"} style={hollow ? undefined : { animationDelay: `${1.6 + k * 0.28}s` }} x={600 + c * 24} y={120 + r * 28} width="14" height="14" rx="2.5" fill={hollow ? "none" : TEAL} stroke={hollow ? SUB : "none"} strokeWidth="1.4" opacity={hollow ? 0.9 : 0} />
            );
          })}
          <rect x="602" y="198" width="96" height="14" rx="6" fill="rgba(255,255,255,0.05)" stroke={INK2} strokeWidth="1.2" />
          {[610, 655, 684].map((x) => (
            <line key={x} x1={x} y1="196" x2={x} y2="218" stroke={SUB} strokeWidth="1" strokeDasharray="2 5" opacity="0.6" />
          ))}
        </Part>
        <Part id="whisper-asr" zh="whisper 转录" en="whisper transcription" lang={lang} onPart={onOpenPart}>
          <path
            className="pv-flatten"
            d="M484 388 q6 -12 12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0"
            fill="none"
            stroke={TEAL}
            strokeWidth="1.4"
          />
        </Part>
      </Region>
  );
  const regionSearch = (
          <Region id="search" x={884} w={148} zh="检索求证" en="Search" lang={lang} count={counts.search} visited={visitedCounts.search} active={activeRegion === "search"} isolated={isolated} onOpen={onOpen}>
        {zero ? (
          <Part id="zero-hit-honesty" zh="零命中诚实" en="zero-hit honesty" lang={lang} onPart={onOpenPart} className="pv-zero">
            <circle cx="955" cy="356" r="20" fill="none" stroke={SUB} strokeWidth="1.5" strokeDasharray="5 6" />
            <line x1="941" y1="370" x2="969" y2="342" stroke={SUB} strokeWidth="1.5" />
          </Part>
        ) : (
          <g>
            <Part id="admission-gate" zh="准入门槛" en="admission gate" lang={lang} onPart={onOpenPart} className="pv-ride">
              <circle cx="1026" cy="236" r="6" fill={TEAL} />
            </Part>
            <Part id="hybrid-fusion" zh="混合融合" en="hybrid fusion" lang={lang} onPart={onOpenPart}>
              {[250, 272, 294].map((y, i) => (
                <line key={y} className="pv-ray" style={{ animationDelay: `${0.5 + i * 0.25}s` }} x1="900" y1="236" x2="866" y2={y} stroke={i === 0 ? GOLD : SUB} strokeWidth={i === 0 ? 1.6 : 1} opacity="0.75" />
              ))}
            </Part>
            <Part id="local-rerank" zh="本地重排" en="local rerank" lang={lang} onPart={onOpenPart}>
              {[110, 58, 16].map((w, i) => (
                <rect key={i} className="pv-bar" style={{ animationDelay: `${1.3 + i * 0.25}s` }} x="900" y={330 + i * 26} height="7" rx="3" width={w} fill={i === 0 ? GOLD : SUB} opacity={i === 0 ? 0.9 : 0.45} />
              ))}
            </Part>
            {[340, 366, 392].map((y, i) => (
              <rect key={y} x="1016" y={y} width="9" height="7" rx="2" fill={i === 0 ? TEAL : "rgba(255,255,255,0.14)"} opacity="0.8" />
            ))}
            <Part id="evidence-selection" zh="证据选择" en="evidence selection" lang={lang} onPart={onOpenPart}>
              <rect className="pv-evidence" x="900" y="408" width="110" height="26" rx="6" fill="rgba(255,255,255,0.04)" stroke={GOLD} strokeWidth="1.6" />
              <line x1="952" y1="344" x2="952" y2="406" stroke={GOLD} strokeWidth="1.1" strokeDasharray="3 4" opacity="0.65" />
            </Part>
          </g>
        )}
        {/* the intake port: queries enter here, on the chassis edge */}
        <g fill="rgba(255,255,255,0.05)" stroke={INK2} strokeWidth="1.4" opacity="0.95">
          <rect x="1000" y="206" width="26" height="60" rx="9" />
          <line x1="1006" y1="222" x2="1020" y2="222" stroke={SUB} strokeWidth="1.2" />
          <line x1="1006" y1="236" x2="1020" y2="236" stroke={SUB} strokeWidth="1.2" />
          <line x1="1006" y1="250" x2="1020" y2="250" stroke={SUB} strokeWidth="1.2" />
        </g>
      </Region>
  );

  return (
    <svg viewBox={MACHINE_VB} style={{ width: "100%", height: "auto", display: "block", overflow: "visible" }} role="group" aria-label={pick(lang, "整台机器：从磁盘到可搜", "the whole machine: disk to searchable")}>
      <desc>
        {pick(
          lang,
          "一台连续的机器：文件从磁盘上带，过 L1 指纹门（一件被拒弹入托盘 exit 3，一件四层全过），切块嵌入点亮向量点阵，落进唯一索引盖事务章；查询只从右端入口进入，射线打进索引按分亮条，最长一条连到证据卡；隔一轮全空——零命中也是合法答案 exit 0。底部标尺是质检线：质量门、测试、上传字节。",
          "one continuous machine: files leave the disk on a belt, meet the L1 fingerprint gate (one refused into the tray, exit 3; one through, lighting the four layers), get chunked and embedded into a vector grid, land in the one index with transaction stamps; queries enter only at the intake port on the right, ray into the index, score as bars, top one links to an evidence card; every other round is empty — zero-hit is a legal answer, exit 0. The ruler below is the QA line: gates, tests, uploaded bytes.",
        )}
      </desc>

      {/* one chassis, seams instead of gutters */}
      <rect x="8" y="64" width="1024" height="400" rx="20" fill="rgba(255,255,255,0.028)" stroke={LINE} strokeWidth="1" />
      {[242, 470, 698, 884].map((x) => (
        <line key={x} x1={x} y1="84" x2={x} y2="444" stroke="var(--color-line)" strokeWidth="1" opacity="0.55" />
      ))}







      {/* index: filled drawer stack — WAL, rows, stamps inside the block */}
      <motion.g
        className="index-g"
        animate={{ opacity: dim ? 0.3 : 1 }}
        transition={SPRING}
        onClick={(e) => {
          if (dim) e.stopPropagation();
        }}
      >
        <Part id="idempotency" zh="幂等与续跑" en="idempotency and resume" lang={lang} onPart={onOpenPart}>
          <rect x="726" y="150" width="136" height="162" rx="14" fill="rgba(255,255,255,0.055)" stroke={LINE} strokeWidth="1.8" />
        </Part>
        {[180, 205, 230, 255].map((y) => (
          <g key={y}>
            <line x1="762" y1={y} x2="850" y2={y} stroke={SUB} strokeWidth="1" opacity="0.8" />
            <rect x="836" y={y - 4} width="10" height="8" rx="2" fill={TEAL} opacity="0.85" />
          </g>
        ))}
        <Part id="sqlite-wal" zh="SQLite 与 WAL" en="SQLite and WAL" lang={lang} onPart={onOpenPart}>
          <rect className="pv-wal" x="734" y="158" width="16" height="146" rx="5" fill="rgba(111,179,170,0.16)" stroke={TEAL} strokeWidth="1.4" />
        </Part>
        <Part id="one-transaction" zh="一文件一事务" en="one file, one transaction" lang={lang} onPart={onOpenPart}>
          {[770, 794, 818].map((cx, i) => (
            <circle key={cx} className="pv-stamp" style={{ animationDelay: `${1.9 + i * 1.9}s` }} cx={cx} cy="298" r="5" fill="none" stroke={TEAL} strokeWidth="1.6" />
          ))}
        </Part>
        <text x="794" y="348" textAnchor="middle" className="machine-index-caption" fill={SUB}>
          INDEX · 1 × lib.sqlite
        </text>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <rect key={i} x={744 + i * 18} y="366" width="9" height="7" rx="2" fill={i % 3 === 1 ? TEAL : "rgba(255,255,255,0.14)"} opacity="0.8" />
        ))}
      </motion.g>



      {/* regions in pipeline order; the isolated one paints last (SVG paint order = DOM order) */}
      {(() => {
        const list = [regionWalk, regionIdentity, regionUnderstand, regionSearch];
        if (isolated && isolated !== "rail") {
          const i = list.findIndex((el) => el.props.id === isolated);
          if (i >= 0) list.push(list.splice(i, 1)[0]);
        }
        return list;
      })()}

      {/* the belt: one continuous bed, files march disk → index */}
      <g className="belt-lane" aria-hidden="true">
        <rect x="104" y={BELT_TOP} width="622" height="28" fill="rgba(255,255,255,0.03)" />
        <line x1="104" y1={BELT_TOP} x2="726" y2={BELT_TOP} stroke={LINE} strokeWidth="1.4" />
        <line x1="104" y1="250" x2="726" y2="250" stroke={LINE} strokeWidth="1.4" />
        <line x1="104" y1={BELT_MID} x2="726" y2={BELT_MID} stroke={SUB} strokeWidth="0.8" strokeDasharray="2 8" opacity="0.6" />
        {[0, 1].map((i) => (
          <g key={i} className="pv-march" style={{ animationDelay: `${i * 2.6}s` }}>
            {fileGlyph(104, 209, INK2)}
          </g>
        ))}
      </g>

    </svg>
  );
}
