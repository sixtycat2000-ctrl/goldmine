import { useLayoutEffect, useRef, type ReactNode } from "react";
import { useFinePointer } from "../../lib/hooks";
import { useMotionPreference } from "../../lib/use-motion-preference";

const MAX_TILT = 5; // deg

/**
 * 3D perspective tilt on hover, transform-only (composite). rAF-coalesced
 * pointermove; springs back on leave via CSS transition. Off on touch and
 * under prefers-reduced-motion.
 */
export function TiltCard({ children, className = "" }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const frame = useRef<number | null>(null);
  const generation = useRef(0);
  const fine = useFinePointer();
  const reduced = useMotionPreference();
  const enabled = fine && !reduced;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const cancelPending = () => {
    generation.current++;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
  };

  useLayoutEffect(() => {
    enabledRef.current = enabled;
    if (!enabled && ref.current) ref.current.style.transform = "none";
    return () => {
      enabledRef.current = false;
      cancelPending();
    };
  }, [enabled]);

  const apply = (rx: number, ry: number) => {
    const el = ref.current;
    if (el) el.style.transform = `perspective(900px) rotateX(${rx}deg) rotateY(${ry}deg)`;
  };

  const onMove = (e: React.PointerEvent) => {
    if (!enabledRef.current || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width - 0.5;
    const py = (e.clientY - rect.top) / rect.height - 0.5;
    cancelPending();
    const token = generation.current;
    frame.current = requestAnimationFrame(() => {
      if (!enabledRef.current || token !== generation.current) return;
      frame.current = null;
      apply(-py * MAX_TILT, px * MAX_TILT);
    });
  };

  const onLeave = () => {
    cancelPending();
    apply(0, 0);
  };

  return (
    <div
      ref={ref}
      onPointerMove={enabled ? onMove : undefined}
      onPointerLeave={enabled ? onLeave : undefined}
      className={`transition-transform duration-500 ease-out will-change-transform ${className}`}
      style={{ transformStyle: "preserve-3d", transitionDuration: enabled ? undefined : "0s", transform: enabled ? undefined : "none" }}
    >
      {children}
    </div>
  );
}
