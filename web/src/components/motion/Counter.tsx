import { useEffect, useRef } from "react";
import { useInView } from "framer-motion";

const easeOutExpo = (t: number) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t));

type Props = {
  end: number;
  decimals?: number;
  duration?: number; // ms
  delay?: number; // ms before the tick starts
  prefix?: string;
  suffix?: string;
  className?: string;
  locale?: boolean;
};

/**
 * Count-up numeral that runs once when scrolled into view. Writes straight to
 * the DOM node — no per-frame React state. Reduced motion: static value.
 */
export function Counter({
  end,
  decimals = 0,
  duration = 1400,
  delay = 0,
  prefix = "",
  suffix = "",
  className,
  locale = true,
}: Props) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, amount: 0.35 });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fmt = (v: number) =>
      locale
        ? v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
        : v.toFixed(decimals);
    el.textContent = `${prefix}${fmt(0)}${suffix}`;
    if (!inView) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      el.textContent = `${prefix}${fmt(end)}${suffix}`;
      return;
    }

    let raf = 0;
    let t0 = 0;
    const tick = (now: number) => {
      if (!t0) t0 = now + delay; // first frame anchors the delay window
      const p = Math.min(1, Math.max(0, (now - t0) / duration));
      el.textContent = `${prefix}${fmt(end * easeOutExpo(p))}${suffix}`;
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, end, duration, delay, decimals, prefix, suffix, locale]);

  return <span ref={ref} className={className} />;
}
