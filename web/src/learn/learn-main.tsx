import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "framer-motion";
import "@fontsource-variable/inter";
import "@fontsource/instrument-serif";
import "@fontsource/noto-serif-sc/400.css";
import "@fontsource-variable/jetbrains-mono";
import "../styles/global.css";
import { LangProvider, useLang } from "../lib/lang";
import { SmoothScrollProvider } from "../components/motion/SmoothScrollProvider";
import { loadLearnData } from "../lib/learn-data";
import { LearnApp } from "./LearnApp";

const data = loadLearnData();

function CorruptBanner() {
  const lang = useLang();
  const zh = lang === "zh";
  useEffect(() => {
    // corruption contract: reveal the baked glossary + drop the duplicated h1
    document.documentElement.setAttribute("data-corrupt", "");
    document.querySelector("h1[data-zh]")?.remove();
  }, []);
  return (
    <div className="mx-auto max-w-3xl px-5 pt-28 pb-10">
      <p className="kicker">LEARN · DEGRADED</p>
      <h1 className="mt-3 font-display text-3xl text-ink">
        {zh ? "地图数据损坏 — 已降级为目录视图" : "Map data invalid — downgraded to the list view"}
      </h1>
      <p className="mt-3 font-mono text-[12px] text-sub">
        {zh ? "数据校验未通过" : "data check failed"} ({data!.ok ? "" : data!.reason}) —{" "}
        {zh ? "完整目录在下方" : "the full glossary is below"} ·{" "}
        <a href="/" className="text-gold underline underline-offset-4">
          {zh ? "返回首页" : "home"}
        </a>
      </p>
    </div>
  );
}

function Boot() {
  if (!data.ok) return <CorruptBanner />;
  return <LearnApp data={data} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LangProvider>
      <SmoothScrollProvider>
        <MotionConfig reducedMotion="user">
          <Boot />
        </MotionConfig>
      </SmoothScrollProvider>
    </LangProvider>
  </StrictMode>,
);
