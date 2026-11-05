import { MagneticButton } from "../components/motion/MagneticButton";
import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";
import { FEATURE_STATUS_URL, REPO_URL } from "../lib/metrics";

export function Closing() {
  const lang = useLang();
  return (
    <>
      <section id="limits" className="hairline-t bg-bg1/40">
        <div className="mx-auto max-w-6xl px-5 py-20 sm:py-24">
          <SectionHead
            title={{ zh: "这些结果仍需人工核对", en: "Some results still need a closer look" }}
            lede={{
              zh: "2026-10-26开发集有两项质量门未过：无答案查询的空结果为20/30，OCR相关检索为6/9。原因包含OCR内容缺口与当时评测的对齐规则；它们是该次记录的限制。",
              en: "Two gates failed in the 2026-10-26 development run: 20/30 no-answer queries returned empty results, and OCR retrieval passed 6/9. Causes included missing OCR content and the evaluation's alignment rules; these are limits of that recorded run.",
            }}
          />
          <a href={FEATURE_STATUS_URL} target="_blank" rel="noopener noreferrer" className="text-[13px] text-teal underline decoration-teal/40 underline-offset-4 hover:text-ink">
            {pick(lang, "阅读已知限制与尚缺的验证", "Read the known limits and missing validation")} ↗
          </a>
        </div>
      </section>
      <section id="opensource" className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
        <SectionHead
          title={{ zh: "先用一份资料试一试", en: "Try it with one file" }}
          lede={{
            zh: "代码与文档以MIT协议公开。从词法检索开始，再按需要配置语义与媒体能力。",
            en: "Source and documentation are available under MIT. Start with lexical search, then configure semantic and media capabilities as needed.",
          }}
        />
        <Reveal>
          <div className="flex flex-wrap items-center gap-2">
            <MagneticButton variant="gold" href="/docs#start">{pick(lang, "安装与使用", "Install and start")} →</MagneticButton>
            <MagneticButton variant="ghost" href={REPO_URL} external>GitHub ↗</MagneticButton>
          </div>
        </Reveal>
      </section>
    </>
  );
}
