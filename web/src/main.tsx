import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "framer-motion";
import "@fontsource-variable/inter";
import "@fontsource/instrument-serif";
import "@fontsource-variable/jetbrains-mono";
import "./styles/global.css";
import { LangProvider } from "./lib/lang";
import { SmoothScrollProvider } from "./components/motion/SmoothScrollProvider";
import { HomeApp } from "./home/HomeApp";
import { loadNotoSerifSc } from "./lib/notoSerifSc";

// zh display serif loads out-of-band: its @font-face CSS is ~120KB and must
// stay out of the render-blocking sheet (Songti/serif covers the interim).
loadNotoSerifSc();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <LangProvider>
        <SmoothScrollProvider>
          <HomeApp />
        </SmoothScrollProvider>
      </LangProvider>
    </MotionConfig>
  </StrictMode>,
);
