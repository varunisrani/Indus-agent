// Bundle the main process (one ESM file), the sandboxed preload and the preview guest's preload
// (plain-JS CommonJS files) into dist-electron/ with the workspace's Vite. `electron`, Node
// built-ins and the native `node-pty` (loaded on first use, unpacked by packaging) stay external;
// everything else (zod, @indus/client, our modules) is inlined. The env defaults table
// (`INDUS_DESKTOP_ENV_DEFAULTS`, JSON) is validated and baked into the main bundle.
//
//   node scripts/build-electron.mjs [--out DIR]

import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";
import { validateEnvDefaults } from "../electron/envDefaults.ts";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function envDefaultsJson() {
  const raw = process.env.INDUS_DESKTOP_ENV_DEFAULTS;
  if (!raw) return "{}";
  return JSON.stringify(validateEnvDefaults(JSON.parse(raw)));
}

const BUILTINS = /^(node:|electron$|node-pty$)/;

export async function buildElectron(outDir = path.join(APP_DIR, "dist-electron")) {
  const common = {
    configFile: false,
    root: APP_DIR,
    logLevel: "warn",
    publicDir: false,
    ssr: { noExternal: true, external: ["electron", "node-pty"] },
  };
  await build({
    ...common,
    define: { __INDUS_ENV_DEFAULTS__: JSON.stringify(envDefaultsJson()) },
    build: {
      ssr: path.join(APP_DIR, "electron/main.ts"),
      outDir,
      emptyOutDir: true,
      target: "node22",
      minify: false,
      sourcemap: true,
      rolldownOptions: {
        external: (id) => BUILTINS.test(id),
        output: { format: "es", entryFileNames: "main.js" },
      },
    },
  });
  await build({
    ...common,
    build: {
      ssr: path.join(APP_DIR, "electron/preload.ts"),
      outDir,
      emptyOutDir: false,
      target: "chrome130",
      minify: false,
      sourcemap: false,
      rolldownOptions: {
        external: (id) => id === "electron",
        output: { format: "cjs", entryFileNames: "preload.cjs" },
      },
    },
  });
  await build({
    ...common,
    build: {
      ssr: path.join(APP_DIR, "electron/preview/guestPreloadEntry.ts"),
      outDir,
      emptyOutDir: false,
      target: "chrome130",
      minify: false,
      sourcemap: false,
      rolldownOptions: {
        external: (id) => id === "electron",
        output: { format: "cjs", entryFileNames: "preview-guest.cjs" },
      },
    },
  });
  return outDir;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const at = process.argv.indexOf("--out");
  const out = await buildElectron(at > 0 ? path.resolve(process.argv[at + 1] ?? "") : undefined);
  process.stdout.write(`built ${path.relative(process.cwd(), out)}\n`);
}
