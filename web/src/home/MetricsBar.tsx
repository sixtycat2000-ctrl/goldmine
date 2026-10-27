import { Counter } from "../components/motion/Counter";
import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";
import { LEDGER, SUITE } from "../lib/metrics";

/**
 * The Performance & Architecture bar: counters animate in on intersection,
 * followed by the receipts ledger. Every figure is transcribed from the
 * verified v3 page; see lib/metrics.ts for the verify: anchors.
 */
export function MetricsBar() {
  const lang = useLang();
  const zh = lang === "zh";

  const tiles: { value: React.ReactNode; label: string; gold?: boolean }[] = [
    {
      value: <><Counter end={3200} />+</>,
      label: zh ? "离线测试 · 全量 8 分钟" : "OFFLINE TESTS · 8-MIN SUITE",
    },
    {
      value: <><Counter end={155} />/<Counter end={180} /></>,
      label: zh ? "严格成功@5（官方跑）" : "STRICT SUCCESS@5 (OFFICIAL)",
    },
    {
      value: <><Counter end={17} />/<Counter end={19} /></>,
      label: zh ? "质量门通过 · 2 门写明原因" : "GATES PASSING · 2 REASONED",
      gold: true,
    },
    {
      value: <><Counter end={420} /> · {SUITE.frozenSources}</>,
      label: zh ? "冻结评测题 · 来源文件" : "FROZEN EVAL QS · SOURCE FILES",
    },
    {
      value: <><Counter end={1.4} decimals={1} />s</>,
      label: zh ? "每题中位耗时（QMD 21.8s）" : "MEDIAN PER QUERY (QMD 21.8s)",
    },
    {
      value: <>0 <span className="text-base">B</span></>,
      label: zh ? "上传 · 代码里没有这条路径" : "UPLOADED · NO SUCH CODE PATH",
      gold: true,
    },
  ];

  return (
    <section id="numbers" className="hairline-t hairline-b bg-bg1/40">
      <div className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
        <SectionHead
          no="03"
          kicker={{ zh: "实测数字", en: "MEASURED NUMBERS" }}
          title={{ zh: "数字与回执", en: "Numbers, with receipts" }}
          lede={{
            zh: "这里没有一行营销数字——每一项都能在仓库文档与执行回执（docs/FEATURE_STATUS.md、docs/COMPARISON.md、reports/execution/）里对到原始出处。",
            en: "No marketing numbers here — every figure traces to its raw source in the repo (docs/FEATURE_STATUS.md, docs/COMPARISON.md, reports/execution/).",
          }}
        />
        <div className="grid grid-cols-2 gap-x-6 gap-y-10 sm:grid-cols-3 lg:grid-cols-6">
          {tiles.map((t, i) => (
            <Reveal key={t.label} delay={i * 0.05} y={14}>
              <div className={`font-display text-3xl lg:text-[2rem] ${t.gold ? "text-gold" : "text-ink"}`}>{t.value}</div>
              <div className="mt-2 font-mono text-[10px] leading-relaxed tracking-[0.12em] text-sub uppercase">
                {t.label}
              </div>
            </Reveal>
          ))}
        </div>

        <Reveal y={0} duration={0.5} className="mt-16 overflow-x-auto">
          <div
            className="no-scrollbar hscroll overflow-x-auto"
            tabIndex={0}
            role="region"
            aria-label={zh ? "实测数字台账" : "measured-numbers ledger"}
          >
          <table className="w-full min-w-[46rem] border-collapse text-left">
            <thead>
              <tr className="hairline-b">
                <th scope="col" className="py-3 pr-4 font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
                  {zh ? "编号" : "NO."}
                </th>
                <th scope="col" className="py-3 pr-4 text-right font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
                  {zh ? "实测" : "MEASURED"}
                </th>
                <th scope="col" className="py-3 font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
                  {zh ? "回执与说明" : "RECEIPT & NOTE"}
                </th>
              </tr>
            </thead>
            <tbody>
              {LEDGER.map((row) => (
                <tr key={row.no} className="hairline-b align-top transition-colors hover:bg-bg2/60">
                  <td className="py-4 pr-4 font-mono text-[12px] text-sub">{row.no}</td>
                  <td className="py-4 pr-4 text-right font-mono text-[13px] whitespace-nowrap text-ink">
                    {typeof row.value === "string" ? row.value : pick(lang, row.value.zh, row.value.en)}
                  </td>
                  <td className="max-w-[42em] py-4 text-[13px] leading-relaxed text-ink2">
                    {pick(lang, row.note.zh, row.note.en)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
