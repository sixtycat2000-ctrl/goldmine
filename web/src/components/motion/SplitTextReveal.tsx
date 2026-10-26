import { useMemo } from "react";
import { motion, useReducedMotion } from "framer-motion";

type Word = { chars: string[] };

/** Latin words stay whole; CJK text splits per character (no spaces to lean on). */
function splitWords(text: string): Word[] {
  const words: Word[] = [];
  let cur: string[] = [];
  for (const ch of text) {
    if (ch === " ") {
      if (cur.length) words.push({ chars: cur });
      cur = [];
      words.push({ chars: [" "] });
    } else if (/[\u2E80-\u9FFF\uF900-\uFAFF\uFF01-\uFF60]/.test(ch)) {
      if (cur.length) words.push({ chars: cur });
      cur = [];
      words.push({ chars: [ch] });
    } else {
      cur.push(ch);
    }
  }
  if (cur.length) words.push({ chars: cur });
  return words;
}

type Props = {
  text: string;
  className?: string;
  delay?: number;
  /** seconds between characters, capped so long lines don't drag */
  stagger?: number;
};

/**
 * Staggered per-character spring reveal (stiffness 120, damping 20 per spec).
 * Screen readers get the plain string via aria-label; glyphs are aria-hidden.
 * prefers-reduced-motion: plain text, no animation.
 */
export function SplitTextReveal({ text, className, delay = 0, stagger = 0.022 }: Props) {
  const reduced = useReducedMotion();
  const words = useMemo(() => splitWords(text), [text]);

  if (reduced) {
    return <span className={className}>{text}</span>;
  }

  let idx = 0;
  return (
    <span className={className} style={{ perspective: 600 }}>
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">
        {words.map((word, wi) => (
          <span key={wi} className="inline-block whitespace-pre">
            {word.chars.map((ch, ci) => {
              const d = delay + Math.min(idx++ * stagger, 0.6);
              return (
                <motion.span
                  key={ci}
                  className="inline-block"
                  initial={{ opacity: 0, y: "0.85em" }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: d, type: "spring", stiffness: 120, damping: 20 }}
                >
                  {ch}
                </motion.span>
              );
            })}
          </span>
        ))}
      </span>
    </span>
  );
}
