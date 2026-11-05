import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";
import { HISTORICAL_QMD_URL, QMD_HEAD_TO_HEAD } from "../lib/metrics";
import { IconExt } from "../components/Icons";

export function HeadToHead() {
  const lang = useLang();
  return (
    <section id="compare" className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
      <SectionHead
        title={{ zh: "同一批资料，两种输入方式", en: "One corpus, two input views" }}
        lede={{
          zh: "2026-10-25历史对照：同122份来源、210条开发查询。保留原件与统一预处理文本，分别比较Goldmine和QMD 2.8.3。",
          en: "Historical comparison from 2026-10-25: the same 122 sources and 210 development queries, with native files and common pre-converted text evaluated separately against QMD 2.8.3.",
        }}
      />
      <Reveal y={16}>
        <div
          className="no-scrollbar hscroll overflow-x-auto rounded-2xl border border-line bg-bg1/60"
          tabIndex={0}
          role="region"
          aria-label={pick(lang, "2026-10-25历史QMD对照表，可横向滚动", "Historical QMD comparison, 2026-10-25; scroll horizontally")}
        >
          <table className="w-full min-w-[36rem] border-collapse text-left text-[13px]">
            <thead>
              <tr className="hairline-b">
                <th scope="col" className="px-4 py-3.5 font-normal text-sub">{pick(lang, "范围与指标", "Scope and metric")}</th>
                <th scope="col" className="px-4 py-3.5 text-right text-gold">goldmine</th>
                <th scope="col" className="px-4 py-3.5 text-right text-sub">QMD 2.8.3 query</th>
              </tr>
            </thead>
            <tbody>
              {QMD_HEAD_TO_HEAD.rows.map((row) => (
                <tr key={row.metric.zh} className="hairline-b transition-colors last:border-0 hover:bg-bg2/60">
                  <th scope="row" className="max-w-[26em] px-4 py-3.5 font-normal text-ink">{pick(lang, row.metric.zh, row.metric.en)}</th>
                  <td className={`px-4 py-3.5 text-right ${row.winner === "goldmine" ? "font-semibold text-gold" : "text-ink2"}`}>{row.goldmine}</td>
                  <td className={`px-4 py-3.5 text-right ${row.winner === "qmd" ? "font-semibold text-ink" : "text-sub"}`}>
                    {row.qmd}
                    {"qmdNote" in row && row.qmdNote && (
                      <span className="mt-1 block text-[12px] font-normal text-sub">{pick(lang, row.qmdNote.zh, row.qmdNote.en)}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-5 max-w-4xl space-y-3 text-[13px] leading-relaxed text-sub">
          <p>{pick(lang,
            "成功@5：180道可回答问题中，前5个结果至少命中一个正确来源的比例。噪声另计30道。除噪声范围行外，Goldmine使用fast；QMD使用query。",
            "Success@5 is the fraction of 180 answerable questions with a correct source among the first five results. Noise is measured on 30 separate questions. Goldmine uses fast except in the noise range; QMD uses query.",
          )}</p>
          <p>{pick(lang,
            "耗时来自该轮质量跑逐题CLI调用的附带观察，包含模型加载。正式性能与增量测量未完成；这些历史耗时不代表当前版本或常驻服务的速度。",
            "Times are incidental observations of per-query CLI calls in that quality run, including model loading. The formal performance and incremental measurements were incomplete; these times do not establish current-version or resident-service latency.",
          )}</p>
          <a href={HISTORICAL_QMD_URL} className="inline-flex items-center gap-1 text-teal underline decoration-teal/40 underline-offset-4 hover:text-ink" target="_blank" rel="noopener noreferrer">
            {pick(lang, "查看历史配置、视图与证据范围", "Read the historical configurations, views and evidence scope")} <IconExt size={12} />
          </a>
        </div>
      </Reveal>
    </section>
  );
}
