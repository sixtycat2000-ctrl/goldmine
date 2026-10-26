import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "framer-motion";
import "@fontsource-variable/inter";
import "@fontsource/instrument-serif";
import "@fontsource-variable/jetbrains-mono";
import "../styles/global.css";
import { LangProvider } from "../lib/lang";
import { SmoothScrollProvider } from "../components/motion/SmoothScrollProvider";
import { loadLearnData } from "../lib/learn-data";
import { LearnApp } from "./LearnApp";

const data = loadLearnData();

function Boot() {
  if (!data.ok) {
    // Corruption contract (same semantics as map.js v3): reveal the baked
    // no-JS glossary and say so honestly.
    document.documentElement.setAttribute("data-corrupt", "");
    return (
      <div className="mx-auto max-w-3xl px-5 pt-28 pb-10">
        <p className="kicker">LEARN · DEGRADED</p>
        <h1 className="mt-3 font-display text-3xl text-ink">
          地图数据损坏 — 已降级为目录视图
        </h1>
        <p className="mt-3 font-mono text-[12px] text-sub">
          map data invalid ({data.reason}) — list view below · <a href="/" className="text-gold underline underline-offset-4">home</a>
        </p>
      </div>
    );
  }
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
