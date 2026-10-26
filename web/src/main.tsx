import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "framer-motion";
import "@fontsource-variable/inter";
import "@fontsource/instrument-serif";
import "@fontsource/noto-serif-sc/400.css";
import "@fontsource-variable/jetbrains-mono";
import "./styles/global.css";
import { LangProvider } from "./lib/lang";
import { SmoothScrollProvider } from "./components/motion/SmoothScrollProvider";
import { HomeApp } from "./home/HomeApp";

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
