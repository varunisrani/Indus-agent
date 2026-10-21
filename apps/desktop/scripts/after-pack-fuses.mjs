// electron-builder `afterPack` hook (P20-38 wires it into electron-builder.config.cjs): flip the
// Electron fuses on the packaged binary before anything signs it (D-16.19). A failure fails the
// build: an unfused app must never ship.

import path from "node:path";
import { applyFuses } from "./fuses.mjs";

/** electron-builder's `Arch.arm64`. */
const ARCH_ARM64 = 3;

/** The Electron binary inside one packed output folder. */
export function packedBinary(context) {
  const { appOutDir, electronPlatformName, packager } = context;
  const product = packager.appInfo.productFilename;
  if (electronPlatformName === "darwin" || electronPlatformName === "mas") {
    return path.join(appOutDir, `${product}.app`);
  }
  if (electronPlatformName === "win32") return path.join(appOutDir, `${product}.exe`);
  return path.join(appOutDir, packager.executableName ?? product);
}

export default async function afterPackFuses(context) {
  const binary = packedBinary(context);
  // Flipping fuses rewrites the binary, which breaks the ad-hoc signature an arm64 macOS build
  // needs to launch at all; unsigned 1.0 builds re-sign ad hoc (real signing is P23).
  const resetAdHocDarwinSignature =
    context.electronPlatformName === "darwin" && context.arch === ARCH_ARM64;
  await applyFuses(binary, { resetAdHocDarwinSignature });
  process.stdout.write(`[fuses] set on ${binary}\n`);
}
