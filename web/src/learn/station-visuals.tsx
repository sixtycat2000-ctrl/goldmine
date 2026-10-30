/*
 * Station micro-demos (learn-stage-plan-001, amendments C1/C2/C6).
 * Pure functions of `beat` — the single timebase lives in Stage; nothing here
 * owns a timer. Every demo carries the visible 示意 · 简化演示 tag and the
 * fixed-sample caption. Consequences are per-layer (identity) and the search
 * loop ends in zero-hit on alternate cycles — the honest outcomes, not the
 * happy path only.
 */
import { pick, type Lang } from "../lib/lang";

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

/* ---- identity: four layers, four DIFFERENT consequences (C1) ---- */

const ID_LAYERS = [
  { l: "L1", zh: "索引级指纹", en: "index fingerprint", miss_zh: "不符 → 整库拒收 · exit 3", miss_en: "mismatch → whole index refused · exit 3" },
  { l: "L2", zh: "模型身份", en: "model identity", miss_zh: "不符 → 仅这些行重新嵌入", miss_en: "mismatch → only these rows re-embed" },
  { l: "L3", zh: "抽取器版本", en: "extractor version", miss_zh: "不符 → 重新抽取文本", miss_en: "mismatch → text re-extracted" },
  { l: "L4", zh: "评测基线身份", en: "eval baseline", miss_zh: "— 管评测分母，不裁决摄取", miss_en: "— governs eval denominators, never ingest" },
];

export function IdentityDemo({ beat, lang }: { beat: number; lang: Lang }) {
  // beat 1 = the wave is at identity: layer checks light up in sequence
  const active = beat === 1;
  return (
    <div className="mt-3 rounded-xl bg-bg1/70 p-3" aria-hidden="true">
      <div className="space-y-1">
        {ID_LAYERS.map((row, i) => (
          <div
            key={row.l}
            className={`rounded-md px-2 py-1 font-mono text-[13px] leading-snug transition-colors duration-500 ${
              active && i === 0 ? "bg-fail/15" : ""
            }`}
          >
            <span className={active && i === 0 ? "text-ink" : "text-ink2"}>
              {row.l} · {pick(lang, row.zh, row.en)}
            </span>
            <span className={`block ${active && i === 0 ? "text-ink" : "text-sub"}`}>{pick(lang, row.miss_zh, row.miss_en)}</span>
          </div>
        ))}
      </div>
      <p className="mt-2 font-mono text-[13px] text-sub">
        {pick(lang, "样本：L1 不符 → exit 3，什么都进不了库", "sample: L1 mismatch → exit 3, nothing enters the index")}
      </p>
    </div>
  );
}

/* ---- search: score bars → evidence OR zero-hit (C2) ---- */

export function SearchDemo({ beat, cycle, lang }: { beat: number; cycle: number; lang: Lang }) {
  const resolving = beat >= 3;
  const zeroHit = cycle % 2 === 1;
  return (
    <div className="mt-3 rounded-xl bg-bg1/70 p-3" aria-hidden="true">
      <p className="font-mono text-[13px] text-ink2">
        <span className="text-gold">›</span> {pick(lang, "退款期限是多久？", "how long is the refund window?")}
      </p>
      <div className="mt-2 space-y-1">
        {[
          { w: "86%", hit: true },
          { w: "41%", hit: true },
          { w: "12%", hit: false },
        ].map((b, i) => (
          <div key={i} className="h-1.5 overflow-hidden rounded-full bg-bg3">
            <div
              className={`h-full rounded-full transition-[width] duration-700 ${b.hit ? "bg-gold/80" : "bg-sub/50"}`}
              style={{ width: resolving ? b.w : "0%" }}
            />
          </div>
        ))}
      </div>
      <p className={`mt-2 font-mono text-[13px] transition-opacity duration-500 ${resolving ? "opacity-100" : "opacity-0"}`}>
        {zeroHit ? (
          <span className="text-sub">{pick(lang, "nothing found · 零命中也是合法答案（exit 0）", "nothing found · zero-hit is a legal answer (exit 0)")}</span>
        ) : (
          <span className="text-teal">{pick(lang, "命中 · 证据行 + 为什么是它", "hit · evidence line + why it matched")}</span>
        )}
      </p>
    </div>
  );
}

/* ---- walk / understand: quiet states (A4) ---- */

export function WalkQuiet({ lang }: { lang: Lang }) {
  return (
    <div className="mt-3 rounded-xl bg-bg1/70 p-3" aria-hidden="true">
      <p className="font-mono text-[13px] leading-relaxed text-sub">
        notes/2026-04.md → <span className="text-ink2">wave 1</span>
        <br />
        receipts/q3.xlsx → <span className="text-ink2">wave 1</span>
        <br />
        scans/manual.pdf → <span className="text-ink2">wave 2</span>
      </p>
      <p className="mt-2 font-mono text-[13px] text-sub">
        {pick(lang, "一文件一事务 · 中断不剩半个文件", "one transaction per file · an interrupt leaves no half-file")}
      </p>
      <p className="mt-1 font-mono text-[13px] leading-relaxed text-sub">
        {pick(lang, "walk 门：iCloud dataless 跳过 · inode 移动检测", "walk gates: iCloud dataless skip · inode move detection")}
      </p>
    </div>
  );
}

export function UnderstandQuiet({ lang }: { lang: Lang }) {
  const dots = [1, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1];
  return (
    <div className="mt-3 rounded-xl bg-bg1/70 p-3" aria-hidden="true">
      <div className="grid grid-cols-12 gap-1.5" aria-hidden="true">
        {dots.map((d, i) => (
          <span key={i} className={`h-2.5 w-2.5 rounded-[3px] ${d ? "bg-cl-b/60" : "border border-line bg-transparent"}`} />
        ))}
      </div>
      <p className="mt-2 font-mono text-[13px] text-sub">
        {pick(
          lang,
          "点=向量 · 空框=部分页只有词法索引，不生成向量",
          "dot = vector · hollow = some pages are lexical-only, no vector",
        )}
      </p>
      {/* MM-D3 routing (honesty audit F7): with-transcript audio is text-vectored */}
      <p className="mt-1 font-mono text-[13px] leading-relaxed text-sub">
        {pick(
          lang,
          "有转录的音频走文字向量；无转录才走原始音频边车（逐行标记 asr_status）",
          "audio with a transcript rides text vectors; raw-audio sidecar only without one (asr_status marked per row)",
        )}
      </p>
    </div>
  );
}
