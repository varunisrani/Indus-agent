// Fixed file locations the main process uses. The Indus home is the one the `indus` CLI picks
// (`indus.foundation.home`): `INDUS_HOME` when it is set to an absolute path, else `~/.indus`, or
// `%LOCALAPPDATA%\indus` on Windows (the installer's and the CLI's default there), with
// `INDUS_DATA_DIR_SUFFIX` appended to the folder name on Windows as the CLI does. Builds before
// that used `~/.indus` on Windows too: `legacyWindowsHome` names that folder so main can say where
// earlier settings are. userData is Electron's (or `INDUS_DESKTOP_USER_DATA_DIR`).

import fs from "node:fs";
import path from "node:path";

/** The CLI's `INDUS_DATA_DIR_SUFFIX`, or "" when unset or not a plain name. */
function dataDirSuffix(env: Readonly<Record<string, string | undefined>>): string {
  const suffix = (env.INDUS_DATA_DIR_SUFFIX ?? "").trim();
  if (suffix.includes("/") || suffix.includes("\\") || suffix === "." || suffix === "..") return "";
  return suffix;
}

export function resolveIndusHome(
  env: Readonly<Record<string, string | undefined>>,
  homeDir: string,
  platform: string = process.platform,
): string {
  const explicit = (env.INDUS_HOME ?? "").trim();
  if (explicit !== "" && path.isAbsolute(explicit)) return path.resolve(explicit);
  if (platform === "win32") {
    const local = (env.LOCALAPPDATA ?? "").trim();
    const base = local !== "" ? local : path.win32.join(homeDir, "AppData", "Local");
    return path.win32.join(base, `indus${dataDirSuffix(env)}`);
  }
  return path.join(homeDir, ".indus");
}

/**
 * Windows only: the `~/.indus` an earlier build used, when it still holds settings and the home
 * resolved now has none (so its owner can be told to move it); null otherwise.
 */
export function legacyWindowsHome(
  home: string,
  env: Readonly<Record<string, string | undefined>>,
  homeDir: string,
  platform: string = process.platform,
  exists: (file: string) => boolean = fs.existsSync,
): string | null {
  if (platform !== "win32" || (env.INDUS_HOME ?? "").trim() !== "") return null;
  const legacy = path.win32.join(homeDir, ".indus");
  if (path.win32.resolve(legacy).toLowerCase() === path.win32.resolve(home).toLowerCase()) {
    return null;
  }
  const settled = (dir: string) =>
    exists(path.win32.join(dir, "config.yaml")) || exists(path.win32.join(dir, "state.db"));
  return settled(legacy) && !settled(home) ? legacy : null;
}

/** A userData override (absolute paths only), or null to keep Electron's default. */
export function userDataOverride(env: Readonly<Record<string, string | undefined>>): string | null {
  const explicit = (env.INDUS_DESKTOP_USER_DATA_DIR ?? "").trim();
  return explicit !== "" && path.isAbsolute(explicit) ? path.resolve(explicit) : null;
}

export interface DesktopPaths {
  home: string;
  userData: string;
  desktopLog: string;
  homeConfig: string;
  windowState: string;
  zoomState: string;
  secretsDir: string;
  secretsPolicy: string;
}

export function desktopPaths(home: string, userData: string): DesktopPaths {
  return {
    home,
    userData,
    desktopLog: path.join(home, "logs", "desktop.log"),
    homeConfig: path.join(home, "config.yaml"),
    windowState: path.join(userData, "window-state.json"),
    zoomState: path.join(userData, "zoom-state.json"),
    secretsDir: path.join(userData, "secrets"),
    secretsPolicy: path.join(userData, "secrets-policy.json"),
  };
}
