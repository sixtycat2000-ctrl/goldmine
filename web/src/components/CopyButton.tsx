import { useEffect, useState } from "react";
import { useLang, pick } from "../lib/lang";
import { IconCopy } from "./Icons";

/** Clipboard button with 1.2 s "copied" state (execCommand fallback). */
export function CopyButton({ text, label }: { text: string; label?: { zh: string; en: string } }) {
  const lang = useLang();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1200);
    return () => window.clearTimeout(t);
  }, [copied]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } finally {
        ta.remove();
      }
    }
    setCopied(true);
  };

  return (
    <button
      type="button"
      onClick={copy}
      className="inline-flex items-center gap-1.5 font-mono text-[11px] text-sub transition-colors hover:text-gold"
      aria-live="polite"
    >
      <IconCopy size={13} />
      {copied
        ? pick(lang, "已复制", "copied")
        : label
          ? pick(lang, label.zh, label.en)
          : pick(lang, "复制", "copy")}
    </button>
  );
}
