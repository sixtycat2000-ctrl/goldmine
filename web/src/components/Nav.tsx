import { useEffect, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { useLangState, T } from "../lib/lang";
import { REPO_URL } from "../lib/metrics";
import { IconLogo, IconExt } from "./Icons";

export type Page = "home" | "learn" | "docs";

/**
 * Fixed shell nav: transparent until 24px of scroll, then glass. Top progress
 * bar tracks document scroll (transform-only). Same routes as v1–v3.
 */
export function Nav({ page }: { page: Page }) {
  const { lang, setLang } = useLangState();
  const reduced = useReducedMotion();
  const [scrolled, setScrolled] = useState(false);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    let raf = 0;
    const onScroll = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const y = window.scrollY;
        setScrolled(y > 24);
        const max = document.documentElement.scrollHeight - window.innerHeight;
        setProgress(max > 0 ? Math.min(1, y / max) : 0);
      });
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", onScroll);
    };
  }, []);

  const linkCls = (p: Page) =>
    `font-mono text-[12px] tracking-[0.1em] uppercase transition-colors ${
      page === p ? "text-gold" : "text-ink2 hover:text-ink"
    }`;

  return (
    <>
      <div className="fixed inset-x-0 top-0 z-[90] h-[2px] bg-transparent" aria-hidden="true">
        <div
          className="h-full origin-left bg-gradient-to-r from-gold to-goldbright"
          style={{ transform: `scaleX(${reduced ? 1 : progress})`, width: "100%" }}
        />
      </div>
      <nav
        className={`fixed inset-x-0 top-0 z-[80] transition-colors duration-500 ${
          scrolled ? "glass" : "bg-transparent"
        }`}
      >
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-5">
          <a href="/" className="flex items-center gap-2 text-ink" aria-label="goldmine home">
            <IconLogo size={20} className="text-gold" />
            <b className="text-[15px] tracking-wide">goldmine</b>
          </a>
          <div className="flex items-center gap-4 sm:gap-6">
            <a href="/" className={linkCls("home")} aria-current={page === "home" ? "page" : undefined}>
              <T zh="首页" en="Home" />
            </a>
            <a href="/docs" className={linkCls("docs")} aria-current={page === "docs" ? "page" : undefined}>
              <T zh="文档" en="Docs" />
            </a>
            <a href="/learn" className={linkCls("learn")} aria-current={page === "learn" ? "page" : undefined}>
              Learn
            </a>
            <a
              href={REPO_URL}
              className="hidden items-center gap-1 font-mono text-[12px] uppercase tracking-[0.1em] text-ink2 transition-colors hover:text-ink sm:flex"
            >
              GitHub <IconExt size={11} />
            </a>
            <div className="flex overflow-hidden rounded-full border border-line" role="group" aria-label="language">
              {(["zh", "en"] as const).map((l) => (
                <button
                  key={l}
                  type="button"
                  onClick={() => setLang(l)}
                  aria-pressed={lang === l}
                  className={`relative px-3 py-1 font-mono text-[12px] transition-colors ${
                    lang === l ? "text-bg0" : "text-ink2 hover:text-ink"
                  }`}
                >
                  {lang === l && (
                    <motion.span
                      layoutId="lang-pill"
                      className="absolute inset-0 bg-gold"
                      transition={{ type: "spring", stiffness: 400, damping: 32 }}
                    />
                  )}
                  <span className="relative z-10">{l === "zh" ? "中" : "EN"}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </nav>
    </>
  );
}
