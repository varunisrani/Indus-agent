// Build stamp and generation skew (P20-37, R08.14).
//
// The build stamp (`desktop-build-stamp.json`, written by P20-38) is the one place that names the
// update owner, the channel the build came from, its version and commit. Two checks use it:
//
// - mixed generations: the copy inside `app.asar` and the one in `app.asar.unpacked` disagree (a
//   broken or half-applied update replaced one but not the other) → offer reinstall instead of
//   crashing later on a native module or a lazy chunk from the other generation;
// - app behind runtime: the runtime the app attached to is newer than the app (a backend update
//   ran) → offer the app update.
//
// The renderer's own check (a lazy chunk that 404s) lives in `src/updates/skew.ts`.

import path from "node:path";
import { readJsonFile } from "../fsSecure.ts";
import type { UpdateChannel } from "./feed.ts";
import { isNewer, parseVersion } from "./version.ts";

export const BUILD_STAMP_FILE = "desktop-build-stamp.json";

export type UpdateOwner = "electron-updater" | "manual";

export interface BuildStamp {
  version: string;
  commit: string | null;
  channel: UpdateChannel;
  updateOwner: UpdateOwner;
}

export interface SkewNotice {
  kind: "mixed-generations" | "app-behind-runtime" | "renderer-stale";
  detail: string;
}

/** A usable stamp, or null (missing fields, unknown owner, not a version). */
export function sanitizeStamp(raw: unknown): BuildStamp | null {
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const version = typeof record.version === "string" ? record.version.trim() : "";
  if (parseVersion(version) === null) return null;
  const owner = record.updateOwner ?? record.update_owner;
  if (owner !== "electron-updater" && owner !== "manual") return null;
  const channel = record.channel === "canary" ? "canary" : "stable";
  const commit =
    typeof record.commit === "string" && /^[0-9a-f]{7,64}$/i.test(record.commit)
      ? record.commit
      : null;
  return { version, commit, channel, updateOwner: owner };
}

export function readStamp(file: string): BuildStamp | null {
  return sanitizeStamp(readJsonFile(file));
}

/** Where the stamps live for an app path (`…/app.asar` → its `.unpacked` sibling too). */
export function stampLocations(appPath: string): { inside: string; unpacked: string | null } {
  const inside = path.join(appPath, BUILD_STAMP_FILE);
  return {
    inside,
    unpacked: appPath.endsWith(".asar") ? path.join(`${appPath}.unpacked`, BUILD_STAMP_FILE) : null,
  };
}

/** Both copies exist and name different builds. */
export function mixedGenerations(
  inside: BuildStamp | null,
  unpacked: BuildStamp | null,
): SkewNotice | null {
  if (inside === null || unpacked === null) return null;
  const same =
    inside.version === unpacked.version &&
    (inside.commit === null || unpacked.commit === null || inside.commit === unpacked.commit);
  if (same) return null;
  return {
    kind: "mixed-generations",
    detail: `This app has files from two builds (${inside.version} and ${unpacked.version}). Reinstall or update Indus.`,
  };
}

/** The runtime is newer than this (packaged) app. */
export function appBehindRuntime(appVersion: string, runtimeVersion: string): SkewNotice | null {
  if (!isNewer(runtimeVersion, appVersion)) return null;
  return {
    kind: "app-behind-runtime",
    detail: `Indus ${runtimeVersion} is running, but this app is ${appVersion}. Update the app to match.`,
  };
}
