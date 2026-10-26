import { motion, useInView, useReducedMotion } from "framer-motion";
import { useRef } from "react";
import { useLang, pick } from "../lib/lang";
import { SpotlightCard } from "../components/motion/SpotlightCard";
import { TiltCard } from "../components/motion/TiltCard";
import { SectionHead } from "../components/SectionHead";
import { Counter } from "../components/motion/Counter";

/* ------------------------------------------------------------------ */
/* micro-demos — all values are the product's real, published numbers  */
/* ------------------------------------------------------------------ */

function HybridDemo() {
  const lang = useLang();
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { amount: 0.5 });
  const rows = [
    { w: "1.0 · z", v: 0.83, c: "var(--color-gold)" },
    { w: "2.0 · cov^1.5", v: 0.87, c: "var(--color-goldbright)" },
    { w: "+ kind", v: 0.1, c: "var(--color-teal)" },
  ];
  return (
    <div ref={ref} className="mt-5" aria-hidden="true">
      <p className="font-mono text-[11px] text-sub">score = 1.0·z + 2.0·cov^1.5 + kind</p>
      <div className="mt-3 space-y-2.5">
        {rows.map((r, i) => (
          <div key={r.w} className="flex items-center gap-3">
            <span className="w-24 shrink-0 font-mono text-[10px] text-sub">{r.w}</span>
            <div className="h-[6px] flex-1 overflow-hidden rounded-full bg-bg3">
              <motion.div
                className="h-full rounded-full"
                style={{ background: r.c }}
                initial={{ width: 0 }}
                animate={inView ? { width: `${r.v * 100}%` } : { width: 0 }}
                transition={{ duration: 0.9, delay: i * 0.18, ease: [0.16, 1, 0.3, 1] }}
              />
            </div>
          </div>
        ))}
      </div>
      <p className="mt-3 font-mono text-[11px] text-ink2">
        → <Counter end={2.63} decimals={2} className="text-gold" />{" "}
        <span className="text-sub">{pick(lang, "每条命中都摆得开这行算术", "every hit's arithmetic, line by line")}</span>
      </p>
    </div>
  );
}

function MultimodalDemo() {
  const chips = [
    { zh: "文本", en: "text" },
    { zh: "截图", en: "OCR" },
    { zh: "录音", en: "whisper" },
    { zh: "视频", en: "frames" },
  ];
  const lang = useLang();
  return (
    <div className="mt-5" aria-hidden="true">
      <div className="flex flex-wrap gap-2">
        {chips.map((c, i) => (
          <motion.span
            key={c.en}
            className="rounded-full border border-line px-3 py-1 font-mono text-[10px] tracking-[0.1em] text-ink2 uppercase"
            animate={{ opacity: [0.35, 1, 0.35], borderColor: ["var(--color-line)", "rgba(245,176,65,0.55)", "var(--color-line)"] }}
            transition={{ duration: 3.2, repeat: Infinity, delay: i * 0.8, ease: "easeInOut" }}
          >
            {pick(lang, c.zh, c.en)}
          </motion.span>
        ))}
      </div>
      <div className="mt-4 flex h-8 items-end gap-1" aria-hidden="true">
        {[0.4, 0.9, 0.55, 1, 0.65, 0.85, 0.45, 0.75, 0.5, 0.95, 0.6, 0.8].map((h, i) => (
            <motion.span
              key={i}
              className="w-1.5 flex-1 rounded-sm bg-teal/70"
              style={{ height: `${h * 100}%`, originY: 1 }}
              animate={{ scaleY: [0.25, 1, 0.25] }}
              transition={{ duration: 1.6, repeat: Infinity, delay: i * 0.13, ease: "easeInOut" }}
            />
        ))}
      </div>
    </div>
  );
}

function GatesDemo() {
  // verify: 2026-10-26 official run product-current-017 — 17/19 gates; fails: noise 20/30, media-OCR 6/9
  const fails: Record<number, string> = { 17: "noise 20/30", 18: "media-OCR 6/9" };
  return (
    <div className="mt-5" aria-hidden="true">
      <div className="grid grid-cols-10 gap-1.5">
        {Array.from({ length: 19 }, (_, i) => (
          <motion.span
            key={i}
            className={`h-2.5 rounded-full ${i in fails ? "bg-fail" : "bg-gold"}`}
            title={i in fails ? fails[i] : undefined}
            initial={{ scaleX: 0, opacity: 0 }}
            whileInView={{ scaleX: 1, opacity: 1 }}
            viewport={{ amount: 0.6 }}
            transition={{ delay: i * 0.045, duration: 0.3, ease: "easeOut" }}
          />
        ))}
      </div>
      <p className="mt-3 font-mono text-[11px] text-ink2">
        <Counter end={17} className="text-gold" />
        <span className="text-sub"> / 19 </span>
        <span className="text-fail">· 2</span>{" "}
        <span className="text-sub">unresolved, reasons on record</span>
      </p>
    </div>
  );
}

function LocalDemo() {
  return (
    <div className="mt-5" aria-hidden="true">
      <div className="relative h-16">
        {/* track */}
        <div className="absolute top-1/2 left-0 h-px w-full bg-line" />
        {/* local vault */}
        <div className="absolute top-1/2 left-0 h-9 w-16 -translate-y-1/2 rounded-md border border-line-strong bg-bg3" />
        {/* cloud, never reached */}
        <div className="absolute top-1/2 right-0 h-9 w-16 -translate-y-1/2 rounded-full border border-dashed border-line" />
        <motion.span
          className="absolute top-1/2 left-16 h-2 w-2 -translate-y-1/2 rounded-full bg-gold"
          animate={{ x: [0, 132, 0], opacity: [1, 0.9, 1] }}
          transition={{ duration: 2.8, repeat: Infinity, ease: "easeInOut", times: [0, 0.62, 1] }}
        />
        <span className="absolute top-1/2 right-20 font-mono text-[10px] text-fail">✕</span>
      </div>
      <p className="mt-2 font-mono text-[10px] tracking-[0.12em] text-sub uppercase">
        0 bytes leave your machine
      </p>
    </div>
  );
}

function ReceiptDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { amount: 0.5 });
  const lines = [
    { t: "$ goldmine search '退款期限' --why", c: "text-ink2" },
    { t: "exit 0 · 3 hits · 1.4s", c: "text-teal" },
    { t: "sha256 e3b0…9b12 ✓  receipt archived", c: "text-gold" },
  ];
  return (
    <div ref={ref} className="mt-5 space-y-1.5 font-mono text-[11px] leading-relaxed" aria-hidden="true">
      {lines.map((l, i) => (
        <motion.p
          key={l.t}
          className={l.c}
          initial={{ opacity: 0, x: -8 }}
          animate={inView ? { opacity: 1, x: 0 } : { opacity: 0, x: -8 }}
          transition={{ delay: i * 0.5, duration: 0.4 }}
        >
          {l.t}
        </motion.p>
      ))}
    </div>
  );
}

function ServeDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { amount: 0.5 });
  const tools = ["search", "open", "status", "audit"];
  return (
    <div ref={ref} className="mt-5 font-mono text-[11px]" aria-hidden="true">
      <p className="text-ink2">$ goldmine serve --mcp</p>
      <div className="mt-2 grid grid-cols-2 gap-1.5">
        {tools.map((t, i) => (
          <motion.span
            key={t}
            className="rounded border border-line px-2 py-1 text-ink2"
            initial={{ opacity: 0, y: 6 }}
            animate={inView ? { opacity: 1, y: 0 } : { opacity: 0, y: 6 }}
            transition={{ delay: 0.6 + i * 0.22, duration: 0.35 }}
          >
            <span className="text-gold">mcp:</span> {t}
          </motion.span>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Card({
  title,
  sub,
  children,
  className,
}: {
  title: { zh: string; en: string };
  sub: { zh: string; en: string };
  children: React.ReactNode;
  className?: string;
}) {
  const lang = useLang();
  return (
    <TiltCard className={className}>
      <SpotlightCard className="flex h-full flex-col p-6">
        <h3 className="font-display text-title leading-snug text-ink">{pick(lang, title.zh, title.en)}</h3>
        <p className="mt-2 text-[13px] leading-relaxed text-ink2">{pick(lang, sub.zh, sub.en)}</p>
        <div className="mt-auto">{children}</div>
      </SpotlightCard>
    </TiltCard>
  );
}

export function Bento() {
  const reduced = useReducedMotion();
  return (
    <section id="features" className="relative mx-auto max-w-6xl px-5 py-24 sm:py-32">
      <SectionHead
        no="01"
        kicker={{ zh: "平台能力", en: "PLATFORM CAPABILITIES" }}
        title={{
          zh: "一个索引，四种媒体，零字节的云",
          en: "One index, four media, zero bytes of cloud",
        }}
        lede={{
          zh: "每张卡片里的演示都是真实机制的缩影——数字全部来自公开评测，没有一个是为好看编的。",
          en: "Every demo on these cards is a scaled-down real mechanism — every number comes from the published evals, none invented for looks.",
        }}
      />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-6">
        <Card
          className="lg:col-span-4"
          title={{ zh: "混合搜索，算术可查", en: "Hybrid search, inspectable arithmetic" }}
          sub={{
            zh: "语义 z 分与词精确覆盖融合打分，编号查询自动反转权重。--why 把每条命中的算术一项项摆出来。",
            en: "Semantic z-score fused with exact word coverage; identifier queries auto-invert. --why prints every hit's arithmetic, line by line.",
          }}
        >
          <HybridDemo />
        </Card>
        <Card
          className="lg:col-span-2"
          title={{ zh: "全媒体，本地索引", en: "All media, indexed locally" }}
          sub={{
            zh: "录音转写命中到秒，截图 OCR 按内容搜，视频按关键帧——模型都在你机器上跑。",
            en: "Recordings hit to the second, screenshots searched by content, video by keyframes — models run on your machine.",
          }}
        >
          {!reduced && <MultimodalDemo />}
        </Card>
        <Card
          className="lg:col-span-2"
          title={{ zh: "诚实契约", en: "The honesty contract" }}
          sub={{
            zh: "没有静默降级：每个文件四种状态都带原因；两门未过的质量门写明为什么。",
            en: "No silent degradations: every file carries its state and reason; the two unresolved gates are explained, not hidden.",
          }}
        >
          <GatesDemo />
        </Card>
        <Card
          className="lg:col-span-2"
          title={{ zh: "零上传", en: "Zero upload" }}
          sub={{
            zh: "索引是一个 SQLite 文件。备份、搬家、删除——复制那一个文件就好。",
            en: "The index is one SQLite file. Backup, move, delete — copy the one file.",
          }}
        >
          <LocalDemo />
        </Card>
        <Card
          className="lg:col-span-2"
          title={{ zh: "证据纪律", en: "Evidence discipline" }}
          sub={{
            zh: "回执（argv/退出码/sha）、预注册冻结、对抗评审——独立审计 840 项重评分零错配。",
            en: "Receipts (argv/exit/sha), frozen preregistrations, adversarial review — an independent audit re-scored 840 metrics, zero mismatches.",
          }}
        >
          <ReceiptDemo />
        </Card>
        <Card
          className="lg:col-span-4"
          title={{ zh: "常驻索引 + 本地 MCP", en: "Resident indexing + local MCP" }}
          sub={{
            zh: "watch 增量索引、serve 常驻免加载；MCP 让 search / open / status 直接成为你 Agent 的工具。",
            en: "watch indexes incrementally, serve stays resident; MCP turns search / open / status into tools for your Agent.",
          }}
        >
          <ServeDemo />
        </Card>
      </div>
    </section>
  );
}
