import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";
import { COMPARE_TABLE, QMD_HEAD_TO_HEAD, REPO_URL } from "../lib/metrics";
import { IconYes, IconOk, IconNo, IconExt } from "../components/Icons";

type Cell = string | { zh: string; en: string };

function CellView({ cell }: { cell: Cell }) {
  const lang = useLang();
  if (cell === "yes")
    return <IconYes size={17} className="text-gold" label={lang === "zh" ? "支持" : "yes"} />;
  if (cell === "ok")
    return <IconOk size={17} className="text-ink2" label={lang === "zh" ? "部分支持" : "partial"} />;
  if (cell === "no")
    return <IconNo size={17} className="text-sub" label={lang === "zh" ? "不支持" : "no"} />;
  if (cell === "—") return <span className="text-sub">—</span>;
  return <span className="text-[12px] text-ink2">{pick(lang, (cell as { zh: string; en: string }).zh, (cell as { zh: string; en: string }).en)}</span>;
}

function CompareTable() {
  const lang = useLang();
  return (
    <div
      className="no-scrollbar hscroll overflow-x-auto rounded-2xl border border-line bg-bg1/60"
      tabIndex={0}
      role="region"
      aria-label={lang === "zh" ? "能力对比表" : "capability comparison"}
    >
      <table className="w-full min-w-[44rem] border-collapse text-left text-[13px]">
        <thead>
          <tr className="hairline-b">
            {COMPARE_TABLE.columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={`px-4 py-3.5 font-mono text-[10px] tracking-[0.14em] uppercase ${c.key === "gm" ? "text-gold" : "text-sub"}`}
              >
                {pick(lang, c.label.zh, c.label.en)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {COMPARE_TABLE.rows.map((r) => (
            <tr key={r.cap.zh} className="hairline-b transition-colors last:border-0 hover:bg-bg2/60">
              <th scope="row" className="max-w-[18em] px-4 py-3.5 font-normal text-ink">
                {pick(lang, r.cap.zh, r.cap.en)}
              </th>
              <td className="bg-gold/[0.05] px-4 py-3.5">
                <span className="inline-flex items-center gap-2">
                  <CellView cell={"gm" in r ? r.gm : "—"} />
                  {"gmNote" in r && r.gmNote && (
                    <span className="font-mono text-[10px] text-sub">{pick(lang, r.gmNote.zh, r.gmNote.en)}</span>
                  )}
                </span>
              </td>
              <td className="px-4 py-3.5">
                <span className="inline-flex items-center gap-2">
                  <CellView cell={r.spot} />
                  {"spotNote" in r && r.spotNote && <span className="font-mono text-[10px] text-sub">*</span>}
                </span>
              </td>
              <td className="px-4 py-3.5">
                <CellView cell={r.cloud} />
              </td>
              <td className="px-4 py-3.5">
                <CellView cell={r.grep} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QmdTable() {
  const lang = useLang();
  return (
    <div
      className="no-scrollbar hscroll overflow-x-auto rounded-2xl border border-line bg-bg1/60"
      tabIndex={0}
      role="region"
      aria-label={lang === "zh" ? "QMD 对比表" : "QMD head-to-head"}
    >
      <table className="w-full min-w-[36rem] border-collapse text-left text-[13px]">
        <thead>
          <tr className="hairline-b">
            <th scope="col" className="px-4 py-3.5" />
            <th scope="col" className="px-4 py-3.5 text-right font-mono text-[10px] tracking-[0.14em] text-gold uppercase">
              goldmine
            </th>
            <th scope="col" className="px-4 py-3.5 text-right font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
              QMD 2.8.3
            </th>
          </tr>
        </thead>
        <tbody>
          {QMD_HEAD_TO_HEAD.rows.map((r) => (
            <tr key={r.metric.zh} className="hairline-b transition-colors last:border-0 hover:bg-bg2/60">
              <th scope="row" className="max-w-[24em] px-4 py-3.5 font-normal text-ink">
                {pick(lang, r.metric.zh, r.metric.en)}
              </th>
              <td className={`px-4 py-3.5 text-right font-mono ${r.winner === "goldmine" ? "font-bold text-gold" : "text-ink2"}`}>
                {r.goldmine}
                {r.winner === "goldmine" && <span className="sr-only">{lang === "zh" ? "（更优）" : " (wins)"}</span>}
              </td>
              <td className="px-4 py-3.5 text-right font-mono">
                <span className={r.winner === "qmd" ? "font-bold text-ink" : "text-sub"}>
                  {r.qmd}
                  {"qmdNote" in r && r.qmdNote && (
                    <span className="ml-2 text-[10px]">{pick(lang, r.qmdNote.zh, r.qmdNote.en)}</span>
                  )}
                </span>
                {r.winner === "qmd" && <span className="sr-only">{lang === "zh" ? "（更优）" : " (wins)"}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function HeadToHead() {
  const lang = useLang();
  return (
    <section id="compare" className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
      <SectionHead
        no="05"
        kicker={{ zh: "硬碰硬", en: "HEAD-TO-HEAD" }}
        title={{ zh: "和手里的工具比，和对另一个引擎比", en: "Against your tools, and against another engine" }}
        lede={{
          zh: "表里没有一项是营销话术。QMD 对比钉在 2.8.3 版：同 122 份真实文件、同 210 道题。",
          en: "Nothing here is marketing. The QMD comparison is pinned to 2.8.3: same 122 real files, same 210 questions.",
        }}
      />
      <Reveal>
        <CompareTable />
        <p className="mt-4 max-w-[52em] text-[12px] leading-relaxed text-sub">
          {pick(lang, COMPARE_TABLE.footnote.zh, COMPARE_TABLE.footnote.en)}
        </p>
      </Reveal>
      <div className="h-12" />
      <Reveal>
        <QmdTable />
        <p className="mt-4 font-mono text-[11px] text-sub">
          {pick(
            lang,
            "完整矩阵、失败与岔子、逐行回执：仓库 docs/COMPARISON.md 与 reports/execution/P2-08/。",
            "Full matrix, failures and all: docs/COMPARISON.md and reports/execution/P2-08/ in the repository.",
          )}{" "}
          <a href={`${REPO_URL}#readme`} className="inline-flex items-center gap-1 text-teal hover:text-ink" target="_blank" rel="noopener noreferrer">
            repo <IconExt size={11} />
          </a>
        </p>
      </Reveal>
    </section>
  );
}
