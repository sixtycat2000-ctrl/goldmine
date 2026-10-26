import { useEffect, useState } from "react";

function makeMediaHook(query: string) {
  return function useMedia(): boolean {
    const [matches, setMatches] = useState(
      () => typeof window !== "undefined" && window.matchMedia(query).matches,
    );
    useEffect(() => {
      const mq = window.matchMedia(query);
      const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
      mq.addEventListener("change", onChange);
      setMatches(mq.matches);
      return () => mq.removeEventListener("change", onChange);
    }, [query]);
    return matches;
  };
}

/** Live prefers-reduced-motion state (for non-FramerMotion code paths). */
export const usePrefersReducedMotion = makeMediaHook("(prefers-reduced-motion: reduce)");

/** True only when hover-capable fine pointer — gates all cursor FX on touch devices. */
export const useFinePointer = makeMediaHook("(hover: hover) and (pointer: fine)");
