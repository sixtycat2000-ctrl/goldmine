import { useEffect, type ReactNode } from "react";
import { usePrefersReducedMotion } from "../../lib/hooks";
import { useFirstScrollIntent } from "../../lib/useFirstScrollIntent";

/*
 * Lenis inertia scrolling wired into the GSAP ticker so ScrollTrigger stays in
 * sync. Both libraries stay unloaded until the first scroll intent — nothing
 * above the fold needs them — and everything is torn down on unmount.
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
      const lenis = new Lenis({
        lerp: 0.11,
        smoothWheel: true,
        allowNestedScroll: true,
        wheelMultiplier: 1,
        touchMultiplier: 1,
        anchors: { offset: -72 }, // same-page hash links ride Lenis; clears the 56px nav
      });
      lenis.on("scroll", ScrollTrigger.update);
      if (location.hash) lenis.scrollTo(location.hash, { immediate: true });
      ScrollTrigger.config({ ignoreMobileResize: true }); // iOS URL-bar collapse
      document.fonts.ready.then(() => ScrollTrigger.refresh());
      const onPageShow = (e: PageTransitionEvent) => {
        if (e.persisted) ScrollTrigger.refresh(); // bfcache return
      };
      window.addEventListener("pageshow", onPageShow);
      const raf = (time: number) => lenis.raf(time * 1000);
      gsap.ticker.add(raf);
      gsap.ticker.lagSmoothing(0);
      teardown = () => {
        window.removeEventListener("pageshow", onPageShow);
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
