import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { FakeFeed } from "../../testing/fakeFeed.ts";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { ElectronUpdaterEngine, type NativeUpdaterLike, type UpdateEngine } from "./engine.ts";
import { ReleaseFeed, type ReleaseInfo, type UpdateChannel } from "./feed.ts";
import type { BuildStamp } from "./skew.ts";
import {
  FIRST_CHECK_DELAY_MS,
  MANUAL_COMMAND,
  Updater,
  type UpdateStatus,
  updateOwner,
} from "./updater.ts";

const MAC_STAMP: BuildStamp = {
  version: "1.0.0",
  commit: "abcdef1",
  channel: "stable",
  updateOwner: "electron-updater",
};

class SpyEngine implements UpdateEngine {
  readonly calls: string[] = [];
  available = true;
  failDownload: Error | null = null;
  check(channel: UpdateChannel) {
    this.calls.push(`check:${channel}`);
    return Promise.resolve({ available: this.available, version: "1.1.0" });
  }
  async download(onProgress: (percent: number) => void) {
    this.calls.push("download");
    onProgress(50);
    if (this.failDownload !== null) throw this.failDownload;
    onProgress(100);
    this.calls.push("accepted");
  }
  install() {
    this.calls.push("install");
  }
}

function setup(options: {
  stamp?: BuildStamp | null;
  release?: ReleaseInfo | null;
  feedError?: Error;
  liveMarker?: boolean;
  packaged?: boolean;
  platform?: string;
  env?: Record<string, string>;
}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "upd-"));
  const engine = new SpyEngine();
  const events: UpdateStatus[] = [];
  const order: string[] = [];
  const gateState = { inFlight: false, live: options.liveMarker ?? false };
  const feedCalls: UpdateChannel[] = [];
  const updater = new Updater({
    stamp: options.stamp === undefined ? MAC_STAMP : options.stamp,
    appVersion: "1.0.0",
    packaged: options.packaged ?? true,
    platform: options.platform ?? "darwin",
    env: options.env ?? {},
    feed: {
      latest: async (channel) => {
        feedCalls.push(channel);
        if (options.feedError !== undefined) throw options.feedError;
        return options.release === undefined
          ? { version: "1.1.0", tag: "v1.1.0", url: "https://example.invalid/r", canary: false }
          : options.release;
      },
    },
    engine: async () => {
      order.push("engine");
      return engine;
    },
    gate: {
      setInFlight: (on) => {
        gateState.inFlight = on;
        order.push(`inFlight:${on}`);
      },
      inFlightNow: () => gateState.inFlight,
      liveMarker: async () => gateState.live,
    },
    stopAppWork: async () => {
      order.push("stop-app-work");
      engine.calls.push("stop-app-work");
    },
    channelFile: path.join(dir, "updates.json"),
    emit: (status) => events.push(status),
    timers: new FakeTimers(),
    now: () => 1234,
    log: () => {},
  });
  return { updater, engine, events, order, gateState, feedCalls, dir };
}

describe("update owner", () => {
  it("comes from the stamp only; no stamp means manual", () => {
    expect(updateOwner(MAC_STAMP)).toBe("electron-updater");
    expect(updateOwner({ ...MAC_STAMP, updateOwner: "manual" })).toBe("manual");
    expect(updateOwner(null)).toBe("manual");
  });
});

describe("updater", () => {
  it("a check never downloads", async () => {
    const { updater, engine } = setup({});
    const status = await updater.check();
    expect(status).toMatchObject({ state: "available", version: "1.1.0", checkedAt: 1234 });
    expect(engine.calls).toEqual([]);
  });

  it("nothing newer → idle; a failing feed → error, no crash", async () => {
    expect(
      (
        await setup({
          release: { version: "1.0.0", tag: "v1.0.0", url: null, canary: false },
        }).updater.check()
      ).state,
    ).toBe("idle");
    expect(
      await setup({ feedError: new Error("the releases feed answered 503") }).updater.check(),
    ).toMatchObject({ state: "error", error: "the releases feed answered 503" });
  });

  it("manual owner: the command for a checkout, the release page for a packaged build; apply refused", async () => {
    const checkout = setup({ stamp: null, packaged: false });
    expect(await checkout.updater.check()).toMatchObject({
      owner: "manual",
      state: "manual",
      manualHint: MANUAL_COMMAND,
    });
    const windows = setup({ stamp: { ...MAC_STAMP, updateOwner: "manual" }, platform: "win32" });
    expect(await windows.updater.check()).toEqual(
      expect.objectContaining({ state: "manual", downloadUrl: "https://example.invalid/r" }),
    );
    expect((await windows.updater.check()).manualHint).toBeUndefined();
    expect((await windows.updater.apply()).accepted).toBe(false);
    expect(windows.engine.calls).toEqual([]);
  });

  it("apply: recheck → download → acceptance → stop app work → install, in that order", async () => {
    const { updater, engine, events, order } = setup({});
    const { accepted, done } = await updater.apply();
    expect(accepted).toBe(true);
    await done;
    expect(engine.calls).toEqual([
      "check:stable",
      "download",
      "accepted",
      "stop-app-work",
      "install",
    ]);
    expect(events.map((e) => e.state)).toEqual([
      "checking",
      "downloading",
      "downloading",
      "downloading",
      "ready",
      "installing",
    ]);
    expect(order[0]).toBe("inFlight:true");
    expect(order.at(-1)).toBe("inFlight:false");
  });

  it("a download failure leaves everything running (app work never stopped)", async () => {
    const { updater, engine } = setup({});
    engine.failDownload = new Error("the download failed: 404");
    const { done } = await updater.apply();
    await done;
    expect(engine.calls).not.toContain("stop-app-work");
    expect(engine.calls).not.toContain("install");
    expect(updater.status()).toMatchObject({ state: "error", error: "the download failed: 404" });
  });

  it("refused while another owner's live marker holds the runtime, and while one runs here", async () => {
    const held = setup({ liveMarker: true });
    expect((await held.updater.apply()).accepted).toBe(false);
    expect(held.engine.calls).toEqual([]);
    expect(held.updater.status().state).toBe("error");

    const busy = setup({});
    const first = await busy.updater.apply();
    const second = await busy.updater.apply();
    expect(first.accepted).toBe(true);
    expect(second.accepted).toBe(false);
    await first.done;
  });

  it("the recheck finding nothing ends idle without downloading", async () => {
    const { updater, engine } = setup({});
    engine.available = false;
    await (await updater.apply()).done;
    expect(engine.calls).toEqual(["check:stable"]);
    expect(updater.status().state).toBe("idle");
  });

  it("an AppImage-owned Linux build that is not running as an AppImage cannot update itself", async () => {
    const { updater, engine } = setup({ platform: "linux", env: {} });
    await (await updater.apply()).done;
    expect(engine.calls).toEqual([]);
    expect(updater.status().error).toMatch(/cannot update itself/);
    const appimage = setup({ platform: "linux", env: { APPIMAGE: "/x/Indus.AppImage" } });
    await (await appimage.updater.apply()).done;
    expect(appimage.engine.calls).toContain("install");
  });

  it("the channel is per device and persisted; the next check uses it", async () => {
    const { updater, feedCalls, dir } = setup({});
    expect(updater.status().channel).toBe("stable");
    await updater.setChannel("canary");
    await updater.check();
    expect(feedCalls).toEqual(["canary"]);
    expect(JSON.parse(fs.readFileSync(path.join(dir, "updates.json"), "utf8"))).toEqual({
      channel: "canary",
    });
  });
});

describe("background checks", () => {
  it("first check 30 s after launch, against a loopback fake feed", async () => {
    const feed = new FakeFeed();
    feed.releases = [{ tag_name: "v1.2.0", html_url: "https://example.invalid/r/1.2.0" }];
    await feed.start();
    const timers = new FakeTimers();
    const events: UpdateStatus[] = [];
    const updater = new Updater({
      stamp: MAC_STAMP,
      appVersion: "1.0.0",
      packaged: true,
      platform: "darwin",
      env: {},
      feed: new ReleaseFeed({
        apiUrl: feed.url,
        env: {},
        cliToken: async () => null,
        fetch: (url, init) => fetch(url, init),
        timers: {
          setTimeout: (fn, ms) => setTimeout(fn, ms),
          clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
        },
        log: () => {},
      }),
      engine: async () => {
        throw new Error("not used");
      },
      gate: { setInFlight: () => {}, inFlightNow: () => false, liveMarker: async () => false },
      stopAppWork: async () => {},
      channelFile: path.join(fs.mkdtempSync(path.join(os.tmpdir(), "bg-")), "updates.json"),
      emit: (status) => events.push(status),
      timers,
      now: () => 1,
      log: () => {},
    });
    updater.startBackgroundChecks();
    timers.advance(FIRST_CHECK_DELAY_MS - 1);
    expect(feed.requests).toHaveLength(0);
    timers.advance(1);
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(feed.requests).toHaveLength(1);
    expect(events.at(-1)).toMatchObject({ state: "available", version: "1.2.0" });
    updater.stop();
    await feed.stop();
  });
});

class FakeNative extends EventEmitter implements NativeUpdaterLike {
  autoDownload = true;
  autoInstallOnAppQuit = true;
  allowPrerelease = false;
  allowDowngrade = true;
  channel: string | null = null;
  installed = 0;
  downloadBehaviour: "accept-after" | "accept-before" | "never" | "fail" = "accept-after";
  async checkForUpdates() {
    return { updateInfo: { version: "1.1.0" }, isUpdateAvailable: true };
  }
  async downloadUpdate() {
    this.emit("download-progress", { percent: 40 });
    if (this.downloadBehaviour === "fail") throw new Error("checksum mismatch");
    if (this.downloadBehaviour === "accept-before") this.emit("update-downloaded");
    if (this.downloadBehaviour === "accept-after")
      setTimeout(() => this.emit("update-downloaded"), 0);
    return ["/tmp/x.zip"];
  }
  quitAndInstall() {
    this.installed += 1;
  }
}

describe("electron-updater engine", () => {
  it("never downloads on a check or installs on quit; channel maps to the native feed", async () => {
    const native = new FakeNative();
    const engine = new ElectronUpdaterEngine(native, new FakeTimers());
    expect(native.autoDownload).toBe(false);
    expect(native.autoInstallOnAppQuit).toBe(false);
    await engine.check("canary");
    expect(native).toMatchObject({
      channel: "canary",
      allowPrerelease: true,
      allowDowngrade: false,
    });
  });

  it("download resolves only after the acceptance, whichever comes first", async () => {
    for (const behaviour of ["accept-after", "accept-before"] as const) {
      const native = new FakeNative();
      native.downloadBehaviour = behaviour;
      const progress: number[] = [];
      await new ElectronUpdaterEngine(native, new FakeTimers()).download((p) => progress.push(p));
      expect(progress).toEqual([40]);
      expect(native.listenerCount("update-downloaded")).toBe(0);
    }
  });

  it("a failed download or a missing acceptance rejects (nothing installed)", async () => {
    const failing = new FakeNative();
    failing.downloadBehaviour = "fail";
    await expect(
      new ElectronUpdaterEngine(failing, new FakeTimers()).download(() => {}),
    ).rejects.toThrow(/checksum mismatch/);
    const silent = new FakeNative();
    silent.downloadBehaviour = "never";
    const timers = new FakeTimers();
    const pending = new ElectronUpdaterEngine(silent, timers, 1000).download(() => {});
    await new Promise((resolve) => setTimeout(resolve, 0));
    timers.advance(1000);
    await expect(pending).rejects.toThrow(/not accepted in time/);
    expect(silent.installed).toBe(0);
  });

  it("an updater error event rejects the download", async () => {
    const native = new FakeNative();
    native.downloadBehaviour = "never";
    const pending = new ElectronUpdaterEngine(native, new FakeTimers()).download(() => {});
    await new Promise((resolve) => setTimeout(resolve, 0));
    native.emit("error", new Error("code signature invalid"));
    await expect(pending).rejects.toThrow(/code signature invalid/);
  });
});
