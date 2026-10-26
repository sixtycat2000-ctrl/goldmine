import { useEffect, type ReactNode } from "react";
import { useFirstScrollIntent } from "../../lib/useFirstScrollIntent";
import { usePrefersReducedMotion } from "../../lib/hooks";

/*
 * Lenis inertia scrolling wired into the GSAP ticker so ScrollTrigger stays in
 * sync. Both libraries stay unloaded until the first scroll intent (nothing
 * above the fold needs them) and everything is torn down on unmount.
 */
export function SmoothScrollProvider({ children }: { children: ReactNode }) {
  const reduced = usePrefersReducedMotion();
  const intent = useFirstScrollIntent();
  const active = !reduced && intent;

  useEffect(() => {
    if (!active) return;
    let teardown: (() => void) | null = null;
    let cancelled = false;

    (async () => {
      const [{ default: Lenis }, { gsap }, { ScrollTrigger }] = await Promise.all([
        import("lenis"),
        import("gsap"),
        import("gsap/ScrollTrigger"),
      ]);
      if (cancelled) return;
      gsap.registerPlugin(ScrollTrigger);
      // allowNestedScroll: inner scrollers (lesson drawer, chip rows) keep native wheel
      const lenis = new Lenis({ lerp: 0.11, smoothWheel: true, allowNestedScroll: true });
      lenis.on("scroll", ScrollTrigger.update);
      const raf = (time: number) => lenis.raf(time * 1000);
      gsap.ticker.add(raf);
      gsap.ticker.lagSmoothing(0);
      teardown = () => {
        gsap.ticker.remove(raf);
        lenis.destroy();
      };
    })();

    return () => {
      cancelled = true;
      teardown?.();
    };
  }, [active]);

  return <>{children}</>;
}
