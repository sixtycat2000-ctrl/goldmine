import { useEffect, useRef, type ReactNode } from "react";
import { useFinePointer, usePrefersReducedMotion } from "../../lib/hooks";

const MAX_TILT = 5; // deg

/**
 * 3D perspective tilt on hover, transform-only (composite). rAF-coalesced
 * pointermove; springs back on leave via CSS transition. Off on touch and
 * under prefers-reduced-motion.
 */
export function TiltCard({ children, className = "" }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const frame = useRef(0);
  const fine = useFinePointer();
  const reduced = usePrefersReducedMotion();
  const enabled = fine && !reduced;

  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  const apply = (rx: number, ry: number) => {
    const el = ref.current;
    if (el) el.style.transform = `perspective(900px) rotateX(${rx}deg) rotateY(${ry}deg)`;
  };

  const onMove = (e: React.PointerEvent) => {
    if (!enabled || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width - 0.5;
    const py = (e.clientY - rect.top) / rect.height - 0.5;
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => apply(-py * MAX_TILT, px * MAX_TILT));
  };

  const onLeave = () => {
    cancelAnimationFrame(frame.current);
    apply(0, 0);
  };

  return (
    <div
      ref={ref}
      onPointerMove={enabled ? onMove : undefined}
      onPointerLeave={enabled ? onLeave : undefined}
      className={`transition-transform duration-500 ease-out will-change-transform ${className}`}
      style={{ transformStyle: "preserve-3d" }}
    >
      {children}
    </div>
  );
}
