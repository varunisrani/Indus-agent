// The installing half of the electron-updater owner (P20-37): macOS bundles and the Linux AppImage.
// It never downloads on a check; `download` reports progress and resolves only once the updater
// accepted the update (on macOS, Squirrel accepting the signed app — bounded at 120 s after the
// download); `install` quits and installs. The module is loaded lazily, only in a packaged build
// whose stamp names this owner.

import type { Timers } from "../ambient/hud.ts";
import type { UpdateChannel } from "./feed.ts";

export const ACCEPT_TIMEOUT_MS = 120_000;

/** The slice of electron-updater's `AppUpdater` we use. */
export interface NativeUpdaterLike {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  allowPrerelease: boolean;
  allowDowngrade: boolean;
  channel: string | null;
  checkForUpdates(): Promise<{
    updateInfo: { version: string };
    isUpdateAvailable?: boolean;
  } | null>;
  downloadUpdate(): Promise<unknown>;
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void;
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  removeListener(event: string, listener: (...args: unknown[]) => void): unknown;
}

export interface UpdateEngine {
  check(channel: UpdateChannel): Promise<{ available: boolean; version: string | null }>;
  /** Download and wait for acceptance; rejects on any failure (nothing has been stopped). */
  download(onProgress: (percent: number) => void): Promise<void>;
  install(): void;
}

export class EngineError extends Error {}

export class ElectronUpdaterEngine implements UpdateEngine {
  private readonly updater: NativeUpdaterLike;
  private readonly timers: Timers;
  private readonly acceptTimeoutMs: number;

  constructor(updater: NativeUpdaterLike, timers: Timers, acceptTimeoutMs = ACCEPT_TIMEOUT_MS) {
    this.updater = updater;
    this.timers = timers;
    this.acceptTimeoutMs = acceptTimeoutMs;
    // A check never downloads, and an unrelated quit never installs.
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
  }

  async check(channel: UpdateChannel): Promise<{ available: boolean; version: string | null }> {
    this.updater.channel = channel === "canary" ? "canary" : "latest";
    this.updater.allowPrerelease = channel === "canary";
    // Setting a channel lets electron-updater go backwards; we never do without an explicit choice.
    this.updater.allowDowngrade = false;
    const result = await this.updater.checkForUpdates();
    if (result === null) throw new EngineError("the updater is not active for this build");
    return {
      available: result.isUpdateAvailable !== false,
      version: result.updateInfo?.version ?? null,
    };
  }

  download(onProgress: (percent: number) => void): Promise<void> {
    const updater = this.updater;
    return new Promise<void>((resolve, reject) => {
      let accepted = false;
      let downloaded = false;
      let settled = false;
      let timer: unknown = null;
      const progress = ((info: { percent?: number }) => {
        if (typeof info?.percent === "number") onProgress(Math.max(0, Math.min(100, info.percent)));
      }) as (...args: unknown[]) => void;
      const finish = (error: Error | null) => {
        if (settled) return;
        settled = true;
        if (timer !== null) this.timers.clearTimeout(timer);
        updater.removeListener("download-progress", progress);
        updater.removeListener("update-downloaded", onAccepted);
        updater.removeListener("error", onError);
        if (error === null) resolve();
        else reject(error);
      };
      const onAccepted = (() => {
        accepted = true;
        if (downloaded) finish(null);
      }) as (...args: unknown[]) => void;
      const onError = ((error: unknown) =>
        finish(
          new EngineError(
            error instanceof Error ? `the update failed: ${error.message}` : "the update failed",
          ),
        )) as (...args: unknown[]) => void;
      updater.on("download-progress", progress);
      updater.on("update-downloaded", onAccepted);
      updater.on("error", onError);
      updater.downloadUpdate().then(
        () => {
          downloaded = true;
          if (accepted) {
            finish(null);
            return;
          }
          timer = this.timers.setTimeout(
            () => finish(new EngineError("the update was not accepted in time")),
            this.acceptTimeoutMs,
          );
        },
        (error: unknown) =>
          finish(
            new EngineError(
              error instanceof Error
                ? `the download failed: ${error.message}`
                : "the download failed",
            ),
          ),
      );
    });
  }

  install(): void {
    this.updater.quitAndInstall(false, true);
  }
}

/** The package name, kept out of the bundle: electron-updater ships in the app's node_modules. */
const UPDATER_PACKAGE = "electron-updater";

/** electron-updater's shared updater, loaded only when this build's stamp names it. */
export async function loadElectronUpdater(): Promise<NativeUpdaterLike> {
  const module = (await import(/* @vite-ignore */ UPDATER_PACKAGE)) as {
    autoUpdater?: NativeUpdaterLike;
    default?: { autoUpdater?: NativeUpdaterLike };
  };
  const updater = module.autoUpdater ?? module.default?.autoUpdater;
  if (updater === undefined) throw new EngineError("electron-updater is not available");
  return updater;
}
