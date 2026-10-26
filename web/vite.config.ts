import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const root = dirname(fileURLToPath(import.meta.url));

// Workers static assets resolve /learn → learn.html and /docs → docs.html via
// `html_handling: auto-trailing-slash`; mirror that in dev/preview so URLs match prod.
function edgePathRewrites(): Plugin {
  const rewrite = (url: string): string | null => {
    if (url === "/learn") return "/learn.html";
    if (url === "/docs") return "/docs.html";
    return null;
  };
  const mw = (req: { url?: string }, _res: unknown, next: () => void) => {
    if (req.url) {
      const [path, query] = req.url.split("?");
      const to = rewrite(path);
      if (to) req.url = query ? `${to}?${query}` : to;
    }
    next();
  };
  return {
    name: "edge-path-rewrites",
    configureServer(server) {
      server.middlewares.use(mw);
    },
    configurePreviewServer(server) {
      server.middlewares.use(mw);
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), edgePathRewrites()],
  appType: "mpa",
  build: {
    target: "es2022",
    assetsDir: "assets",
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      input: {
        index: resolve(root, "index.html"),
        learn: resolve(root, "learn.html"),
      },
    },
  },
});
