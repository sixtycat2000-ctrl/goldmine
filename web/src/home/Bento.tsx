import { useLang, pick } from "../lib/lang";
import { SpotlightCard } from "../components/motion/SpotlightCard";
import { TiltCard } from "../components/motion/TiltCard";
import { Reveal } from "../components/motion/Reveal";
import { SectionHead } from "../components/SectionHead";
import { IconCopy, IconExt, IconLogo, IconSearch } from "../components/Icons";

const FEATURES = [
  {
    icon: IconSearch,
    title: { zh: "不必记住文件名", en: "You needn't remember the filename" },
    body: {
      zh: "用词句或完整编号查找内容，查看命中片段与出处，以及附带的页码或时间位置。",
      en: "Look up content with words or complete identifiers, then inspect the excerpt, source and available page or time location.",
    },
    tags: [{ zh: "词句", en: "Words" }, { zh: "完整编号", en: "Identifiers" }, { zh: "原文出处", en: "Sources" }],
  },
  {
    icon: IconCopy,
    title: { zh: "截图和录音也能查", en: "Search screenshots and recordings" },
    body: {
      zh: "启用所需本地工具后，可检索截图中的文字和录音转写。图像内容检索另需配置媒体模型。",
      en: "With the required local tools, search screenshot text and recording transcripts. Visual retrieval also requires the media model.",
    },
    tags: [{ zh: "截图文字", en: "OCR text" }, { zh: "录音转写", en: "Transcripts" }, { zh: "可选媒体模型", en: "Optional media model" }],
  },
  {
    icon: IconLogo,
    title: { zh: "本机建库，本机检索", en: "Index and search on your Mac" },
    body: {
      zh: "只处理你选定的文件夹。未下载的 iCloud 文件会跳过；用 status 查看处理状态、缺少的能力与原因。",
      en: "Process the folders you choose. Undownloaded iCloud files are skipped; status shows processing states, missing capabilities and reasons.",
    },
    tags: [{ zh: "选定范围", en: "Chosen folders" }, { zh: "本地索引", en: "Local index" }, { zh: "处理记录", en: "File status" }],
  },
  {
    icon: IconExt,
    title: { zh: "让助手查询选定索引", en: "Let your assistant query a chosen index" },
    body: {
      zh: "通过 MCP 返回原文与引用给调用方。证据交给助手后，是否继续发送到云端取决于客户端。",
      en: "MCP returns source text and citations to its caller. Any onward transmission to a cloud service is controlled by that client.",
    },
    tags: [{ zh: "MCP 接入", en: "MCP access" }, { zh: "四个只读工具", en: "Four read-only tools" }, { zh: "可核对引用", en: "Verifiable citations" }],
  },
] as const;

export function Bento() {
  const lang = useLang();
  return (
    <section id="features" className="relative mx-auto max-w-6xl px-5 py-24 sm:py-32">
      <SectionHead
        title={{ zh: "找到内容，也看得到出处", en: "Find the content. Inspect the source." }}
        lede={{
          zh: "文档、截图与录音放进同一索引，按需要启用相应能力。",
          en: "Keep documents, screenshots and recordings in one index, enabling the capabilities you need.",
        }}
      />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {FEATURES.map((feature, i) => {
          const Icon = feature.icon;
          return (
            <Reveal key={feature.title.en} y={18} delay={(i % 2) * 0.06}>
              <TiltCard className="h-full">
                <SpotlightCard className="flex h-full flex-col p-6 sm:p-8">
                  <span className="mb-6 flex h-11 w-11 items-center justify-center rounded-xl border border-gold/20 bg-gold/5 text-gold">
                    <Icon size={23} />
                  </span>
                  <h3 className="font-display text-title leading-snug text-ink">{pick(lang, feature.title.zh, feature.title.en)}</h3>
                  <p className="mt-3 max-w-[35em] text-[14px] leading-relaxed text-ink2">{pick(lang, feature.body.zh, feature.body.en)}</p>
                  <div className="mt-auto flex flex-wrap gap-2 pt-6">
                    {feature.tags.map((tag) => (
                      <span key={tag.en} className="rounded-full border border-line bg-bg2/60 px-3 py-1 text-[12px] text-sub">
                        {pick(lang, tag.zh, tag.en)}
                      </span>
                    ))}
                  </div>
                </SpotlightCard>
              </TiltCard>
            </Reveal>
          );
        })}
      </div>
    </section>
  );
}
