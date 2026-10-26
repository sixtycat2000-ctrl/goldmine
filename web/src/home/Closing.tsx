import { MagneticButton } from "../components/motion/MagneticButton";
import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang } from "../lib/lang";
import { REPO_URL } from "../lib/metrics";

export function Closing() {
  const lang = useLang();
  const zh = lang === "zh";
  return (
    <>
      <section id="limits" className="hairline-t bg-bg1/40">
        <div className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
          <SectionHead
            no="06"
            kicker={{ zh: "诚实的边界", en: "HONEST LIMITS" }}
            title={{ zh: "0.2.0 · development——把未过的门写出来", en: "0.2.0 · development — the failed gates are published" }}
            lede={{
              zh: "严格成功@5 155/180；19 项质量门过 17。两门未过都是已归档的硬上限：noise 20/30（陷阱虚构在问题前提层，无词可查）、media-OCR 6/9（冻结评测器 32 字符对齐最小窗 + OCR 内容缺口）。我们认为「敢把失败写清楚」本身就是检索质量的证据。",
              en: "Strict success@5 155/180; 17 of 19 quality gates pass. Both fails are archived hard limits: noise 20/30 (the fabricated premise has no verbatim token to check) and media-OCR 6/9 (frozen-evaluator 32-char alignment floor + an OCR content gap). We believe publishing failures plainly is itself evidence of retrieval quality.",
            }}
          />
        </div>
      </section>

      <section id="opensource" className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
        <SectionHead
          no="07"
          kicker={{ zh: "开源", en: "OPEN SOURCE" }}
          title={{
            zh: "MIT。代码、文档、评测与执行记录都在仓库里。",
            en: "MIT. Code, docs, evals and execution records live in the repo.",
          }}
          lede={{
            zh: "代码之外，仓库带着完整的工作记录：能力与质量台账（FEATURE_STATUS）、五讲子系统走读、术语考古词典（JOURNEY）、锚点命令卡（一分钟亲手验证）、逐提交的执行档案。想审计，从这里开始。",
            en: "Beyond code, the repo carries its full working record: the capability ledger (FEATURE_STATUS), five subsystem walkthroughs, a term-archaeology glossary (JOURNEY), anchor command cards (verify by hand in a minute), and per-commit execution archives. Start an audit there.",
          }}
        />
        <Reveal>
          <div className="flex flex-wrap items-center gap-2">
            <MagneticButton variant="gold" href={REPO_URL} external>
              GitHub ↗
            </MagneticButton>
            <MagneticButton variant="ghost" href="/docs">
              {zh ? "文档与路线图" : "Docs & roadmap"} →
            </MagneticButton>
            <MagneticButton variant="ghost" href="/learn">
              {zh ? "Learn：145 个概念，逐层下探" : "Learn: 145 concepts, layer by layer"}
            </MagneticButton>
          </div>
        </Reveal>
      </section>
    </>
  );
}
