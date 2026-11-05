import { Suspense, lazy, useEffect, useState } from "react";
import { MagneticButton } from "../components/motion/MagneticButton";
import { useLang } from "../lib/lang";
import { useMotionPreference } from "../lib/use-motion-preference";
import { HomeSearchDemo } from "./HomeSearchDemo";
import { HOME_COPY, HOME_DEMO_SCENES, type Scene } from "./home-demo-data";
import "./home-upgrade.css";

const ParticleField = lazy(() => import("./ParticleField"));

export function Hero() {
  const [scene, setScene] = useState<Scene>(0);
  const [ready, setReady] = useState(false);
  const [paused, setPaused] = useState(false);
  const lang = useLang();
  const reduced = useMotionPreference();
  const copy = HOME_COPY[lang];
  const zh = lang === "zh";

  // The heading, description and example are useful before WebGL is loaded.
  useEffect(() => {
    const w = window as Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (typeof w.requestIdleCallback === "function") {
      const id = w.requestIdleCallback(() => setReady(true), { timeout: 2200 });
      return () => w.cancelIdleCallback?.(id);
    }
    const timer = window.setTimeout(() => setReady(true), 900);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <header className="home-hero" data-home-hero="" data-home-motion={reduced ? "reduced" : paused ? "paused" : "playing"}>
      <div className="home-hero-stars" aria-hidden="true">
        {ready && <Suspense fallback={null}><ParticleField scene={scene} paused={paused} /></Suspense>}
      </div>
      <div className="home-hero-scrim" aria-hidden="true" />
      <div className="home-hero-body">
        <div className="home-hero-copy">
          <h1 className="home-hero-title font-display" data-home-title="">{copy.title}</h1>
          <p className="home-hero-description" data-home-description="">{copy.description}</p>
          <div className="home-hero-actions">
            <MagneticButton variant="gold" href="/docs#start" className="home-hero-start">
              {zh ? "开始使用" : "Get started"} <span aria-hidden="true">→</span>
            </MagneticButton>
            <MagneticButton variant="ghost" href={`/learn#c=${HOME_DEMO_SCENES[scene].lessonId}`}>
              {zh ? "看懂检索原理" : "How search works"} <span aria-hidden="true">↗</span>
            </MagneticButton>
          </div>
        </div>
        <div className="home-hero-preview">
          <HomeSearchDemo scene={scene} onScene={setScene} lang={lang} />
          <div className="home-hero-motion-row">
            <button className="home-hero-motion" type="button" data-home-pause="" aria-pressed={paused} disabled={reduced} onClick={() => setPaused((value) => !value)}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                {reduced ? <path d="M3 8h10" /> : paused ? <path d="m5 3 7 5-7 5Z" /> : <path d="M5 3v10M11 3v10" />}
              </svg>
              {reduced ? (zh ? "减少动态效果已开启" : "Reduced motion is on") : paused ? (zh ? "播放背景动画" : "Play background") : (zh ? "暂停背景动画" : "Pause background")}
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
