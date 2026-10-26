import { useEffect, useRef, type ReactNode } from "react";

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
  const frame = useRef(0);
  const pt = useRef({ x: 0, y: 0 });

  useEffect(() => {
    return () => cancelAnimationFrame(frame.current);
  }, []);

  const onMove = (e: React.PointerEvent) => {
    if (!e.clientX && !e.clientY) return; // touch scroll artifacts
    const host = e.currentTarget as HTMLElement;
    const rect = host.getBoundingClientRect();
    pt.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      host.style.setProperty("--mx", `${pt.current.x}px`);
      host.style.setProperty("--my", `${pt.current.y}px`);
    });
  };

  const Tag = as;
  return (
    <Tag
      ref={ref as never}
      onPointerMove={onMove}
      onClick={onClick}
      aria-label={ariaLabel}
      className={`spot glow-border relative overflow-hidden rounded-2xl glass ${cursor === "pointer" ? "cursor-pointer" : ""} ${className}`}
    >
      {children}
    </Tag>
  );
}
