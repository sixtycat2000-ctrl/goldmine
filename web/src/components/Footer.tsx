import { T, useLang } from "../lib/lang";
import { REPO_URL } from "../lib/metrics";

export function Footer() {
  const zh = useLang() === "zh";
  return (
    <footer className="hairline-t">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-5 py-8 text-[13px] text-sub sm:flex-row">
        <span>goldmine · <T zh="本机文件检索" en="local file retrieval" /> · MIT</span>
        <nav aria-label={zh ? "页尾导航" : "Footer navigation"} className="flex flex-wrap justify-center gap-5">
          <a href="/" className="transition-colors hover:text-ink"><T zh="首页" en="Home" /></a>
          <a href="/docs" className="transition-colors hover:text-ink"><T zh="文档" en="Docs" /></a>
          <a href="/learn" className="transition-colors hover:text-ink"><T zh="学习" en="Learn" /></a>
          <a href={REPO_URL} className="transition-colors hover:text-ink">GitHub</a>
        </nav>
        <span className="text-gold"><T zh="本机建库与检索" en="Local indexing and retrieval" /></span>
      </div>
    </footer>
  );
}
