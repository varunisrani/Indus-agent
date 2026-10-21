// Unsigned desktop artefacts for the host OS (P20-38). Steps: renderer build (Vite → dist/),
// main + preload build (dist-electron/), build stamp, staged app (build/app/), electron-builder
// with publishing off and every signing variable removed from its environment. The afterPack hook
// in electron-builder.config.cjs flips the fuses and checks the archive.
//
//   node scripts/dist-unsigned.mjs [--dir] [--skip-build] [--channel stable|canary]
//
//   --dir         unpacked app only (no DMG/ZIP/NSIS/AppImage), fastest local check
//   --skip-build  reuse dist/ and dist-electron/ from an earlier build
//
// Output: build/release/. Signing and notarisation are P23; nothing here uploads anything.

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { APP_DIR, stageApp } from "./stage-app.mjs";
import { STAMP_FILE, writeBuildStamp } from "./write-build-stamp.mjs";

const require = createRequire(import.meta.url);

/** Variables that would let electron-builder sign, notarise or publish. Removed, never read. */
export const SIGNING_ENV = Object.freeze([
  "CSC_LINK",
  "CSC_KEY_PASSWORD",
  "CSC_NAME",
  "CSC_KEYCHAIN",
  "WIN_CSC_LINK",
  "WIN_CSC_KEY_PASSWORD",
  "APPLE_ID",
  "APPLE_APP_SPECIFIC_PASSWORD",
  "APPLE_TEAM_ID",
  "APPLE_API_KEY",
  "APPLE_API_KEY_ID",
  "APPLE_API_ISSUER",
  "APPLE_KEYCHAIN",
  "APPLE_KEYCHAIN_PROFILE",
  "GH_TOKEN",
  "GITHUB_TOKEN",
  "EP_GH_IGNORE_TIME",
]);
const SIGNING_PREFIXES = ["AZURE_", "CSC_", "WIN_CSC_", "APPLE_"];

/** A copy of `env` without signing / publishing variables, with certificate discovery off. */
export function unsignedEnv(env) {
  const out = {};
  for (const [key, value] of Object.entries(env)) {
    if (SIGNING_ENV.includes(key)) continue;
    if (SIGNING_PREFIXES.some((prefix) => key.startsWith(prefix))) continue;
    out[key] = value;
  }
  out.CSC_IDENTITY_AUTO_DISCOVERY = "false";
  return out;
}

/** electron-builder targets for the host platform (`--dir` → unpacked only). */
export function targetsFor(platform, { dir = false } = {}) {
  if (platform === "darwin") return { mac: dir ? ["dir"] : ["dmg", "zip"] };
  if (platform === "win32") return { win: dir ? ["dir"] : ["nsis", "zip"] };
  if (platform === "linux") return { linux: dir ? ["dir"] : ["AppImage"] };
  throw new Error(`no desktop build for platform ${platform}`);
}

export function parseFlags(argv) {
  const flags = { dir: false, skipBuild: false, channel: undefined };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--dir") flags.dir = true;
    else if (arg === "--skip-build") flags.skipBuild = true;
    else if (arg === "--channel") {
      flags.channel = argv[i + 1];
      i += 1;
      if (flags.channel === undefined) throw new Error("--channel needs a value");
    } else throw new Error(`unknown argument ${arg}`);
  }
  return flags;
}

async function buildBundles() {
  const { build } = await import("vite");
  await build({
    root: APP_DIR,
    configFile: path.join(APP_DIR, "vite.config.ts"),
    logLevel: "warn",
  });
  const { buildElectron } = await import("./build-electron.mjs");
  await buildElectron();
}

export async function distUnsigned(flags) {
  if (!flags.skipBuild) await buildBundles();
  const stamp = writeBuildStamp(path.join(APP_DIR, "build", STAMP_FILE), {
    ...(flags.channel === undefined ? {} : { channel: flags.channel }),
  });
  const staged = stageApp({ stamp });
  process.stdout.write(
    `[stage] ${staged.outDir} (${stamp.version}, ${stamp.channel}, owner ${stamp.updateOwner}` +
      `${staged.natives.length > 0 ? `, native: ${staged.natives.map((n) => n.name).join(", ")}` : ""})\n`,
  );

  fs.rmSync(path.join(APP_DIR, "build", "release"), { recursive: true, force: true });
  const clean = unsignedEnv(process.env);
  for (const key of Object.keys(process.env)) if (!(key in clean)) delete process.env[key];
  process.env.CSC_IDENTITY_AUTO_DISCOVERY = "false";

  const builder = require("electron-builder");
  const config = require(path.join(APP_DIR, "electron-builder.config.cjs"));
  const artefacts = await builder.build({
    projectDir: APP_DIR,
    config,
    publish: "never",
    ...targetsFor(process.platform, { dir: flags.dir }),
  });
  for (const file of artefacts) process.stdout.write(`[artefact] ${file}\n`);
  return artefacts;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await distUnsigned(parseFlags(process.argv.slice(2)));
  } catch (error) {
    process.stderr.write(
      `${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`,
    );
    process.exit(1);
  }
}
