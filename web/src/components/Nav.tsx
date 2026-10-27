import { useState } from "react";
import { motion, useScroll, useMotionValueEvent, useReducedMotion } from "framer-motion";
import { useLangState, T } from "../lib/lang";
import { REPO_URL } from "../lib/metrics";
import { IconLogo, IconExt } from "./Icons";

export type Page = "home" | "learn" | "docs";

/**
 * Fixed shell nav: transparent until 24px of scroll, then glass. Top progress
 * bar is a MotionValue-driven transform write — zero React renders per frame.
 */
export function Nav({ page }: { page: Page }) {
  const { lang, setLang } = useLangState();
  const reduced = useReducedMotion();
  const [scrolled, setScrolled] = useState(false);
  const { scrollY, scrollYProgress } = useScroll();

  useMotionValueEvent(scrollY, "change", (y) => {
    setScrolled((s) => (y > 24 !== s ? !s : s));
  });

  const linkCls = (p: Page) =>
    `font-mono text-[12px] tracking-[0.1em] uppercase transition-colors ${
      page === p ? "text-gold" : "text-ink2 hover:text-ink"
    }`;

  return (
    <>
      <div className="fixed inset-x-0 top-0 z-[90] h-[2px] bg-transparent" aria-hidden="true">
        <motion.div
          className="h-full w-full origin-left bg-gradient-to-r from-gold to-goldbright will-change-transform"
          style={{ scaleX: reduced ? 1 : scrollYProgress }}
        />
      </div>
      <nav
        className={`fixed inset-x-0 top-0 z-[80] transition-colors duration-500 ${
          scrolled ? "glass" : "bg-transparent"
        }`}
      >
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-5">
          <a href="/" className="flex items-center gap-2 text-ink" aria-label={lang === "zh" ? "goldmine 首页" : "goldmine home"}>
            <IconLogo size={20} className="text-gold" />
            <b className="text-[15px] tracking-wide">goldmine</b>
          </a>
          <div className="flex items-center gap-3 sm:gap-6">
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
            <div className="flex overflow-hidden rounded-full border border-line" role="group" aria-label={lang === "zh" ? "语言" : "language"}>
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
