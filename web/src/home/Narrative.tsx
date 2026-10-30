import { useRef } from "react";
import { motion, useInView } from "framer-motion";
import { useLang, pick } from "../lib/lang";
import { SectionHead } from "../components/SectionHead";
import { CopyButton } from "../components/CopyButton";
import { Reveal } from "../components/motion/Reveal";

/* Four steps in plain document flow. The old pinned 600svh GSAP scrub
   corridor made the page fight the scroll wheel; steps read top to bottom
   now, each revealed as it enters. */

const STEPS = [
  {
    tag: { zh: "划定范围", en: "SCOPE" },
    title: { zh: "你指定文件夹，只索引这些", en: "You choose folders; only those are indexed" },
    body: {
      zh: "没有全盘扫描。构建产物、密钥、iCloud 未下载占位一律跳过并留原因——索引永远不会触发一次 iCloud 下载。",
      en: "No whole-disk scan. Build outputs, secrets and undownloaded iCloud placeholders are skipped with reasons. Indexing never triggers an iCloud download.",
    },
  },
  {
    tag: { zh: "向量化", en: "EMBED" },
    title: { zh: "内容变向量，每种媒体一条路", en: "Content becomes vectors; one path per modality" },
    body: {
      zh: "文本走钉版 embeddinggemma-2；装上可选边车后：图像/音频向量、视频约每 3 秒一帧（aHash 去重）、截图走 Vision OCR、录音走 whisper——命中直接跳到那一秒。",
      en: "Text uses the pinned embeddinggemma-2. With the optional sidecar: image/audio vectors, a video frame every ~3 s (aHash dedup), Vision OCR for screenshots, whisper for recordings. Hits jump to the second.",
    },
  },
  {
    tag: { zh: "融合检索", en: "FUSE" },
    title: { zh: "含义 + 精确词融合，算术可查", en: "Meaning + exact words fused, inspectably" },
    body: {
      zh: "score = 1.0·语义 z 分 + 2.0·词覆盖^1.5 + 类型加成；编号查询自动反转权重。--why 把每条命中的打分明细摆出来。",
      en: "score = 1.0·semantic z + 2.0·coverage^1.5 + kind bonus; identifier queries auto-invert. --why prints the score breakdown for every hit.",
    },
  },
  {
    tag: { zh: "落库", en: "COMMIT" },
    title: { zh: "一文件一事务，一库一文件", en: "One transaction per file, one file per index" },
    body: {
      zh: "每个文件的写入是单个 SQLite 事务——中断最坏丢这一个文件的这一次。备份、搬家、删除，就是复制那一个文件。",
      en: "Each file commits as one SQLite transaction: an interrupt loses at most that file's pass. Backup, moving, deletion: copy the one file.",
    },
  },
];

export function Narrative() {
  const lang = useLang();
  return (
    <section id="how" className="relative">
      <div className="mx-auto max-w-6xl px-5 pt-24 sm:pt-32">
        <SectionHead
          title={{ zh: "从磁盘到可搜，四步", en: "Disk to searchable, four steps" }}
          lede={{
            zh: "四步走完：每一步都是可查的真实机制。",
            en: "Four steps, each one a checkable mechanism.",
          }}
        />
        <ol className="mt-4">
          {STEPS.map((s) => (
            <li key={s.tag.en}>
              <Reveal y={20}>
                <div className="grid gap-2 py-8 sm:grid-cols-[11rem_1fr] sm:gap-10 sm:py-10">
                  <div className="font-display text-2xl leading-none text-gold sm:text-3xl">
                    {pick(lang, s.tag.zh, s.tag.en)}
                  </div>
                  <div>
                    <h3 className="font-display text-title leading-snug text-ink">
                      {pick(lang, s.title.zh, s.title.en)}
                    </h3>
                    <p className="mt-3 max-w-[34em] text-[15px] leading-relaxed text-ink2">
                      {pick(lang, s.body.zh, s.body.en)}
                    </p>
                  </div>
                </div>
              </Reveal>
            </li>
          ))}
        </ol>
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
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { once: true, amount: 0.3 });
  return (
    <div className="mx-auto max-w-6xl px-5 pb-24 sm:pb-32">
      <motion.div
        ref={ref}
        initial={false}
        className="mx-auto max-w-3xl overflow-hidden rounded-2xl border border-line bg-bg1"
      >
        <div className="hairline-b flex items-center justify-between px-4 py-2.5">
          <span className="font-mono text-[12px] text-sub">
            {pick(lang, "quickstart · 零模型也能跑", "quickstart · runs with zero models")}
          </span>
          <CopyButton text={QUICKSTART.join("\n")} />
        </div>
        <pre className="overflow-x-auto p-4 font-mono text-[12px] leading-7">
          {QUICKSTART.map((l, i) => (
            <motion.code
              key={l}
              className="block text-ink2"
              initial={{ opacity: 0 }}
              animate={inView ? { opacity: 1 } : { opacity: 0 }}
              transition={{ delay: i * 0.18, duration: 0.4 }}
            >
              <span className="mr-3 text-gold/70 select-none">$</span>
              {l}
            </motion.code>
          ))}
        </pre>
      </motion.div>
    </div>
  );
}
