import { useEffect, useRef, useState } from "react";
import { motion, useInView } from "framer-motion";
import { usePrefersReducedMotion } from "../lib/hooks";
import { useFirstScrollIntent } from "../lib/useFirstScrollIntent";
import { useLang, pick } from "../lib/lang";
import { SectionHead } from "../components/SectionHead";
import { CopyButton } from "../components/CopyButton";

const STEPS = [
  {
    n: "01 · SCOPE",
    title: { zh: "你指定文件夹，只索引这些", en: "You choose folders; only those are indexed" },
    body: {
      zh: "没有全盘扫描。构建产物、密钥、iCloud 未下载占位一律跳过并留原因——索引永远不会触发一次 iCloud 下载。",
      en: "No whole-disk scan. Build outputs, secrets and undownloaded iCloud placeholders are skipped with reasons — indexing never triggers an iCloud download.",
    },
  },
  {
    n: "02 · EMBED",
    title: { zh: "内容变向量，每种媒体一条路", en: "Content becomes vectors; one path per modality" },
    body: {
      zh: "文本走钉版 embeddinggemma-2；装上可选边车后：图像/音频向量、视频约每 3 秒一帧（aHash 去重）、截图走 Vision OCR、录音走 whisper——命中直接跳到那一秒。",
      en: "Text uses the pinned embeddinggemma-2. With the optional sidecar: image/audio vectors, a video frame every ~3 s (aHash dedup), Vision OCR for screenshots, whisper for recordings — hits jump to the second.",
    },
  },
  {
    n: "03 · HYBRID SEARCH",
    title: { zh: "含义 + 精确词融合，算术可查", en: "Meaning + exact words fused, inspectably" },
    body: {
      zh: "score = 1.0·语义 z 分 + 2.0·词覆盖^1.5 + 类型加成；编号查询自动反转权重。--why 把每条命中的算术一项项摆出来。",
      en: "score = 1.0·semantic z + 2.0·coverage^1.5 + kind bonus; identifier queries auto-invert. --why prints every hit's arithmetic, line by line.",
    },
  },
  {
    n: "04 · ONE FILE",
    title: { zh: "一文件一事务，一库一文件", en: "One transaction per file, one file per index" },
    body: {
      zh: "每个文件的写入是单个 SQLite 事务——中断最坏丢这一个文件的这一次。备份、搬家、删除，就是复制那一个文件。",
      en: "Each file commits as one SQLite transaction — an interrupt loses at most that file's pass. Backup, moving, deletion: copy the one file.",
    },
  },
];

/** Pinned stacked-card scroll narrative (GSAP scrub + CSS sticky). */
export function Narrative() {
  const reduced = usePrefersReducedMotion();
  const scrollIntent = useFirstScrollIntent();
  const lang = useLang();
  const containerRef = useRef<HTMLDivElement>(null);
  const cardsRef = useRef<(HTMLDivElement | null)[]>([]);
  const [active, setActive] = useState(0);

  useEffect(() => {
    if (reduced || !scrollIntent) return;
    let ctx: { revert: () => void } | null = null;
    let cancelled = false;

    (async () => {
      const [{ gsap }, { ScrollTrigger }] = await Promise.all([import("gsap"), import("gsap/ScrollTrigger")]);
      if (cancelled) return;
      gsap.registerPlugin(ScrollTrigger);
      const cards = cardsRef.current;
      if (!cards.length) return;

      // pacing: each card holds a full unit of scroll; swaps take 0.5 units.
      const HOLD = 1;
      const SWAP = 0.5;
      const DUR = STEPS.length * HOLD + (STEPS.length - 1) * SWAP;

      ctx = gsap.context(() => {
        // GSAP parses computed matrices — the CSS translateY(45%) would become
        // px `y`, leaving yPercent at 0 and the rise a no-op. Set it in GSAP.
        cards.forEach((c, i) => i > 0 && gsap.set(c, { yPercent: 45 }));
        const tl = gsap.timeline({
          scrollTrigger: {
            trigger: containerRef.current,
            start: "top top",
            end: "bottom bottom",
            scrub: true, // Lenis already smooths; a second lag layer floats
            onUpdate(self) {
              const t = self.progress * DUR;
              const idx = Math.min(
                STEPS.length - 1,
                Math.max(0, Math.floor((t + SWAP / 2) / (HOLD + SWAP) + 1e-4)),
              );
              setActive((prevA) => (prevA === idx ? prevA : idx));
            },
          },
        });
        for (let i = 1; i < cards.length; i++) {
          const at = HOLD + (i - 1) * (HOLD + SWAP);
          tl.to(cards[i - 1], { yPercent: -14, scale: 0.95, opacity: 0.22, duration: SWAP, ease: "power2.in" }, at).to(
            cards[i],
            { yPercent: 0, opacity: 1, duration: SWAP, ease: "power2.out", immediateRender: false },
            at,
          );
        }
      }, containerRef);
    })();

    return () => {
      cancelled = true;
      ctx?.revert();
    };
  }, [reduced, scrollIntent]);

  return (
    <section id="how" className="relative">
      <div className="mx-auto max-w-6xl px-5 pt-24 sm:pt-32">
        <SectionHead
          no="02"
          kicker={{ zh: "工作原理", en: "HOW IT WORKS" }}
          title={{ zh: "从磁盘到可搜，四步", en: "Disk to searchable, four steps" }}
          lede={{
            zh: "跟着滚动走完这条流水线——每一步都停在真实机制上，没有魔法。",
            en: "Scroll through the pipeline — every step lands on a real mechanism, no magic.",
          }}
        />
      </div>

      <div
        ref={containerRef}
        className="relative"
        style={reduced ? undefined : { height: `${STEPS.length * 150}svh` }}
      >
        <div className={reduced ? "" : "sticky top-0 flex h-svh items-center overflow-hidden"}>
          {/* step rail */}
          <div className="pointer-events-none absolute top-1/2 left-4 hidden -translate-y-1/2 flex-col gap-3 lg:flex" aria-hidden="true">
            {STEPS.map((s, i) => (
              <span
                key={s.n}
                className={`h-8 w-px transition-colors duration-500 ${i <= active ? "bg-gold" : "bg-line-strong"}`}
              />
            ))}
          </div>
          <div className="relative mx-auto h-[26rem] w-full max-w-3xl px-5 sm:h-[24rem]">
            {STEPS.map((s, i) => (
              <div
                key={s.n}
                ref={(el) => {
                  cardsRef.current[i] = el;
                }}
                className={`glass rounded-3xl p-8 sm:p-10 ${
                  reduced ? "relative mb-4" : "absolute inset-x-5 inset-y-0"
                } ${!reduced && i > 0 ? "opacity-0" : ""}`}
              >
                <p className="kicker">{s.n}</p>
                <h3 className="mt-4 font-display text-title leading-snug text-ink sm:text-3xl">
                  {pick(lang, s.title.zh, s.title.en)}
                </h3>
                <p className="mt-4 max-w-[36em] text-[15px] leading-relaxed text-ink2">
                  {pick(lang, s.body.zh, s.body.en)}
                </p>
              </div>
            ))}
          </div>
        </div>
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
          <span className="font-mono text-[11px] tracking-[0.1em] text-sub">
            {pick(lang, "quickstart — 零模型也能跑", "quickstart — runs with zero models")}
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
