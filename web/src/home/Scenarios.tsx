import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { useLang, pick } from "../lib/lang";

const ITEMS = [
  {
    title: { zh: "上周说退款的会议录音", en: "The meeting recording that mentioned refunds" },
    body: {
      zh: "whisper 转成带时间戳的文本。搜「退款期限」，open 直接跳到那一秒。",
      en: "whisper transcribes it with timestamps. Search 退款期限, open jumps to the second.",
    },
  },
  {
    title: { zh: "截图里的那段报错", en: "The error message in a screenshot" },
    body: {
      zh: "Vision OCR 把像素读成可搜的原文；图像向量再兜一层「长得像」。",
      en: "Vision OCR reads the pixels into searchable text; the image vector adds a looks-like layer.",
    },
  },
  {
    title: { zh: "两百页 PDF 里的那个编号", en: "The identifier inside that 200-page PDF" },
    body: {
      zh: "编号类查询自动反转权重，词法精确命中压过语义——数字不会被「意思相近」淹没。",
      en: "Identifier-like queries invert the weights: exact lexical hits beat semantics, so numbers don't drown in vibes.",
    },
  },
];

export function Scenarios() {
  const lang = useLang();
  return (
    <section id="scenarios" className="mx-auto max-w-6xl px-5 py-24 sm:py-32">
      <SectionHead
        title={{ zh: "三件每天都会发生的事", en: "Three things that happen every week" }}
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
