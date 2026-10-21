import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { QuitGuard } from "../quitGuard.ts";
import {
  blockerWanted,
  KeepAwake,
  type PowerSaveBlockerLike,
  TIMED_SLEEP_IDLE_SECONDS,
} from "./keepAwake.ts";
import {
  AmbientSettingsStore,
  defaultAmbientSettings,
  type KeepAwakeMode,
  sanitizeAmbientSettings,
} from "./settings.ts";

class FakeBlocker implements PowerSaveBlockerLike {
  starts = 0;
  stops = 0;
  private live = new Set<number>();
  private next = 1;
  start(): number {
    this.starts += 1;
    const id = this.next++;
    this.live.add(id);
    return id;
  }
  stop(id: number): void {
    this.stops += 1;
    this.live.delete(id);
  }
  isStarted(id: number): boolean {
    return this.live.has(id);
  }
  /** The OS dropped it behind our back. */
  dropAll(): void {
    this.live.clear();
  }
}

function setup(mode: KeepAwakeMode = "while-working") {
  const blocker = new FakeBlocker();
  const state = { mode, offered: false, offers: 0, canShow: true };
  const keepAwake = new KeepAwake({
    blocker,
    mode: () => state.mode,
    offered: () => state.offered,
    markOffered: () => {
      state.offered = true;
    },
    offer: () => {
      if (!state.canShow) return false;
      state.offers += 1;
      return true;
    },
    log: () => {},
  });
  return { blocker, state, keepAwake };
}

describe("keep awake", () => {
  it("mode table", () => {
    expect(blockerWanted("never", true)).toBe(false);
    expect(blockerWanted("while-working", false)).toBe(false);
    expect(blockerWanted("while-working", true)).toBe(true);
    expect(blockerWanted("always", false)).toBe(true);
  });

  it("while working: started once per working period and released at the end", () => {
    const { blocker, keepAwake } = setup();
    keepAwake.reconcile();
    expect(blocker.starts).toBe(0);
    keepAwake.setWorking(true);
    keepAwake.setWorking(true);
    keepAwake.setWorking(true);
    expect(blocker.starts).toBe(1);
    expect(keepAwake.active()).toBe(true);
    keepAwake.setWorking(false);
    expect(blocker.stops).toBe(1);
    expect(keepAwake.active()).toBe(false);
    keepAwake.setWorking(true);
    expect(blocker.starts).toBe(2);
  });

  it("follows the quit guard's work picture across windows", () => {
    const { blocker, keepAwake } = setup();
    const guard = new QuitGuard();
    guard.subscribe(() => keepAwake.setWorking(guard.work().length > 0));
    guard.report(1, [{ id: "s1", label: "" }]);
    guard.report(2, [{ id: "s1", label: "" }]);
    guard.report(1, []);
    expect(blocker.starts).toBe(1);
    expect(keepAwake.active()).toBe(true);
    guard.forget(2);
    expect(keepAwake.active()).toBe(false);
  });

  it("always / never and a mode change while working", () => {
    const { blocker, state, keepAwake } = setup("always");
    keepAwake.reconcile();
    expect(keepAwake.active()).toBe(true);
    keepAwake.setWorking(true);
    state.mode = "while-working";
    keepAwake.reconcile();
    expect(blocker.starts).toBe(1); // kept, not restarted
    state.mode = "never";
    keepAwake.reconcile();
    expect(keepAwake.active()).toBe(false);
  });

  it("a blocker the OS dropped is started again on the next reconcile", () => {
    const { blocker, keepAwake } = setup("always");
    keepAwake.reconcile();
    blocker.dropAll();
    keepAwake.reconcile();
    expect(blocker.starts).toBe(2);
    keepAwake.dispose();
    expect(keepAwake.active()).toBe(false);
  });

  it("offers once after a timed sleep interrupted a turn while off", () => {
    const { state, keepAwake } = setup("never");
    keepAwake.setWorking(true);
    keepAwake.noteSuspend(TIMED_SLEEP_IDLE_SECONDS);
    keepAwake.noteResume();
    expect(state.offers).toBe(1);
    expect(state.offered).toBe(true);
    keepAwake.noteSuspend(600);
    keepAwake.noteResume();
    expect(state.offers).toBe(1);
  });

  it("no offer: short idle (a closed lid), not working, keep awake on", () => {
    for (const [mode, working, idle] of [
      ["never", true, TIMED_SLEEP_IDLE_SECONDS - 1],
      ["never", false, 600],
      ["while-working", true, 600],
      ["always", true, 600],
    ] as const) {
      const { state, keepAwake } = setup(mode);
      keepAwake.setWorking(working);
      keepAwake.noteSuspend(idle);
      keepAwake.noteResume();
      expect(state.offers).toBe(0);
    }
  });

  it("no window to show it: kept for the next wake", () => {
    const { state, keepAwake } = setup("never");
    keepAwake.setWorking(true);
    keepAwake.noteSuspend(120);
    state.canShow = false;
    keepAwake.noteResume();
    expect(state.offered).toBe(false);
    state.canShow = true;
    keepAwake.noteResume();
    expect(state.offers).toBe(1);
  });
});

describe("ambient settings", () => {
  it("defaults and sanitising", () => {
    expect(defaultAmbientSettings()).toEqual({
      minimizeToTray: false,
      keepAwake: "while-working",
      keepAwakeOffered: false,
      notifications: true,
      sounds: true,
    });
    expect(sanitizeAmbientSettings({ keepAwake: "sometimes", minimizeToTray: "yes" })).toEqual(
      defaultAmbientSettings(),
    );
    expect(sanitizeAmbientSettings({ keepAwake: "always", sounds: false })).toMatchObject({
      keepAwake: "always",
      sounds: false,
    });
  });

  it("persists per device and survives a corrupt file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "amb-"));
    const file = path.join(dir, "ambient.json");
    const store = new AmbientSettingsStore(file, () => {});
    store.update({ minimizeToTray: true, keepAwake: "never" });
    expect(new AmbientSettingsStore(file, () => {}).get()).toMatchObject({
      minimizeToTray: true,
      keepAwake: "never",
    });
    fs.writeFileSync(file, "garbage");
    expect(new AmbientSettingsStore(file, () => {}).get()).toEqual(defaultAmbientSettings());
    fs.rmSync(dir, { recursive: true, force: true });
  });
});
