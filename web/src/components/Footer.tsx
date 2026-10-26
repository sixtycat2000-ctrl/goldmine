import { T } from "../lib/lang";
import { REPO_URL } from "../lib/metrics";

export function Footer() {
  return (
    <footer className="hairline-t">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-5 py-8 font-mono text-[11px] tracking-[0.08em] text-sub sm:flex-row">
        <span>
          goldmine · <T zh="本地优先语义搜索" en="local-first semantic search" /> · MIT
        </span>
        <span className="flex gap-5">
          <a href="/" className="transition-colors hover:text-ink">index</a>
          <a href="/docs" className="transition-colors hover:text-ink">docs</a>
          <a href="/learn" className="transition-colors hover:text-ink">learn</a>
          <a href={REPO_URL} className="transition-colors hover:text-ink">GitHub</a>
        </span>
        <span>
          <span className="text-gold">0 bytes</span> <T zh="离开你的机器" en="leave your machine" />
        </span>
      </div>
    </footer>
  );
}
