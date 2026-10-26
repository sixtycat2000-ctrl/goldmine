// Injects <link rel="preload" as="font"> for the three latin workhorse fonts
// (Instrument Serif 400, Inter var latin, JetBrains Mono var latin) into both
// built HTML files. Runs after `vite build`; font URLs are content-hashed so
// they are read back out of the emitted CSS.
import { readFileSync, writeFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const assetsDir = join(dist, "assets");

const cssFiles = readdirSync(assetsDir)
  .filter((f) => f.endsWith(".css"))
  .map((f) => join(assetsDir, f));

let css = "";
for (const f of cssFiles) css += readFileSync(f, "utf8");

const pick = (re) => {
  const m = css.match(re);
  if (!m) throw new Error(`font preload: pattern not found: ${re}`);
  const file = m[0].split("/").pop();
  return `assets/${file}`;
};

const fonts = [
  pick(/instrument-serif-latin-400-normal-[A-Za-z0-9_-]+\.woff2/),
  pick(/inter-latin-wght-normal-[A-Za-z0-9_-]+\.woff2/),
  pick(/jetbrains-mono-latin-wght-normal-[A-Za-z0-9_-]+\.woff2/),
];

const links = fonts
  .map((href) => `<link rel="preload" as="font" type="font/woff2" crossorigin href="/${href}">`)
  .join("\n");

for (const file of ["index.html", "learn.html"]) {
  const p = join(dist, file);
  let html = readFileSync(p, "utf8");
  if (!html.includes('rel="preload" as="font"')) {
    // preloads go first in head: fonts start fetching before CSS is parsed
    html = html.replace(/(<head[^>]*>\s*)/, `$1${links}\n`);
  }
  if (file === "learn.html") {
    // the app JS must not compete with the streaming HTML shell for the slow
    // pipe — the static header paints without it, hydration follows after
    html = html.replace(
      /<link rel="modulepreload" crossorigin href="([^"]+)">/g,
      '<link rel="modulepreload" fetchpriority="low" crossorigin href="$1">',
    );
  }
  writeFileSync(p, html);
  console.log(`font preloads injected -> ${file}`);
}

// The entry stylesheet's preloaded faces go font-display:block (the async Noto
// sheet keeps swap): text paints once, with the final font, at the shell
// paint — no late swap-repaint to supersede the LCP.
for (const f of cssFiles) {
  const src = readFileSync(f, "utf8");
  if (!src.includes("font-display:swap")) continue;
  if (f.includes("400-")) continue; // async Noto sheet: swap is correct there
  writeFileSync(f, src.replaceAll("font-display:swap", "font-display:block"));
  console.log(`font-display:block patched -> ${f}`);
}
