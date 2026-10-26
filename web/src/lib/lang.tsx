import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

/*
 * Bilingual layer, contract-compatible with the v1–v3 site:
 * same `gm-lang` localStorage key, same `zh-CN` / `en` <html lang> values,
 * so a visitor's existing preference survives the redesign.
 */

export type Lang = "zh" | "en";

function initialLang(): Lang {
  try {
    const stored = localStorage.getItem("gm-lang");
    if (stored === "en" || stored === "zh") return stored;
    if (navigator.language.toLowerCase().startsWith("zh")) return "zh";
    return "en";
  } catch {
    return "zh";
  }
}

type LangState = { lang: Lang; setLang: (l: Lang) => void };
const LangCtx = createContext<LangState>({ lang: "zh", setLang: () => {} });

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(initialLang);

  const setLang = useCallback((l: Lang) => setLangState(l), []);

  useEffect(() => {
    document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
    try {
      localStorage.setItem("gm-lang", lang);
    } catch {
      /* private mode: preference is session-only */
    }
  }, [lang]);

  return <LangCtx.Provider value={{ lang, setLang }}>{children}</LangCtx.Provider>;
}

export function useLangState(): LangState {
  return useContext(LangCtx);
}

export function useLang(): Lang {
  return useContext(LangCtx).lang;
}

/** Pick the active string of a bilingual pair. */
export function pick(lang: Lang, zh: string, en: string): string {
  return lang === "en" ? en : zh;
}

/** <T zh="…" en="…"/> — the React replacement for data-zh/data-en. */
export function T({ zh, en }: { zh: string; en: string }) {
  return <>{pick(useContext(LangCtx).lang, zh, en)}</>;
}
