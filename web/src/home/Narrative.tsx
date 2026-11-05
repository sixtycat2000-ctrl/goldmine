import { useLang, pick } from "../lib/lang";
import { SectionHead } from "../components/SectionHead";
import { CopyButton } from "../components/CopyButton";
import { Reveal } from "../components/motion/Reveal";

const STEPS = [
  {
    tag: { zh: "选择", en: "CHOOSE" },
    title: { zh: "选好要查的文件夹", en: "Choose the folders to search" },
    body: {
      zh: "从资料所在的目录开始建库，无需扫描整个磁盘。未下载的 iCloud 占位文件会跳过，并留下原因。",
      en: "Build an index from your chosen folders, without scanning the whole disk. Undownloaded iCloud placeholders are skipped with a reason.",
    },
  },
  {
    tag: { zh: "建库", en: "INDEX" },
    title: { zh: "先试词法，再按需扩展", en: "Start with words; add capabilities as needed" },
    body: {
      zh: "词法检索不需要模型。语义检索需配置文本嵌入模型；截图文字、录音转写与图像检索各需相应的本地工具或模型。",
      en: "Lexical search needs no model. Semantic search requires a text embedding model; OCR, transcription and visual retrieval each need their corresponding local tools or models.",
    },
  },
  {
    tag: { zh: "核对", en: "CHECK" },
    title: { zh: "从结果回到原文", en: "Follow the result back to its source" },
    body: {
      zh: "查看命中片段、来源文件，以及已有的页码或时间位置。需要解释排序时，加上 --why 查看打分与命中依据。",
      en: "Inspect the excerpt, source file and available page or time location. Add --why when you want to examine scoring and evidence.",
    },
  },
] as const;

export function Narrative() {
  const lang = useLang();
  return (
    <section id="how" className="relative">
      <div className="mx-auto max-w-6xl px-5 pt-24 sm:pt-32">
        <SectionHead title={{ zh: "从一个文件夹开始", en: "Start with one folder" }} />
        <ol className="mt-4">
          {STEPS.map((step) => (
            <li key={step.tag.en}>
              <Reveal y={20}>
                <div className="grid gap-2 py-7 sm:grid-cols-[9rem_1fr] sm:gap-10 sm:py-9">
                  <div className="font-display text-2xl leading-none text-gold sm:text-3xl">{pick(lang, step.tag.zh, step.tag.en)}</div>
                  <div>
                    <h3 className="font-display text-title leading-snug text-ink">{pick(lang, step.title.zh, step.title.en)}</h3>
                    <p className="mt-3 max-w-[36em] text-[15px] leading-relaxed text-ink2">{pick(lang, step.body.zh, step.body.en)}</p>
                  </div>
                </div>
              </Reveal>
            </li>
          ))}
        </ol>
        <p className="mt-4 mb-12 max-w-3xl rounded-xl border border-line bg-bg1 px-5 py-4 text-[13px] leading-relaxed text-sub">
          {pick(lang, "备份或迁移时，用 ", "To back up or move an index, export a consistent snapshot with ")}
          <code className="break-words text-ink2">goldmine backup --db s.sqlite --out s.backup.sqlite</code>
          {pick(lang, " 导出一致快照，再复制。恢复前先停止使用该索引的服务。", ", then copy it. Stop services using the index before restoring.")}
        </p>
      </div>
      <Quickstart />
    </section>
  );
}

const QUICKSTART = [
  "git clone https://github.com/sixtycat2000-ctrl/goldmine && cd goldmine",
  "python3 -m venv .venv && .venv/bin/python -m pip install .",
  "mkdir -p sample && printf '知识库：退款期限为四十五天。\\n' > sample/notes.md",
  ".venv/bin/goldmine ingest sample --db s.sqlite --lexical-only",
  ".venv/bin/goldmine search '退款期限' --db s.sqlite --lexical --pack",
];

function Quickstart() {
  const lang = useLang();
  return (
    <div className="mx-auto max-w-6xl px-5 pb-24 sm:pb-32">
      <div className="mx-auto max-w-3xl">
        <p className="mb-4 text-[13px] leading-relaxed text-sub">
          {pick(lang, "macOS · Python 3.12+。先用示例文件试词法检索，无需下载模型。", "macOS · Python 3.12+. Try lexical search on a sample file before downloading any models.")}
        </p>
        <div className="overflow-hidden rounded-2xl border border-line bg-bg1">
          <div className="hairline-b flex items-center justify-between px-4 py-2.5">
            <span className="text-[12px] text-sub">{pick(lang, "词法检索入门", "Lexical quickstart")}</span>
            <CopyButton text={QUICKSTART.join("\n")} />
          </div>
          <pre className="overflow-x-auto p-4 font-mono text-[12px] leading-7">
            {QUICKSTART.map((line) => (
              <code key={line} className="block text-ink2">
                <span className="mr-3 text-gold/70 select-none">$</span>{line}
              </code>
            ))}
          </pre>
        </div>
      </div>
    </div>
  );
}
