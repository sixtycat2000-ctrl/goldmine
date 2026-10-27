import { Suspense, lazy, useEffect, useState } from "react";
import { motion, useScroll, useTransform, useReducedMotion } from "framer-motion";
import { SplitTextReveal } from "../components/motion/SplitTextReveal";
import { Reveal } from "../components/motion/Reveal";
import { MagneticButton } from "../components/motion/MagneticButton";
import { Counter } from "../components/motion/Counter";
import { SceneTabs, type Scene } from "./SceneTabs";
import { useLang } from "../lib/lang";
import { SUITE, REPO_URL } from "../lib/metrics";

const ParticleField = lazy(() => import("./ParticleField"));

export function Hero() {
  const [scene, setScene] = useState<Scene>(0);
  const [ready, setReady] = useState(false);
  const lang = useLang();
  const reduced = useReducedMotion();

  // exit choreography: content drifts up slowly, fades faster — depth without
  // fighting the particle camera. Post-mount only, so the LCP is untouched.
  const { scrollY } = useScroll();
  const heroY = useTransform(scrollY, [0, 900], [0, -140], { clamp: true });
  const heroFade = useTransform(scrollY, [0, 520], [1, 0], { clamp: true });

  // Defer the WebGL chunk until after first paint + idle: LCP is text.
  // (Safari < 17 has no requestIdleCallback — fall back to a timeout.)
  useEffect(() => {
    const w = window as Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (typeof w.requestIdleCallback === "function") {
      const id = w.requestIdleCallback(() => setReady(true), { timeout: 2200 });
      return () => w.cancelIdleCallback?.(id);
    }
    const t = window.setTimeout(() => setReady(true), 900);
    return () => window.clearTimeout(t);
  }, []);

  const zh = lang === "zh";

  return (
    <header className="relative flex min-h-[100svh] flex-col overflow-hidden">
      {ready && (
        <Suspense fallback={null}>
          <ParticleField scene={scene} />
        </Suspense>
      )}
      {/* scrims: light left wash for the copy column (the field is re-graded
          dim at the source), bottom melt into bg0 for the stats strip */}
      <div
        className="pointer-events-none absolute inset-0"
        aria-hidden="true"
        style={{
          background:
            "linear-gradient(90deg, rgba(8,8,10,0.62) 0%, rgba(8,8,10,0.30) 36%, rgba(8,8,10,0.08) 56%, rgba(8,8,10,0) 70%), linear-gradient(to bottom, rgba(8,8,10,0) 55%, rgba(8,8,10,0.4) 78%, #08080A 97%)",
        }}
      />

      <motion.div
        className="relative z-10 mx-auto flex w-full max-w-6xl flex-1 flex-col justify-center px-5 pt-28 pb-16 will-change-transform"
        style={reduced ? undefined : { y: heroY, opacity: heroFade }}
      >
        <Reveal y={12}>
          <p className="kicker">OPEN SOURCE · MACOS · LOCAL-FIRST</p>
        </Reveal>
        <h1 className="mt-5 max-w-[13em] font-display text-hero leading-[1.04] text-ink">
          <SplitTextReveal
            text={zh ? "按意思搜索，文件不出这台 Mac。" : "Search by meaning. Files never leave your Mac."}
            delay={0.1}
            stagger={0.016}
          />
        </h1>
        {/* the dek renders statically (identical to the pre-mount shell): it is
            the page's largest text node, and animating it would push the LCP
            to animation end. The h1 keeps the spring choreography. */}
        <p
          className="mt-6 max-w-[44em] text-[15px] leading-relaxed text-ink2 sm:text-base"
          style={{ textShadow: "0 1px 2px rgba(8,8,10,0.85), 0 0 24px rgba(8,8,10,0.6)" }}
        >
          {zh
            ? "你记得内容，不记得文件名——会议录音、截图、两百页 PDF 都一样。goldmine 把你指定的文件夹索引成一个本地 SQLite 文件：文字按语义搜、编号按精确搜、录音命中到秒、截图按内容搜。上传？这条代码路径不存在。"
            : "You remember the content, not the filename — meeting recordings, screenshots, 200-page PDFs, all the same. goldmine indexes the folders you choose into one local SQLite file: text by meaning, identifiers verbatim, audio to the second, screenshots by content. Uploads? That code path does not exist."}
        </p>
        <Reveal delay={0.45} className="mt-8">
          <div className="flex flex-wrap items-center gap-2">
            <MagneticButton variant="gold" href="/docs#start">
              {zh ? "快速开始" : "Quickstart"} →
            </MagneticButton>
            <MagneticButton variant="ghost" href={REPO_URL} external>
              GitHub
            </MagneticButton>
          </div>
        </Reveal>
        <Reveal delay={0.55}>
          <div style={{ textShadow: "0 1px 2px rgba(8,8,10,0.85), 0 0 24px rgba(8,8,10,0.6)" }}>
            <SceneTabs scene={scene} onScene={setScene} />
          </div>
        </Reveal>
      </motion.div>

      <div className="relative z-10 hairline-t">
        <div className="mx-auto grid max-w-6xl grid-cols-2 gap-y-6 px-5 py-6 md:grid-cols-4">
          <div>
            <div className="font-display text-3xl text-gold">
              <Counter end={3200} delay={500} duration={1100} />+
            </div>
            <div className="mt-1 font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
              {zh ? "离线测试（全量 8 分钟）" : "OFFLINE TESTS (8-MIN SUITE)"}
            </div>
          </div>
          <div>
            <div className="font-display text-3xl text-ink">
              <Counter end={420} />
            </div>
            <div className="mt-1 font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
              {zh ? `冻结评测题 · ${SUITE.frozenSources} 来源` : `FROZEN EVAL QS · ${SUITE.frozenSources} SOURCES`}
            </div>
          </div>
          <div>
            <div className="font-display text-3xl text-ink">1</div>
            <div className="mt-1 font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
              {zh ? "个 SQLite 文件 = 全部索引" : "SQLITE FILE = WHOLE INDEX"}
            </div>
          </div>
          <div>
            <div className="font-display text-3xl text-gold">
              0<i className="font-sans text-base"> B</i>
            </div>
            <div className="mt-1 font-mono text-[10px] tracking-[0.14em] text-sub uppercase">
              {zh ? "上传（代码里没有这条路径）" : "UPLOADED (no such code path)"}
            </div>
          </div>
        </div>
      </div>
    </header>
  );
}
