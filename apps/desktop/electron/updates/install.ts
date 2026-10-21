// Main's update side (P20-37), in two steps because the boot needs the gate before the backend
// is installed and the updater needs the backend's installer:
//
//   const updateGate = createUpdateGate(context);                    // before installBackend
//   installBackend(context, { ..., updateGate: gateHooks(updateGate) });
//   installUpdates(context, electron, { gate: updateGate, stopAppWork }); // after it
//
// Background checks run only in a packaged build or against an explicit feed
// (`INDUS_DESKTOP_UPDATE_FEED`), and never with `INDUS_DESKTOP_NO_UPDATE_CHECK=1`.

import path from "node:path";
import type { MainContext } from "../context.ts";
import type { EventChannel } from "../ipc/channels.ts";
import type { EventTarget } from "../ipc/registry.ts";
import type { PayloadOf } from "../ipc/schemas.ts";
import { ElectronUpdaterEngine, loadElectronUpdater, type UpdateEngine } from "./engine.ts";
import { DEFAULT_RELEASES_API, ghCliToken, ReleaseFeed } from "./feed.ts";
import { nodeProcessFacts, UpdateGate } from "./marker.ts";
import {
  appBehindRuntime,
  mixedGenerations,
  readStamp,
  type SkewNotice,
  stampLocations,
} from "./skew.ts";
import { Updater } from "./updater.ts";

const timers = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function createUpdateGate(context: MainContext): UpdateGate {
  const gate = new UpdateGate({
    home: context.paths.home,
    facts: nodeProcessFacts(context.platform),
    timers,
    now: Date.now,
    log: (line) => context.log.write(line),
  });
  gate.watch();
  return gate;
}

/** The two hooks P20-12's boot orchestrator takes (`waitForUpdate`, `updateRunning`). */
export function gateHooks(gate: UpdateGate): {
  waitForUpdate(): Promise<void>;
  updateRunning(): boolean;
} {
  return {
    waitForUpdate: async () => {
      await gate.waitUntilClear();
    },
    updateRunning: () => gate.isRunning(),
  };
}

export interface UpdatesElectron {
  app: { getAppPath(): string; on(event: "will-quit", listener: () => void): unknown };
  fetch: typeof fetch;
  /** The runtime version the app is attached to now (the daemon's `code_version`), or null. */
  runtimeVersion(): string | null;
}

export interface UpdatesHandle {
  gate: UpdateGate;
  updater: Updater;
}

export function installUpdates(
  context: MainContext,
  electron: UpdatesElectron,
  deps: { gate: UpdateGate; stopAppWork(): Promise<void> },
): UpdatesHandle {
  const { registry, windows, paths, log } = context;
  const line = (text: string) => log.write(text);
  const broadcast = <N extends EventChannel>(name: N, payload: PayloadOf<N>) => {
    for (const win of windows.windows())
      registry.emit(win.webContents as EventTarget, name, payload);
  };

  const locations = stampLocations(electron.app.getAppPath());
  const stamp = readStamp(locations.inside);
  const skews: SkewNotice[] = [];
  const mixed = mixedGenerations(
    stamp,
    locations.unpacked === null ? null : readStamp(locations.unpacked),
  );
  if (mixed !== null) {
    skews.push(mixed);
    line("[updates] this app holds files from two builds; offering a reinstall");
  }

  const feedUrl = (process.env.INDUS_DESKTOP_UPDATE_FEED ?? "").trim();
  const feed = new ReleaseFeed({
    apiUrl: feedUrl === "" ? DEFAULT_RELEASES_API : feedUrl,
    env: process.env,
    cliToken: () => ghCliToken(),
    fetch: (url, init) => electron.fetch(url, init),
    timers,
    log: line,
  });

  let engine: UpdateEngine | null = null;
  const updater = new Updater({
    stamp,
    appVersion: context.appVersion,
    packaged: context.packaged,
    platform: context.platform,
    env: process.env,
    feed,
    engine: async () => {
      if (engine === null) engine = new ElectronUpdaterEngine(await loadElectronUpdater(), timers);
      return engine;
    },
    gate: {
      setInFlight: (on) => deps.gate.setInFlight(on),
      inFlightNow: () => deps.gate.inFlightNow(),
      liveMarker: async () => (await deps.gate.refresh()).state === "live",
    },
    stopAppWork: deps.stopAppWork,
    channelFile: path.join(paths.userData, "updates.json"),
    emit: (status) => broadcast("indus:updates:progress", status),
    timers,
    now: Date.now,
    log: line,
  });

  registry.handle("indus:updates:status", () => updater.status());
  registry.handle("indus:updates:check", () => updater.check());
  registry.handle("indus:updates:apply", async () => ({
    accepted: (await updater.apply()).accepted,
  }));
  registry.handle("indus:updates:set-channel", ({ channel }) => updater.setChannel(channel));

  // A window that loads (or reloads) hears every skew main knows about.
  windows.onWindowCreated((win) => {
    win.webContents.on("did-finish-load", () => {
      for (const skew of skews)
        registry.emit(win.webContents as EventTarget, "indus:updates:skew", skew);
    });
  });

  // A runtime update just finished: is the app now behind it?
  deps.gate.onCleared(() => {
    const runtime = electron.runtimeVersion();
    if (context.packaged && runtime !== null) {
      const behind = appBehindRuntime(context.appVersion, runtime);
      if (behind !== null && !skews.some((s) => s.kind === behind.kind)) {
        skews.push(behind);
        broadcast("indus:updates:skew", behind);
      }
    }
    void updater.check();
  });

  const backgroundAllowed =
    process.env.INDUS_DESKTOP_NO_UPDATE_CHECK !== "1" && (context.packaged || feedUrl !== "");
  if (backgroundAllowed) updater.startBackgroundChecks();
  electron.app.on("will-quit", () => {
    updater.stop();
    deps.gate.stop();
  });

  return { gate: deps.gate, updater };
}
