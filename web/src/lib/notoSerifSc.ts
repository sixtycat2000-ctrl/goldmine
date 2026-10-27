/*
 * Noto Serif SC gives Chinese its display serif voice cross-platform, but its
 * @font-face sheet is ~120KB of unicode-range blocks — render-blocking if
 * bundled. The `?url` import emits it as a plain stylesheet asset; we attach
 * it with a <link> after mount (Songti/serif covers the interim). Loads
 * immediately when the visitor is already reading Chinese, otherwise idle.
 */
import notoCssUrl from "@fontsource/noto-serif-sc/400.css?url";

let started = false;

export function loadNotoSerifSc(): void {
  if (started) return;
  started = true;

  const kick = () => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = notoCssUrl;
    document.head.appendChild(link);
  };

  // a zh-conditional preload may already be in head (injected at build) —
  // the sheet is then in flight, so attach immediately
  const preloaded = document.querySelector('link[rel="preload"][as="style"]');

  let zhPref = false;
  try {
    zhPref =
      localStorage.getItem("gm-lang") === "zh" ||
      (!localStorage.getItem("gm-lang") && navigator.language.toLowerCase().startsWith("zh"));
  } catch {
    zhPref = navigator.language?.toLowerCase().startsWith("zh");
  }

  if (zhPref || preloaded) {
    kick();
    return;
  }
  if (typeof requestIdleCallback === "function") {
    requestIdleCallback(kick, { timeout: 3000 });
  } else {
    setTimeout(kick, 900);
  }
}
