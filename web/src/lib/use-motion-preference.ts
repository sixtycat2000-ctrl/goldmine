import { useSyncExternalStore } from "react";

// Keep the listener and snapshots on one native preference source. The
// installed animation-library hook reads only its initial value.
const preference = typeof window !== "undefined" && typeof window.matchMedia === "function"
  ? window.matchMedia("(prefers-reduced-motion: reduce)")
  : null;

function subscribe(notify: () => void) {
  preference?.addEventListener("change", notify);
  return () => preference?.removeEventListener("change", notify);
}

const getSnapshot = () => preference?.matches ?? true;
const getServerSnapshot = () => true;

export function useMotionPreference() {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
