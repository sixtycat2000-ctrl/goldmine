import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";

const ITEMS = [
  {
    title: { zh: "录音里说过的那句话", en: "A sentence in a recording" },
    body: {
      zh: "配置本地转写工具后，按词句搜索带时间戳的转写，再核对对应录音片段。",
      en: "With local transcription configured, search the timestamped transcript and check the corresponding part of the recording.",
    },
  },
  {
    title: { zh: "截图里的那段报错", en: "The error message in a screenshot" },
    body: {
      zh: "启用 Vision OCR 后，可搜索截图中提取的文字。图像内容检索另需配置媒体模型。",
      en: "Enable Vision OCR to search extracted screenshot text. Visual retrieval requires the media model separately.",
    },
  },
  {
    title: { zh: "两百页 PDF 里的那个编号", en: "The identifier inside that 200-page PDF" },
    body: {
      zh: "用完整编号查询，核对命中的原文与页码。编号匹配会检查实际存储文本。",
      en: "Search for the complete identifier and inspect its text and page. Identifier matching checks the actual stored text.",
    },
  },
];

export function Scenarios() {
  const lang = useLang();
  return (
    <section id="scenarios" className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
      <SectionHead
        title={{ zh: "从记得的一点线索开始", en: "Start with the clue you remember" }}
      />
      <ol className="grid gap-4 md:grid-cols-3">
        {ITEMS.map((it, i) => (
          <li key={it.title.zh} className="list-none">
            <Reveal delay={i * 0.12} y={32} duration={0.75} className="h-full">
              <div className="group relative h-full overflow-hidden rounded-2xl bg-bg1 p-6">
                <h3 className="font-display text-title leading-snug text-ink">
                  {pick(lang, it.title.zh, it.title.en)}
                </h3>
                <p className="mt-3 text-[14px] leading-relaxed text-ink2">{pick(lang, it.body.zh, it.body.en)}</p>
              </div>
            </Reveal>
          </li>
        ))}
      </ol>
    </section>
  );
}
