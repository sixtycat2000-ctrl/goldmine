import { useLayoutEffect, useRef, type ReactNode } from "react";
import { useFinePointer } from "../../lib/hooks";
import { useMotionPreference } from "../../lib/use-motion-preference";

type Props = {
  className?: string;
  children: ReactNode;
  cursor?: "default" | "pointer";
  onClick?: () => void;
  ariaLabel?: string;
  as?: "div" | "article" | "li";
};

/**
 * Glass card with a cursor-tracked radial wash (--mx/--my drive .spot::before)
 * and a masked 1px border glow. Coordinates are written straight to style in a
 * rAF batch — no React state per pointermove. Touch devices and reduced-motion
 * users simply see the static glass surface.
 */
export function SpotlightCard({
  className = "",
  children,
  cursor = "default",
  onClick,
  ariaLabel,
  as = "div",
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const frame = useRef<number | null>(null);
  const generation = useRef(0);
  const pt = useRef({ x: 0, y: 0 });
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
    if (!enabled) {
      ref.current?.style.removeProperty("--mx");
      ref.current?.style.removeProperty("--my");
    }
    return () => {
      enabledRef.current = false;
      cancelPending();
    };
  }, [enabled]);

  const onMove = (e: React.PointerEvent) => {
    if (!enabledRef.current || (!e.clientX && !e.clientY)) return;
    const host = e.currentTarget as HTMLElement;
    const rect = host.getBoundingClientRect();
    pt.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    cancelPending();
    const token = generation.current;
    frame.current = requestAnimationFrame(() => {
      if (!enabledRef.current || token !== generation.current) return;
      frame.current = null;
      host.style.setProperty("--mx", `${pt.current.x}px`);
      host.style.setProperty("--my", `${pt.current.y}px`);
    });
  };

  const Tag = as;
  return (
    <Tag
      ref={ref as never}
      onPointerMove={enabled ? onMove : undefined}
      onClick={onClick}
      aria-label={ariaLabel}
      className={`spot glow-border relative overflow-hidden rounded-2xl glass ${cursor === "pointer" ? "cursor-pointer" : ""} ${className}`}
    >
      {children}
    </Tag>
  );
}
