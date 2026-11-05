import { useLayoutEffect, useRef, type ReactNode } from "react";
import { motion, useMotionValue, useSpring, type MotionValue } from "framer-motion";
import { useFinePointer } from "../../lib/hooks";
import { useMotionPreference } from "../../lib/use-motion-preference";

const MAGNET_RADIUS = 40; // px of pull zone around the button (spec)

/*
 * One shared window pointermove listener + one rAF for every MagneticButton on
 * the page: rects are read for all subscribers first, then all transforms are
 * written (no read/write interleaving). Each button's spring/motion values are
 * its own; the shell is pointer-events-none so the magnet zone never steals
 * clicks. Skipped entirely for coarse pointers and reduced motion.
 */

type Sub = {
  el: HTMLElement;
  mx: MotionValue<number>;
  my: MotionValue<number>;
};

const subs = new Set<Sub>();
let listening = false;
let frame: number | null = null;
let generation = 0;
let px = -1;
let py = -1;

function flush() {
  frame = null;
  if (!subs.size) return;
  const rects = new Map<Sub, DOMRect>();
  for (const s of subs) rects.set(s, s.el.getBoundingClientRect()); // reads first
  for (const s of subs) {
    const r = rects.get(s)!;
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dx = px - cx;
    const dy = py - cy;
    const dist = Math.hypot(dx, dy);
    const zone = Math.max(r.width, r.height) / 2 + MAGNET_RADIUS;
    if (dist > zone) {
      s.mx.set(0);
      s.my.set(0);
      continue;
    }
    const pull = Math.min(6, (1 - dist / zone) * 9);
    s.mx.set((dx / (dist || 1)) * pull);
    s.my.set((dy / (dist || 1)) * pull);
  }
}

function onPointerMove(e: PointerEvent) {
  px = e.clientX;
  py = e.clientY;
  if (frame !== null) return;
  const token = generation;
  frame = requestAnimationFrame(() => {
    if (token === generation) flush();
  });
}

function subscribe(s: Sub) {
  subs.add(s);
  if (!listening) {
    listening = true;
    window.addEventListener("pointermove", onPointerMove, { passive: true });
  }
}

function unsubscribe(s: Sub) {
  subs.delete(s);
  if (!subs.size) {
    window.removeEventListener("pointermove", onPointerMove);
    listening = false;
    generation++;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
  }
}

type Props = {
  href?: string;
  onClick?: () => void;
  variant?: "gold" | "ghost";
  className?: string;
  ariaLabel?: string;
  external?: boolean;
  children: ReactNode;
};

export function MagneticButton({
  href,
  onClick,
  variant = "ghost",
  className = "",
  ariaLabel,
  external,
  children,
}: Props) {
  const shellRef = useRef<HTMLSpanElement>(null);
  const fine = useFinePointer();
  const reduced = useMotionPreference();
  const mx = useMotionValue(0);
  const my = useMotionValue(0);
  const x = useSpring(mx, { stiffness: 180, damping: 16, mass: 0.4 });
  const y = useSpring(my, { stiffness: 180, damping: 16, mass: 0.4 });

  const active = fine && !reduced;

  useLayoutEffect(() => {
    const el = shellRef.current;
    const reset = () => {
      mx.jump(0);
      my.jump(0);
      x.jump(0);
      y.jump(0);
      if (el) el.style.transform = "none";
    };
    if (!active || !el) {
      reset();
      return;
    }
    const sub: Sub = { el, mx, my };
    subscribe(sub);
    return () => {
      unsubscribe(sub);
      reset();
    };
  }, [active, mx, my, x, y]);

  const base =
    variant === "gold"
      ? "bg-gold text-bg0 border border-gold hover:text-bg0"
      : "text-gold border border-line-strong hover:border-gold/60";

  const cls = `pointer-events-auto group/mag relative inline-flex items-center gap-2 overflow-hidden rounded-full px-6 py-3 text-[14px] font-medium transition-colors duration-300 ${base} ${className}`;

  const inner = (
    <>
      <span className="liquid" aria-hidden="true" />
      <span className={`relative z-10 transition-colors duration-300 ${variant === "ghost" ? "group-hover/mag:text-bg0" : ""}`}>
        {children}
      </span>
    </>
  );

  return (
    <motion.span ref={shellRef} style={{ x, y, display: "inline-block" }} className="pointer-events-none">
      {href ? (
        <motion.a
          href={href}
          aria-label={ariaLabel}
          className={cls}
          {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
          whileTap={active ? { scale: 0.97 } : undefined}
        >
          {inner}
        </motion.a>
      ) : (
        <motion.button
          type="button"
          onClick={onClick}
          aria-label={ariaLabel}
          className={cls}
          whileTap={active ? { scale: 0.97 } : undefined}
        >
          {inner}
        </motion.button>
      )}
    </motion.span>
  );
}
