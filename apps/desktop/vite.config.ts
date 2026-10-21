// The renderer build (P20-15). `vite build` writes dist/index.html (main's packaged `file://`
// origin) and `vite` serves it in development (main reads INDUS_DESKTOP_DEV_SERVER). Both get
// P20-04's Content-Security-Policy as the first tag in <head> (`injectCspMeta`).
//
// No React-refresh plugin: its preamble is an inline script, which the CSP forbids. JSX goes
// through Vite's own transform; a dev edit reloads the page.

import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";
import {
  buildCsp,
  injectCspMeta,
  RUNTIME_CONNECT_SOURCES,
  withRuntimeConnect,
} from "./electron/security/csp.ts";

export const DEV_HOST = "127.0.0.1";
export const DEV_PORT = 5174;
export const DEV_ORIGIN = `http://${DEV_HOST}:${DEV_PORT}`;

export { RUNTIME_CONNECT_SOURCES };

/** The policy written into the page itself. */
export function rendererMetaCsp(input: { dev: boolean; devServerOrigin?: string }): string {
  return withRuntimeConnect(buildCsp(input));
}

export function cspMetaPlugin(): Plugin {
  let dev = false;
  return {
    name: "indus-csp-meta",
    configResolved(config) {
      dev = config.command === "serve";
    },
    transformIndexHtml: {
      order: "post",
      handler(html) {
        const csp = dev
          ? rendererMetaCsp({ dev: true, devServerOrigin: DEV_ORIGIN })
          : rendererMetaCsp({ dev: false });
        return injectCspMeta(html, csp);
      },
    },
  };
}

export default defineConfig({
  root: import.meta.dirname,
  // Relative asset URLs: the packaged page is opened from file://.
  base: "./",
  publicDir: false,
  plugins: [tailwindcss(), cspMetaPlugin()],
  server: { host: DEV_HOST, port: DEV_PORT, strictPort: true },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "chrome130",
    chunkSizeWarningLimit: 1_500,
    rollupOptions: {
      // Radix ships "use client" markers for server-component bundlers; meaningless here.
      onwarn(warning, warn) {
        if (warning.code === "MODULE_LEVEL_DIRECTIVE") return;
        warn(warning);
      },
    },
  },
});
