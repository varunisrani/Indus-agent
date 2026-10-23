// The Electron fuses every packaged build carries (D-16.19), shared by the afterPack hook that
// flips them and the verifier that reads them back. Changing a value here needs a D-id.

import fs from "node:fs";
import path from "node:path";
import {
  FuseState,
  FuseV1Options,
  FuseVersion,
  flipFuses,
  getCurrentFuseWire,
} from "@electron/fuses";
import identity from "./channel-identity.cjs";

/** Every channel's packaged names (P23-16: a canary has its own product and Linux executable). */
const CHANNEL_NAMES = identity.CHANNELS.map((channel) => identity.channelIdentity(channel));

/** Fuse name → wanted state (true = enabled). Exactly these six. */
export const WANTED_FUSES = Object.freeze({
  RunAsNode: false,
  EnableNodeOptionsEnvironmentVariable: false,
  EnableNodeCliInspectArguments: false,
  EnableEmbeddedAsarIntegrityValidation: true,
  OnlyLoadAppFromAsar: true,
  EnableCookieEncryption: true,
});

/** The config object `flipFuses` takes. */
export function fuseConfig({ resetAdHocDarwinSignature = false } = {}) {
  const config = { version: FuseVersion.V1, resetAdHocDarwinSignature };
  for (const [name, wanted] of Object.entries(WANTED_FUSES)) {
    const index = FuseV1Options[name];
    if (typeof index !== "number") throw new Error(`unknown fuse ${name}`);
    config[index] = wanted;
  }
  return config;
}

/** Flip the wanted fuses on one Electron binary (or `.app` bundle). */
export async function applyFuses(binary, options = {}) {
  await flipFuses(binary, fuseConfig(options));
}

function stateName(value) {
  if (value === FuseState.ENABLE) return "on";
  if (value === FuseState.DISABLE) return "off";
  if (value === FuseState.REMOVED) return "removed";
  return "unset";
}

/** `{ name: "on" | "off" | "removed" | "unset" | "missing" }` for the six wanted fuses. */
export async function readFuses(binary) {
  const wire = await getCurrentFuseWire(binary);
  const out = {};
  for (const name of Object.keys(WANTED_FUSES)) {
    const index = FuseV1Options[name];
    out[name] = index in wire ? stateName(wire[index]) : "missing";
  }
  return out;
}

/** Names whose state differs from the wanted one. */
export function fuseMismatches(states) {
  return Object.entries(WANTED_FUSES)
    .filter(([name, wanted]) => states[name] !== (wanted ? "on" : "off"))
    .map(([name]) => name);
}

function isExecutableFile(file) {
  try {
    const info = fs.statSync(file);
    return info.isFile() && (info.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

/**
 * The packaged Electron binaries under an electron-builder output folder: `*.app` bundles
 * (macOS), `win*-unpacked/<name>.exe` (Windows) and `linux*-unpacked/<executable>` (Linux).
 */
export function findPackagedBinaries(distDir, { productName = undefined } = {}) {
  const products =
    productName === undefined ? CHANNEL_NAMES.map((id) => id.productName) : [productName];
  const executables = CHANNEL_NAMES.map((id) => id.executableName);
  const found = [];
  const walk = (dir, depth) => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory() && entry.name.endsWith(".app")) {
        found.push(full);
      } else if (entry.isDirectory() && /^win.*-unpacked$/.test(entry.name)) {
        const exe = products.map((n) => path.join(full, `${n}.exe`)).find((f) => fs.existsSync(f));
        if (exe !== undefined) found.push(exe);
      } else if (entry.isDirectory() && /^linux.*-unpacked$/.test(entry.name)) {
        const names = [...products, ...products.map((n) => n.toLowerCase()), ...executables];
        const candidates = names.map((n) => path.join(full, n));
        const bin = candidates.find(isExecutableFile);
        if (bin !== undefined) found.push(bin);
      } else if (entry.isDirectory() && depth < 3) {
        walk(full, depth + 1);
      }
    }
  };
  walk(distDir, 0);
  return found;
}
