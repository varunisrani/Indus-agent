import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { brandFavicon } from "./vite.brand.ts";
import { CHUNK_LIMIT_KB, chunkBudget, chunkFor } from "./vite.chunks.ts";
import { apiProxy, backendUrl, devBootstrap } from "./vite.dev-bootstrap.ts";

// The dashboard backend (`indus dashboard`) listens on 127.0.0.1:9119 (`INDUS_WEB_DEV_BACKEND`
// points elsewhere); in development the Vite server forwards every `/api` request, WebSocket
// upgrades included, to it, and the dev-only bootstrap plugin copies the token and flags from the
// backend's own index.html (P18-29). Builds land in `dist/`; the release copies them into the
// Python package (`python -m indus.web.bundle publish`).
const BACKEND = backendUrl();

export default defineConfig({
  plugins: [tailwindcss(), devBootstrap({ backend: BACKEND }), chunkBudget(), brandFavicon()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: apiProxy(BACKEND),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: CHUNK_LIMIT_KB,
    rollupOptions: {
      output: { manualChunks: chunkFor },
      // Radix ships "use client" markers for server-component bundlers; meaningless in an SPA.
      onwarn(warning, warn) {
        if (warning.code === "MODULE_LEVEL_DIRECTIVE") return;
        warn(warning);
      },
    },
  },
});
