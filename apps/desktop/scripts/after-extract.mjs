// electron-builder `afterExtract` hook (P23-15): stamp the build's identity onto the freshly
// extracted Electron binary, per platform, before electron-builder copies the app in, computes the
// ASAR integrity and rewrites the bundle's metadata. Anything stamped later would either be
// rejected by the integrity check or fall outside what the signature seals.
//
//   darwin  `IndusBuildVersion`, `IndusBuildChannel`, `IndusBuildCommit` in the pristine
//           `Electron.app/Contents/Info.plist` (electron-builder keeps keys it does not set)
//   win32   the executable's version resource and icon in the pristine `electron.exe`, with the
//           same three build values (stampWindowsExecutable in sign-win.mjs, P23-16)
//
// The values come from the build stamp (`build/desktop-build-stamp.json`, P20-38), written before
// electron-builder starts; a build without a valid stamp fails here.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTopLevelString } from "./plist-text.mjs";
import { stampWindowsExecutable } from "./sign-win.mjs";
import { STAMP_FILE, validateBuildStamp } from "./write-build-stamp.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const DEFAULT_STAMP = path.join(APP_DIR, "build", STAMP_FILE);

/** Info.plist key → build stamp field. */
export const MAC_STAMP_KEYS = Object.freeze({
  IndusBuildVersion: "version",
  IndusBuildChannel: "channel",
  IndusBuildCommit: "commit",
});

/** The extracted, not yet renamed Electron bundle (`Electron.app` unless the framework says). */
export function pristineMacBundle(context) {
  const name = context.packager?.info?.framework?.distMacOsAppName ?? "Electron.app";
  return path.join(context.appOutDir, name);
}

/** Write the stamp keys into `bundle`'s Info.plist; returns the keys written. */
export function stampMacBundle(bundle, stamp) {
  const plistFile = path.join(bundle, "Contents", "Info.plist");
  let text = fs.readFileSync(plistFile, "utf8");
  for (const [key, field] of Object.entries(MAC_STAMP_KEYS)) {
    text = setTopLevelString(text, key, stamp[field]);
  }
  fs.writeFileSync(plistFile, text);
  return Object.keys(MAC_STAMP_KEYS);
}

/** Platform → stamper `(context, stamp) => void`. */
export const STAMPERS = Object.freeze({
  darwin: (context, stamp) => {
    const bundle = pristineMacBundle(context);
    const keys = stampMacBundle(bundle, stamp);
    process.stdout.write(
      `[after-extract] stamped ${keys.length} build keys into ${path.basename(bundle)}\n`,
    );
  },
  win32: async (context, stamp) => {
    await stampWindowsExecutable(context, stamp);
  },
});

export default async function afterExtract(context, { stampFile = DEFAULT_STAMP } = {}) {
  const stamper = STAMPERS[context.electronPlatformName];
  if (stamper === undefined) return;
  const stamp = validateBuildStamp(JSON.parse(fs.readFileSync(stampFile, "utf8")));
  await stamper(context, stamp);
}
