import { Suspense, lazy, useEffect, useState } from "react";
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

  // Defer the WebGL chunk until after first paint + idle: LCP is text.
  useEffect(() => {
    const id = requestIdleCallback(() => setReady(true), { timeout: 1500 });
    return () => cancelIdleCallback(id);
  }, []);

  const zh = lang === "zh";

  return (
    <header className="relative flex min-h-[100svh] flex-col overflow-hidden">
      {ready && (
        <Suspense fallback={null}>
          <ParticleField scene={scene} />
        </Suspense>
      )}
      {/* scrims: keep text legible over the field, melt into bg0 below */}
      <div
        className="pointer-events-none absolute inset-0"
        aria-hidden="true"
        style={{
          background:
            "linear-gradient(90deg, rgba(8,8,10,0.82) 0%, rgba(8,8,10,0.55) 38%, rgba(8,8,10,0.08) 68%, rgba(8,8,10,0) 100%), radial-gradient(70% 55% at 50% 42%, rgba(8,8,10,0) 30%, rgba(8,8,10,0.55) 100%), linear-gradient(to bottom, rgba(8,8,10,0.25), rgba(8,8,10,0) 22%, rgba(8,8,10,0) 62%, #08080A 96%)",
        }}
      />

      <div className="relative z-10 mx-auto flex w-full max-w-6xl flex-1 flex-col justify-center px-5 pt-28 pb-16">
        <Reveal y={12}>
          <p className="kicker">OPEN SOURCE · MACOS · LOCAL-FIRST</p>
        </Reveal>
        <h1 className="mt-5 max-w-[13em] font-display text-hero leading-[1.04] text-ink">
          <SplitTextReveal
            text={zh ? "按意思搜索，文件不出这台 Mac。" : "Search by meaning. Files never leave your Mac."}
            delay={0.25}
          />
        </h1>
        <Reveal delay={0.9} className="mt-6 max-w-[44em]">
          <p className="text-[15px] leading-relaxed text-ink2 sm:text-base">
            {zh
              ? "你记得内容，不记得文件名——会议录音、截图、两百页 PDF 都一样。goldmine 把你指定的文件夹索引成一个本地 SQLite 文件：文字按语义搜、编号按精确搜、录音命中到秒、截图按内容搜。上传？这条代码路径不存在。"
              : "You remember the content, not the filename — meeting recordings, screenshots, 200-page PDFs, all the same. goldmine indexes the folders you choose into one local SQLite file: text by meaning, identifiers verbatim, audio to the second, screenshots by content. Uploads? That code path does not exist."}
          </p>
        </Reveal>
        <Reveal delay={1.05} className="mt-8">
          <div className="flex flex-wrap items-center gap-2">
            <MagneticButton variant="gold" href="/docs#start">
              {zh ? "快速开始" : "Quickstart"} →
            </MagneticButton>
            <MagneticButton variant="ghost" href={REPO_URL} external>
              GitHub
            </MagneticButton>
          </div>
        </Reveal>
        <Reveal delay={1.2}>
          <SceneTabs scene={scene} onScene={setScene} />
        </Reveal>
      </div>

      <div className="relative z-10 hairline-t">
        <div className="mx-auto grid max-w-6xl grid-cols-2 gap-y-6 px-5 py-6 md:grid-cols-4">
          <div>
            <div className="font-display text-3xl text-gold">
              <Counter end={3200} />+
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
