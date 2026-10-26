import { useEffect, useRef, useState } from "react";
import { useInView, useReducedMotion } from "framer-motion";

type Props = {
  end: number;
  decimals?: number;
  duration?: number; // ms
  prefix?: string;
  suffix?: string;
  className?: string;
  locale?: boolean;
};

const easeOutExpo = (t: number) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t));

/** Count-up numeral that runs once when scrolled into view. RM: static value. */
export function Counter({ end, decimals = 0, duration = 1400, prefix = "", suffix = "", className, locale = true }: Props) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, amount: 0.6 });
  const reduced = useReducedMotion();
  const [val, setVal] = useState(0);

  useEffect(() => {
    if (!inView) return;
    if (reduced) {
      setVal(end);
      return;
    }
    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - t0) / duration);
      setVal(end * easeOutExpo(p));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, reduced, end, duration]);

  const text = locale
    ? val.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
    : val.toFixed(decimals);

  return (
    <span ref={ref} className={className}>
      {prefix}
      {text}
      {suffix}
    </span>
  );
}
