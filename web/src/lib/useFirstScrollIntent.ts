import { useEffect, useState } from "react";

/**
 * True once the visitor shows scroll intent (wheel / touch / key / scrollbar
 * drag). Heavy scroll machinery (Lenis, GSAP ScrollTrigger) stays unloaded
 * until then — nothing above the fold needs it, and the critical window
 * stays lean.
 */
export function useFirstScrollIntent(): boolean {
  const [intent, setIntent] = useState(false);
  useEffect(() => {
    if (intent) return;
    const opts: AddEventListenerOptions = { passive: true, once: true };
    const on = () => setIntent(true);
    window.addEventListener("wheel", on, opts);
    window.addEventListener("touchmove", on, opts);
    window.addEventListener("scroll", on, opts);
    window.addEventListener("keydown", on, opts);
    return () => {
      window.removeEventListener("wheel", on);
      window.removeEventListener("touchmove", on);
      window.removeEventListener("scroll", on);
      window.removeEventListener("keydown", on);
    };
  }, [intent]);
  return intent;
}
