import { useRef, type ReactNode } from "react";
import { motion, useMotionValue, useSpring, useReducedMotion } from "framer-motion";
import { useFinePointer } from "../../lib/hooks";

const MAGNET_RADIUS = 40; // px of pull zone around the button (spec)

type Props = {
  href?: string;
  onClick?: () => void;
  variant?: "gold" | "ghost";
  className?: string;
  ariaLabel?: string;
  external?: boolean;
  children: ReactNode;
};

/**
 * Magnetic CTA: pulls up to ~6px toward the cursor within a 40px zone (spring
 * return), with a liquid gold fill sweeping in on hover. The 40px zone is a
 * transparent padding ring compensated by negative margin, so layout is
 * unchanged. Pointer FX are gated to fine pointers and skipped under
 * reduced motion; keyboard focus gets the standard gold ring.
 */
export function MagneticButton({
  href,
  onClick,
  variant = "ghost",
  className = "",
  ariaLabel,
  external,
  children,
}: Props) {
  const ref = useRef<HTMLSpanElement>(null);
  const fine = useFinePointer();
  const reduced = useReducedMotion();
  const mx = useMotionValue(0);
  const my = useMotionValue(0);
  const x = useSpring(mx, { stiffness: 180, damping: 16, mass: 0.4 });
  const y = useSpring(my, { stiffness: 180, damping: 16, mass: 0.4 });

  const active = fine && !reduced;

  const onMove = (e: React.PointerEvent) => {
    if (!active || !ref.current) return;
    const r = ref.current.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dx = e.clientX - cx;
    const dy = e.clientY - cy;
    const dist = Math.hypot(dx, dy);
    const zone = Math.max(r.width, r.height) / 2 + MAGNET_RADIUS;
    if (dist > zone) return;
    const pull = Math.min(6, (1 - dist / zone) * 9);
    mx.set((dx / (dist || 1)) * pull);
    my.set((dy / (dist || 1)) * pull);
  };
  const onLeave = () => {
    mx.set(0);
    my.set(0);
  };

  const base =
    variant === "gold"
      ? "bg-gold text-bg0 border border-gold hover:text-bg0"
      : "text-gold border border-line-strong hover:border-gold/60";

  const cls = `group/mag relative inline-flex items-center gap-2 overflow-hidden rounded-full px-6 py-3 font-mono text-[13px] tracking-[0.08em] uppercase transition-colors duration-300 ${base} ${className}`;

  const inner = (
    <>
      <span className="liquid" aria-hidden="true" />
      <span className={`relative z-10 transition-colors duration-300 ${variant === "ghost" ? "group-hover/mag:text-bg0" : ""}`}>
        {children}
      </span>
    </>
  );

  return (
    <motion.span
      ref={ref}
      style={{ x, y, display: "inline-block" }}
      className="-m-10 p-10"
      onPointerMove={active ? onMove : undefined}
      onPointerLeave={active ? onLeave : undefined}
    >
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
