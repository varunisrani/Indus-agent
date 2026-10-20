import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// The dashboard backend (`indus dashboard`) listens on 127.0.0.1:9119; in development the Vite
// server forwards every `/api` request, WebSocket upgrades included, to it. The dev-only bootstrap
// plugin that copies the token and flags from the backend's own index.html is P18-29's.
const BACKEND = "http://127.0.0.1:9119";

function chunkFor(id: string): string | undefined {
  if (!id.includes("node_modules")) return undefined;
  if (/[\\/]node_modules[\\/](react|react-dom|scheduler|react-router)[\\/]/.test(id))
    return "react";
  if (id.includes("@xterm")) return "xterm";
  return "vendor";
}

export default defineConfig({
  plugins: [tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: false, ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 600,
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
