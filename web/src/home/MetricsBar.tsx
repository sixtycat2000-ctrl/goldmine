import { Counter } from "../components/motion/Counter";
import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";
import { FEATURE_STATUS_URL, LEDGER, SUITE } from "../lib/metrics";

export function MetricsBar() {
  const lang = useLang();
  const zh = lang === "zh";
  const tiles = [
    {
      value: <Counter end={SUITE.testsCount} />,
      label: pick(lang, SUITE.testsNote.zh, SUITE.testsNote.en),
    },
    {
      value: SUITE.strictSuccess,
      label: zh ? "2026-10-26 · 开发集严格成功@5" : "2026-10-26 · development strict success@5",
    },
    {
      value: `${SUITE.gatesPassed} / ${SUITE.gatesTotal}`,
      label: zh ? "同期质量门 · 2项未过" : "Quality gates in that run · 2 failed",
    },
  ];

  return (
    <section id="numbers" className="hairline-t hairline-b bg-bg1/40">
      <div className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
        <SectionHead
          title={{ zh: "这些数字测了什么", en: "What these numbers measure" }}
          lede={{
            zh: "功能测试验证实现边界，检索评测检查固定问题上的命中情况。下面保留各自的日期与范围。",
            en: "Functional tests check implementation boundaries; retrieval evaluations check a fixed set of questions. Each record keeps its own date and scope.",
          }}
        />
        <div className="grid grid-cols-1 gap-x-8 gap-y-8 sm:grid-cols-3">
          {tiles.map((tile, i) => (
            <Reveal key={tile.label} delay={i * 0.05} y={14}>
              <div className="font-display text-3xl text-gold lg:text-[2rem]">{tile.value}</div>
              <div className="mt-2 text-[13px] leading-relaxed text-sub">{tile.label}</div>
            </Reveal>
          ))}
        </div>
        <Reveal y={0} duration={0.5} className="mt-12">
          <ol className="divide-y divide-line">
            {LEDGER.map((row) => (
              <li key={row.no} className="grid gap-3 py-6 sm:grid-cols-[12rem_1fr] sm:gap-8">
                <p className="font-display text-xl text-ink">{typeof row.value === "string" ? row.value : pick(lang, row.value.zh, row.value.en)}</p>
                <p className="max-w-[48em] text-[13px] leading-relaxed text-ink2">{pick(lang, row.note.zh, row.note.en)}</p>
              </li>
            ))}
          </ol>
          <p className="mt-5 max-w-3xl text-[13px] leading-relaxed text-sub">
            {pick(lang, "历史成绩不代表你的资料上的命中率；当前版本的完整CLI/MCP耗时与检索质量提升仍需单独测量。", "Historical results do not predict accuracy on your files. Current whole-command CLI/MCP latency and retrieval-quality gains still require separate measurement.")}{" "}
            <a href={FEATURE_STATUS_URL} target="_blank" rel="noopener noreferrer" className="text-teal underline decoration-teal/40 underline-offset-4 hover:text-ink">
              {pick(lang, "查看测试与评测范围", "Read the testing and evaluation scope")} ↗
            </a>
          </p>
        </Reveal>
      </div>
    </section>
  );
}
