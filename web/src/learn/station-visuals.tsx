/*
 * Station visuals (learn-stage-plan-001, amendments-003): every pod is a
 * hand-drawn SVG schematic — no visible text inside the machine. The honest
 * semantics (four-layer consequences, zero-hit legality, lexical-only pages)
 * live in sr-only strings (<desc> + Stage footer tags); all loops are CSS
 * (pv-*) paused by .stage-paused and frozen under reduced motion; `beat`
 * only lights the active pod.
 */
import { pick, type Lang } from "../lib/lang";

const GOLD = "var(--color-gold)";
const TEAL = "var(--color-cl-b)";
const SUB = "var(--color-sub)";
const LINE = "var(--color-line-strong)";
const FAIL = "var(--color-fail)";
const INK2 = "var(--color-ink2)";

export function DemoTag({ lang }: { lang: Lang }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] text-sub">
      {pick(lang, "示意：演示数值经过简化", "demo, values stylized")}
    </span>
  );
}

const DEMO_FOOTNOTE = { zh: "固定示例数据，非实时运行", en: "fixed sample data, not a live run" };

export function DemoFootnote({ lang }: { lang: Lang }) {
  return <p className="mt-1.5 font-mono text-[13px] text-sub">{pick(lang, DEMO_FOOTNOTE.zh, DEMO_FOOTNOTE.en)}</p>;
}

const FRAME = { width: "100%", height: "auto", display: "block" } as const;

/* ---------- walk: disk → three wave lanes → transaction stamps ---------- */

export function WalkDemo({ beat, lang }: { beat: number; lang: Lang }) {
  const active = beat === 0;
  const file = (y: number, cls: string, d: number) => (
    <g className={`pv-file ${cls}`} style={{ animationDelay: `${d}s` }} transform={`translate(36 ${y})`}>
      <rect width="22" height="26" rx="3" fill="none" stroke={INK2} strokeWidth="1.5" />
      <path d="M12 0 h6 l6 6 h-12 z" fill="none" stroke={INK2} strokeWidth="1.2" />
      <line x1="4" y1="13" x2="16" y2="13" stroke={SUB} strokeWidth="1" />
      <line x1="4" y1="18" x2="12" y2="18" stroke={SUB} strokeWidth="1" />
    </g>
  );
  return (
    <svg viewBox="0 0 220 150" style={FRAME} aria-hidden="true" className="podviz">
      <rect x="4" y="28" width="28" height="94" rx="5" fill="none" stroke={active ? GOLD : LINE} strokeWidth="1.6" />
      {[42, 60, 78, 96].map((y) => (
        <line key={y} x1="10" y1={y} x2="26" y2={y} stroke={SUB} strokeWidth="1.2" />
      ))}
      {[52, 75, 98].map((y) => (
        <path key={y} d={`M36 ${y} H148`} stroke={SUB} strokeWidth="1" strokeDasharray="3 5" fill="none" opacity="0.6" />
      ))}
      <rect x="150" y="46" width="62" height="58" rx="9" fill="none" stroke={active ? GOLD : LINE} strokeWidth="1.8" />
      {[62, 75, 88].map((y, i) => (
        <circle key={y} className="pv-stamp" style={{ animationDelay: `${1.9 + i * 1.9}s` }} cx={i === 1 ? 181 : 168 + i * 12} cy={y} r="5" fill="none" stroke={TEAL} strokeWidth="1.6" />
      ))}
      {file(40, "pv-f1", 0)}
      {file(63, "pv-f2", 1.9)}
      {file(86, "pv-f3", 3.8)}
      <desc>{pick(lang, "磁盘上三个文件沿三条波次道进入一个库；每件落地盖事务章——中断不剩半个文件", "three files travel wave lanes from the disk into one index; each arrival is stamped — an interrupt leaves no half-file")}</desc>
    </svg>
  );
}

/* ---------- identity: two files, four gates; one refused at L1, one passes ---------- */

export function IdentityDemo({ beat, lang }: { beat: number; lang: Lang }) {
  const active = beat === 1;
  const gate = (x: number) => (
    <g>
      <line x1={x} y1="24" x2={x} y2="126" stroke={INK2} strokeWidth="1.6" />
      {[42, 62, 82, 102].map((y) => (
        <rect key={y} x={x - 4} y={y - 4} width="8" height="8" rx="1.5" fill="none" stroke={INK2} strokeWidth="1.3" />
      ))}
    </g>
  );
  return (
    <svg viewBox="14 16 200 118" style={FRAME} aria-hidden="true" className="podviz">
      <line x1="20" y1="51" x2="192" y2="51" stroke={LINE} strokeWidth="1" strokeDasharray="2 5" />
      <line x1="20" y1="99" x2="192" y2="99" stroke={LINE} strokeWidth="1" strokeDasharray="2 5" />
      {gate(70)}
      {gate(104)}
      {gate(138)}
      <g className="pv-bounce">
        <rect x="26" y="38" width="22" height="26" rx="3" fill="none" stroke={FAIL} strokeWidth="1.6" />
      </g>
      <circle className="pv-refuse" cx="70" cy="51" r="10" fill="none" stroke={FAIL} strokeWidth="1.6" />
      <g className="pv-pass">
        <rect x="26" y="86" width="22" height="26" rx="3" fill="none" stroke={TEAL} strokeWidth="1.6" />
      </g>
      {[42, 62, 82, 102].map((y, i) => (
        <circle key={y} className="pv-layer" style={{ animationDelay: `${2.7 + i * 0.35}s` }} cx="198" cy={y} r="5.5" fill="none" stroke={active ? GOLD : INK2} strokeWidth="1.8" />
      ))}
      <desc>{pick(lang, "两个文件过四层身份门：一个在第一层被拒（整库拒收 exit 3），一个四层全过、逐层点亮——拒收、逐行重嵌、重抽文本、评测分母各不相同", "two files meet four identity gates: one refused at layer 1 (whole index refused, exit 3), one passes all four — refusal, row re-embed, re-extract and eval denominator all differ")}</desc>
    </svg>
  );
}

/* ---------- understand: doc → three chunks → vector grid; waveform → text ---------- */

export function UnderstandDemo({ beat, lang }: { beat: number; lang: Lang }) {
  const active = beat === 2;
  const cells = [];
  for (let r = 0; r < 3; r++)
    for (let c = 0; c < 4; c++) {
      const hollow = (r * 4 + c) % 5 === 4;
      cells.push(
        <rect
          key={`${r}${c}`}
          className={hollow ? "" : "pv-cell"}
          style={hollow ? undefined : { animationDelay: `${1.6 + (r * 4 + c) * 0.28}s` }}
          x={132 + c * 22}
          y={34 + r * 26}
          width="14"
          height="14"
          rx="2.5"
          fill={hollow ? "none" : TEAL}
          stroke={hollow ? SUB : "none"}
          strokeWidth="1.5"
          opacity={hollow ? 0.9 : 0}
        />,
      );
    }
  return (
    <svg viewBox="0 0 220 150" style={FRAME} aria-hidden="true" className="podviz">
      <rect x="8" y="26" width="34" height="88" rx="4" fill="none" stroke={active ? GOLD : LINE} strokeWidth="1.6" />
      <line x1="8" y1="55" x2="42" y2="55" stroke={SUB} strokeWidth="1" strokeDasharray="4 4" />
      <line x1="8" y1="84" x2="42" y2="84" stroke={SUB} strokeWidth="1" strokeDasharray="4 4" />
      {[0, 1, 2].map((i) => (
        <rect key={i} className="pv-chunk" style={{ animationDelay: `${i * 0.9}s` }} x="52" y={34 + i * 30} width="30" height="22" rx="3" fill="none" stroke={INK2} strokeWidth="1.4" />
      ))}
      {cells}
      <path
        className="pv-wave"
        d="M8 138 q6 -12 12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0 t12 0"
        fill="none"
        stroke={TEAL}
        strokeWidth="1.4"
      />
      <desc>{pick(lang, "一份文档切成三块，逐块点亮向量点阵；空框=部分页只有词法索引不生成向量；底部音频波形压平成文字（有转录走文字向量，MM-D3）", "a document splits into three chunks lighting up a vector grid; hollow = lexical-only pages get no vector; the audio waveform flattens into text (transcript rides text vectors, MM-D3)")}</desc>
    </svg>
  );
}

/* ---------- search: query pulse → rays → score bars → evidence | zero ---------- */

export function SearchDemo({ beat, cycle, lang }: { beat: number; lang: Lang; cycle: number }) {
  const active = beat >= 3;
  const zero = cycle % 2 === 1;
  return (
    <svg viewBox="0 0 220 150" style={FRAME} aria-hidden="true" className="podviz">
      {zero ? (
        <g className="pv-zero">
          <circle cx="110" cy="72" r="20" fill="none" stroke={SUB} strokeWidth="1.5" strokeDasharray="5 6" />
          <line x1="96" y1="86" x2="124" y2="58" stroke={SUB} strokeWidth="1.5" />
        </g>
      ) : (
        <g>
          <g className="pv-query">
            <circle cx="206" cy="72" r="6" fill={TEAL} />
          </g>
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
          <rect className="pv-evidence" x="44" y="116" width="110" height="24" rx="5" fill="none" stroke={active ? GOLD : SUB} strokeWidth="1.6" />
          <line x1="99" y1="45" x2="99" y2="114" stroke={GOLD} strokeWidth="1.2" strokeDasharray="3 4" opacity="0.7" />
        </g>
      )}
      <desc>{pick(lang, "一个查询脉冲从右缘进入（查询只从检索进），射线打到三个候选按分亮条，最长一条连到证据卡；隔一轮全空——零命中也是合法答案（exit 0）", "a query pulse enters from the right (queries only enter at search), rays hit three candidates scored as bars, the top links to an evidence card; every other round is empty — zero-hit is a legal answer (exit 0)")}</desc>
    </svg>
  );
}
