// Fixed file locations the main process uses. The Indus home is `INDUS_HOME` when it is set to an
// absolute path, else `~/.indus`; userData is Electron's (or `INDUS_DESKTOP_USER_DATA_DIR`).

import path from "node:path";

export function resolveIndusHome(
  env: Readonly<Record<string, string | undefined>>,
  homeDir: string,
): string {
  const explicit = (env.INDUS_HOME ?? "").trim();
  if (explicit !== "" && path.isAbsolute(explicit)) return path.resolve(explicit);
  return path.join(homeDir, ".indus");
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
