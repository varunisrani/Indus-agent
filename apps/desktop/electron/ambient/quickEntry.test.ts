import { beforeEach, describe, expect, it } from "vitest";
import { FakeScreen, FakeWindow } from "../../testing/fakeFloating.ts";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { ActiveSessions } from "./activeSession.ts";
import {
  QUICK_RELAY_TIMEOUT_MS,
  QuickEntryController,
  type QuickTarget,
  quickBounds,
} from "./quickEntry.ts";

describe("quick entry bounds", () => {
  it("centred a quarter down the pointer's display, inside it", () => {
    expect(quickBounds({ x: 0, y: 0, width: 1920, height: 1080 })).toEqual({
      x: 650,
      y: 270,
      width: 620,
      height: 150,
    });
    expect(quickBounds({ x: 1920, y: 0, width: 500, height: 100 })).toEqual({
      x: 1920,
      y: 0,
      width: 500,
      height: 100,
    });
  });
});

describe("quick entry controller", () => {
  let timers: FakeTimers;
  let screen: FakeScreen;
  let created: FakeWindow[];
  let app: FakeWindow;
  let target: QuickTarget | null;
  let delivered: Array<{ requestId: string; text: string; sessionId: string | null }>;
  let deliverOk: boolean;
  let raised: FakeWindow[];
  let shown: number;
  let logs: string[];
  let quick: QuickEntryController;
  let ids: number;

  beforeEach(() => {
    timers = new FakeTimers();
    screen = new FakeScreen([
      { workArea: { x: 0, y: 0, width: 1920, height: 1080 } },
      { workArea: { x: 1920, y: 0, width: 1280, height: 1024 } },
    ]);
    created = [];
    app = new FakeWindow();
    target = { contentsId: app.webContents.id, sessionId: "s-last", window: app };
    delivered = [];
    deliverOk = true;
    raised = [];
    shown = 0;
    logs = [];
    ids = 0;
    quick = new QuickEntryController({
      create: (bounds) => {
        const win = new FakeWindow(bounds);
        created.push(win);
        return win;
      },
      screen,
      target: () => target,
      deliver: (_target, payload) => {
        delivered.push(payload);
        return deliverOk;
      },
      shown: () => {
        shown += 1;
      },
      raise: (win) => raised.push(win as FakeWindow),
      timers,
      newId: () => `r${++ids}`,
      log: (line) => logs.push(line),
    });
  });

  function openQuick(): FakeWindow {
    quick.show();
    const win = created.at(-1) as FakeWindow;
    win.show(); // the manager reveals it on paint
    return win;
  }

  it("toggles; the window takes focus when shown; blur hides it", () => {
    const win = openQuick();
    expect(win.focused).toBe(1);
    quick.toggle();
    expect(win.visible).toBe(false);
    screen.cursor = { x: 2500, y: 500 };
    quick.toggle();
    expect(created).toHaveLength(1);
    expect(win.visible).toBe(true);
    expect(win.getBounds().x).toBeGreaterThanOrEqual(1920); // moved to the pointer's display
    expect(shown).toBe(1);
    win.emit("blur");
    expect(win.visible).toBe(false);
  });

  it("delivers to the last-active window and answers when it acknowledges", async () => {
    const win = openQuick();
    const result = quick.submit(win.webContents.id, "  hello  ");
    expect(delivered).toEqual([{ requestId: "r1", text: "hello", sessionId: "s-last" }]);
    quick.ack(app.webContents.id, { requestId: "r1", ok: true, sessionId: "s-last" });
    await expect(result).resolves.toEqual({ ok: true, sessionId: "s-last" });
    expect(win.visible).toBe(false);
    expect(raised).toEqual([app]);
  });

  it("refuses other senders, empty text, and a second send while one is waiting", async () => {
    const win = openQuick();
    await expect(quick.submit(app.webContents.id, "x")).resolves.toMatchObject({
      ok: false,
      code: "failed",
    });
    await expect(quick.submit(win.webContents.id, "   ")).resolves.toMatchObject({
      ok: false,
      code: "empty",
    });
    void quick.submit(win.webContents.id, "one");
    await expect(quick.submit(win.webContents.id, "two")).resolves.toMatchObject({
      ok: false,
      code: "busy",
    });
    expect(delivered).toHaveLength(1);
  });

  it("no app window → no-target, and the composer keeps its text (still visible)", async () => {
    target = null;
    const win = openQuick();
    await expect(quick.submit(win.webContents.id, "hi")).resolves.toMatchObject({
      ok: false,
      code: "no-target",
    });
    expect(win.visible).toBe(true);
    target = { contentsId: app.webContents.id, sessionId: null, window: app };
    deliverOk = false;
    await expect(quick.submit(win.webContents.id, "hi")).resolves.toMatchObject({
      code: "no-target",
    });
  });

  it("no answer in 15 s → unconfirmed (never failed); a late answer is only logged", async () => {
    const win = openQuick();
    const result = quick.submit(win.webContents.id, "slow");
    timers.advance(QUICK_RELAY_TIMEOUT_MS);
    await expect(result).resolves.toMatchObject({ ok: false, code: "unconfirmed" });
    quick.ack(app.webContents.id, { requestId: "r1", ok: true });
    expect(logs.join("\n")).toMatch(/late answer/);
    expect(raised).toEqual([]);
    expect(win.visible).toBe(true);
  });

  it("an answer from a window that was not asked is ignored; a failure keeps the text", async () => {
    const win = openQuick();
    const result = quick.submit(win.webContents.id, "x");
    quick.ack(win.webContents.id, { requestId: "r1", ok: true });
    quick.ack(app.webContents.id, { requestId: "r1", ok: false, error: "refused" });
    await expect(result).resolves.toEqual({ ok: false, code: "failed", message: "refused" });
    expect(win.visible).toBe(true);
  });

  it("close (feature off) closes the window", () => {
    const win = openQuick();
    quick.close();
    expect(win.destroyed).toBe(true);
    expect(quick.isQuick(win.webContents.id)).toBe(false);
  });
});

describe("last-active session", () => {
  it("the newest report from a live window wins; focus moves a window to the front", () => {
    const active = new ActiveSessions();
    const live = new Set([1, 2, 3]);
    active.report(1, "a");
    active.report(2, "b");
    expect(active.latest((id) => live.has(id))).toMatchObject({ contentsId: 2, sessionId: "b" });
    active.touch(1);
    expect(active.latest((id) => live.has(id))).toMatchObject({ contentsId: 1, sessionId: "a" });
    active.touch(3);
    expect(active.latest((id) => live.has(id))).toMatchObject({ contentsId: 3, sessionId: null });
    live.delete(3);
    expect(active.latest((id) => live.has(id))?.contentsId).toBe(1);
    active.forget(1);
    expect(active.latest((id) => live.has(id))?.contentsId).toBe(2);
    expect(new ActiveSessions().latest(() => true)).toBeNull();
  });
});
