// Linux launcher entry for the AppImage build (P20-38). An AppImage installs nothing, so the app
// adds itself to the desktop's application menu: once the main window is first visible it writes
// `<data home>/applications/ai.indus.desktop.desktop` (Exec = the AppImage file, with `%U` so
// `indus://` links reach it) and copies its icon into the hicolor theme. Only for a packaged
// AppImage, only when the content changed, never fatal.

import fs from "node:fs";
import path from "node:path";

export const LAUNCHER_ID = "ai.indus.desktop";
export const LAUNCHER_ICON_SIZE = 256;

export interface LauncherInput {
  /** The AppImage file (`APPIMAGE`); quoted for the Exec line. */
  execPath: string;
  /** The deep-link scheme the app registered (`indus`, or `indus-dev` in dev). */
  scheme: string;
  name?: string;
}

/** Quote one argument for a desktop entry Exec line (spec: double quotes, escape `"`, `` ` ``, `$`, `\`). */
export function quoteExecArg(arg: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(arg)) return arg.replaceAll("%", "%%");
  return `"${arg.replace(/(["`$\\])/g, "\\$1").replaceAll("%", "%%")}"`;
}

/** The desktop entry text. */
export function desktopEntry(input: LauncherInput): string {
  const name = input.name ?? "Indus";
  return [
    "[Desktop Entry]",
    "Type=Application",
    `Name=${name}`,
    "Comment=Desktop app for Indus Agent",
    `Exec=${quoteExecArg(input.execPath)} %U`,
    `Icon=${LAUNCHER_ID}`,
    "Terminal=false",
    "Categories=Development;",
    `MimeType=x-scheme-handler/${input.scheme};`,
    `StartupWMClass=${name}`,
    "X-Indus-Managed=true",
    "",
  ].join("\n");
}

export interface LauncherFs {
  readFile(file: string): string | null;
  writeFileAtomic(file: string, data: string | Buffer): void;
  copyFile(from: string, to: string): void;
  exists(file: string): boolean;
}

export const nodeLauncherFs: LauncherFs = {
  readFile: (file) => {
    try {
      return fs.readFileSync(file, "utf8");
    } catch {
      return null;
    }
  },
  writeFileAtomic: (file, data) => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const temp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(temp, data, { mode: 0o644 });
    fs.renameSync(temp, file);
  },
  copyFile: (from, to) => {
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.copyFileSync(from, to);
  },
  exists: (file) => fs.existsSync(file),
};

export interface LauncherDeps {
  platform: string;
  packaged: boolean;
  env: Readonly<Record<string, string | undefined>>;
  homeDir: string;
  /** `process.resourcesPath` (the icon ships as `icons/indus.png` beside the archive). */
  resourcesPath: string;
  scheme: string;
  log: (line: string) => void;
  fs?: LauncherFs;
}

export type LauncherOutcome =
  | { written: true; file: string }
  | {
      written: false;
      reason: "not-linux" | "not-packaged" | "not-appimage" | "unchanged" | "failed";
    };

/** Write (or refresh) the launcher entry now. */
export function writeLinuxLauncher(deps: LauncherDeps): LauncherOutcome {
  if (deps.platform !== "linux") return { written: false, reason: "not-linux" };
  if (!deps.packaged) return { written: false, reason: "not-packaged" };
  const appImage = (deps.env.APPIMAGE ?? "").trim();
  if (appImage === "" || !path.isAbsolute(appImage))
    return { written: false, reason: "not-appimage" };
  const files = deps.fs ?? nodeLauncherFs;
  const dataHome =
    deps.env.XDG_DATA_HOME !== undefined && path.isAbsolute(deps.env.XDG_DATA_HOME)
      ? deps.env.XDG_DATA_HOME
      : path.join(deps.homeDir, ".local", "share");
  const file = path.join(dataHome, "applications", `${LAUNCHER_ID}.desktop`);
  const icon = path.join(
    dataHome,
    "icons",
    "hicolor",
    `${LAUNCHER_ICON_SIZE}x${LAUNCHER_ICON_SIZE}`,
    "apps",
    `${LAUNCHER_ID}.png`,
  );
  const text = desktopEntry({ execPath: appImage, scheme: deps.scheme });
  try {
    if (!files.exists(icon))
      files.copyFile(path.join(deps.resourcesPath, "icons", "indus.png"), icon);
    if (files.readFile(file) === text) return { written: false, reason: "unchanged" };
    files.writeFileAtomic(file, text);
    deps.log(`[launcher] desktop entry written (${file})`);
    return { written: true, file };
  } catch (error) {
    deps.log(
      `[launcher] desktop entry not written: ${error instanceof Error ? error.message : String(error)}`,
    );
    return { written: false, reason: "failed" };
  }
}

export interface ShownWindow {
  isVisible(): boolean;
  on(event: string, listener: (...args: never[]) => void): unknown;
}

/** Write the launcher entry after `win` is first visible (now, when it already is). */
export function installLinuxLauncher(win: ShownWindow, deps: LauncherDeps): void {
  if (deps.platform !== "linux" || !deps.packaged) return;
  let done = false;
  const run = () => {
    if (done) return;
    done = true;
    writeLinuxLauncher(deps);
  };
  if (win.isVisible()) run();
  else win.on("show", run);
}
