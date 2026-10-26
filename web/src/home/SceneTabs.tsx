import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useReducedMotion } from "framer-motion";
import { useLang, pick } from "../lib/lang";

export type Scene = 0 | 1 | 2;

const SCENES: {
  id: string;
  label: { zh: string; en: string };
  query: { zh: string; en: string };
  mech: { zh: string; en: string };
}[] = [
  {
    id: "semantic",
    label: { zh: "语义", en: "Semantic" },
    query: { zh: "退款期限还有几天？", en: "how long is the refund window?" },
    mech: {
      zh: "按意思唤起最近的邻居——同义、改写都算",
      en: "nearest neighbors by meaning — paraphrases count",
    },
  },
  {
    id: "lexical",
    label: { zh: "词法", en: "Lexical" },
    query: { zh: "ZT-84", en: "ZT-84" },
    mech: {
      zh: "编号查询反转权重：精确词一击即中（青色点）",
      en: "identifier queries invert weights: exact token, one shot (teal)",
    },
  },
  {
    id: "multimodal",
    label: { zh: "多模态", en: "Multimodal" },
    query: { zh: "截图里的那段报错", en: "that error in a screenshot" },
    mech: {
      zh: "文本、OCR、音频、图像——四路证据合流",
      en: "text, OCR, audio, image — four evidence paths converge",
    },
  },
];

function useTypewriter(text: string, speed = 55): string {
  const reduced = useReducedMotion();
  const [n, setN] = useState(reduced ? text.length : 0);
  useEffect(() => {
    if (reduced) {
      setN(text.length);
      return;
    }
    setN(0);
    const t0 = performance.now();
    const id = window.setInterval(() => {
      const k = Math.floor((performance.now() - t0) / speed);
      setN(Math.min(text.length, k));
      if (k >= text.length) window.clearInterval(id);
    }, speed);
    return () => window.clearInterval(id);
  }, [text, speed, reduced]);
  return text.slice(0, n);
}

/**
 * Scene switcher for the hero canvas demo. Buttons (not ARIA tabs — the
 * "panel" is a decorative canvas), sliding gold pill via shared layoutId.
 */
export function SceneTabs({ scene, onScene }: { scene: Scene; onScene: (s: Scene) => void }) {
  const lang = useLang();
  const active = SCENES[scene];
  const query = useTypewriter(pick(lang, active.query.zh, active.query.en));

  return (
    <div className="mt-10">
      <div
        className="inline-flex flex-wrap items-center gap-1 rounded-full border border-line bg-bg1/60 p-1 backdrop-blur-sm"
        role="group"
        aria-label="search modes"
      >
        {SCENES.map((s, i) => (
          <button
            key={s.id}
            type="button"
            onClick={() => onScene(i as Scene)}
            aria-pressed={scene === i}
            className={`relative rounded-full px-4 py-1.5 font-mono text-[12px] tracking-[0.08em] uppercase transition-colors ${
              scene === i ? "text-bg0" : "text-ink2 hover:text-ink"
            }`}
          >
            {scene === i && (
              <motion.span
                layoutId="scene-pill"
                className="absolute inset-0 rounded-full bg-gold"
                transition={{ type: "spring", stiffness: 380, damping: 30 }}
              />
            )}
            <span className="relative z-10">{pick(lang, s.label.zh, s.label.en)}</span>
          </button>
        ))}
      </div>
      <p className="mt-3 font-mono text-[12px] text-ink2" aria-hidden="true">
        <span className="text-gold">›</span> {query}
        <span className="ml-2 text-sub">
          <span className="text-teal">→</span> {pick(lang, active.mech.zh, active.mech.en)}
        </span>
      </p>
      {/* static, per-scene announcement — the typewriter must not churn live regions */}
      <p className="sr-only" aria-live="polite">
        {`${pick(lang, active.label.zh, active.label.en)}: ${pick(lang, active.query.zh, active.query.en)} — ${pick(lang, active.mech.zh, active.mech.en)}`}
      </p>
      <p className="sr-only">{pick(lang, "背景粒子场为机制示意演示", "the background particle field is a stylized mechanism demo")}</p>
    </div>
  );
}
