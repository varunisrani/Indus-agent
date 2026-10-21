// Locate the Electron binary for tests that need a real one. Electron 44 downloads its binary on
// first use (`node_modules/electron/install.js`, cached in ELECTRON_CACHE / electron_config_cache);
// tests never download it themselves — they skip with the reason instead.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);

export interface ElectronBinary {
  path: string | null;
  reason: string;
}

export function electronBinary(): ElectronBinary {
  let pkgDir: string;
  try {
    pkgDir = path.dirname(require.resolve("electron/package.json"));
  } catch {
    return { path: null, reason: "the electron package is not installed" };
  }
  const pathFile = path.join(pkgDir, "path.txt");
  if (!fs.existsSync(pathFile)) {
    return {
      path: null,
      reason:
        "the Electron binary is not downloaded (run `node node_modules/electron/install.js` in apps/desktop)",
    };
  }
  const binary = path.join(pkgDir, "dist", fs.readFileSync(pathFile, "utf8").trim());
  return fs.existsSync(binary)
    ? { path: binary, reason: "" }
    : { path: null, reason: `missing ${binary}` };
}

/** How to give Electron a display here: xvfb-run on a headless Linux box, nothing elsewhere. */
export function displayWrapper(): { command: string[]; reason: string } | null {
  if (process.platform !== "linux") return { command: [], reason: "" };
  if (process.env.DISPLAY || process.env.WAYLAND_DISPLAY) return { command: [], reason: "" };
  const probe = spawnSync("sh", ["-c", "command -v xvfb-run"], { encoding: "utf8" });
  if (probe.status === 0 && probe.stdout.trim() !== "")
    return { command: [probe.stdout.trim(), "-a"], reason: "" };
  return null;
}

/** Chromium refuses to run as root without its OS sandbox off; test runs only (D-16.81). */
export function rootSandboxArgs(): string[] {
  return typeof process.getuid === "function" && process.getuid() === 0 ? ["--no-sandbox"] : [];
}
