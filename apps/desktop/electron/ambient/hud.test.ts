import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeScreen, FakeWindow } from "../../testing/fakeFloating.ts";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  defaultHudBounds,
  dragBounds,
  HUD_DRAG_MAX_MS,
  HUD_DRAG_POLL_MS,
  HUD_MIN_SIZE,
  HUD_SAVE_DEBOUNCE_MS,
  HudController,
  type HudState,
  snapBounds,
  usableSavedBounds,
} from "./hud.ts";

const AREA = { x: 0, y: 0, width: 1920, height: 1080 };

describe("HUD geometry", () => {
  it("default bounds: centred near the bottom, never above the area", () => {
    expect(defaultHudBounds(AREA)).toEqual({ x: 650, y: 688, width: 620, height: 320 });
    expect(defaultHudBounds({ x: 0, y: 0, width: 300, height: 200 })).toEqual({
      x: 0,
      y: 0,
      width: 300,
      height: 200,
    });
  });

  it("saved bounds kept on a display, refused when the display is gone, raised to the minimum", () => {
    const displays = [{ workArea: AREA }];
    expect(usableSavedBounds({ x: 10, y: 10, width: 400, height: 200 }, displays)).toEqual({
      x: 10,
      y: 10,
      width: 400,
      height: 200,
    });
    expect(usableSavedBounds({ x: 3000, y: 10, width: 400, height: 200 }, displays)).toBeNull();
    expect(usableSavedBounds({ x: 10, y: 10, width: 10, height: 10 }, displays)).toEqual({
      x: 10,
      y: 10,
      width: HUD_MIN_SIZE.width,
      height: HUD_MIN_SIZE.height,
    });
    expect(usableSavedBounds({ x: "a", y: 1, width: 400, height: 200 }, displays)).toBeNull();
    expect(
      usableSavedBounds({ x: Number.NaN, y: 1, width: 400, height: 200 }, displays),
    ).toBeNull();
    expect(usableSavedBounds(null, displays)).toBeNull();
  });

  it("snap puts the pointer at the top centre and keeps the HUD inside the area", () => {
    expect(snapBounds({ x: 1000, y: 500 }, { width: 400, height: 200 }, AREA)).toEqual({
      x: 800,
      y: 460,
      width: 400,
      height: 200,
    });
    expect(snapBounds({ x: 5, y: 1075 }, { width: 400, height: 200 }, AREA)).toEqual({
      x: 0,
      y: 880,
      width: 400,
      height: 200,
    });
  });

  it("drag: move follows, edges resize and never shrink below the minimum", () => {
    const start = { x: 100, y: 100, width: 500, height: 300 };
    expect(dragBounds(start, "move", 10, -5)).toEqual({ ...start, x: 110, y: 95 });
    expect(dragBounds(start, "se", 20, 30)).toEqual({ ...start, width: 520, height: 330 });
    expect(dragBounds(start, "nw", 20, 30)).toEqual({ x: 120, y: 130, width: 480, height: 270 });
    expect(dragBounds(start, "w", 400, 0)).toEqual({
      ...start,
      x: 100 + 500 - HUD_MIN_SIZE.width,
      width: HUD_MIN_SIZE.width,
    });
    expect(dragBounds(start, "n", 0, 999)).toMatchObject({ height: HUD_MIN_SIZE.height });
  });
});

describe("HUD controller", () => {
  let dir: string;
  let timers: FakeTimers;
  let screen: FakeScreen;
  let main: FakeWindow;
  let created: FakeWindow[];
  let events: HudState[];
  let openChanges: boolean[];
  let hud: HudController;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "hud-"));
    timers = new FakeTimers();
    screen = new FakeScreen();
    main = new FakeWindow();
    main.visible = true;
    created = [];
    events = [];
    openChanges = [];
    hud = new HudController({
      create: (_options, bounds) => {
        const win = new FakeWindow(bounds);
        created.push(win);
        return win;
      },
      mainWindow: () => main,
      screen,
      statePath: path.join(dir, "hud-state.json"),
      timers,
      now: () => timers.now,
      broadcast: (state) => events.push(state),
      onOpenChanged: (open) => openChanges.push(open),
      log: () => {},
    });
  });

  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it("opens once; the main window steps aside on show and comes back (unfocused) on close", () => {
    hud.open("s1", null);
    hud.open("s1", null);
    expect(created).toHaveLength(1);
    const win = created[0] as FakeWindow;
    win.show();
    expect(main.visible).toBe(false);
    expect(openChanges).toEqual([true]);
    hud.close();
    expect(win.destroyed).toBe(true);
    expect(main.visible).toBe(true);
    expect(main.focused).toBe(0);
    expect(openChanges).toEqual([true, false]);
    expect(events.at(-1)).toEqual({ open: false, sessionId: "s1", profile: null });
  });

  it("another session switches in place; another profile replaces the window", () => {
    hud.open("s1", "work");
    hud.open("s2", "work");
    expect(created).toHaveLength(1);
    expect(events.at(-1)).toEqual({ open: true, sessionId: "s2", profile: "work" });
    hud.open("s3", "home");
    expect(created).toHaveLength(2);
    expect((created[0] as FakeWindow).destroyed).toBe(true);
    expect(hud.state()).toEqual({ open: true, sessionId: "s3", profile: "home" });
    // The replaced window's close is not a teardown.
    expect(openChanges).toEqual([true]);
  });

  it("toggle opens the fallback session, then closes", () => {
    hud.toggle(() => ({ sessionId: "last", profile: null }));
    expect(hud.state()).toMatchObject({ open: true, sessionId: "last" });
    hud.toggle(() => ({ sessionId: "other", profile: null }));
    expect(hud.state().open).toBe(false);
  });

  it("a closed HUD (from its own side) still restores main and tells every window", () => {
    hud.open("s1", null);
    const win = created[0] as FakeWindow;
    win.show();
    win.destroy(); // a crash or a native close
    expect(main.visible).toBe(true);
    expect(events.at(-1)?.open).toBe(false);
  });

  it("drag follows the pointer only from the HUD and only while held; geometry is saved", () => {
    hud.open("s1", null);
    const win = created[0] as FakeWindow;
    const start = win.getBounds();
    hud.dragFrom(9999, "start", "move"); // not the HUD
    screen.cursor = { x: 150, y: 120 };
    timers.advance(HUD_DRAG_POLL_MS * 2);
    expect(win.getBounds()).toEqual(start);

    screen.cursor = { x: 100, y: 100 };
    hud.dragFrom(win.webContents.id, "start", "move");
    screen.cursor = { x: 150, y: 120 };
    timers.advance(HUD_DRAG_POLL_MS);
    expect(win.getBounds()).toMatchObject({ x: start.x + 50, y: start.y + 20 });
    hud.dragFrom(win.webContents.id, "end", "move");
    screen.cursor = { x: 400, y: 400 };
    timers.advance(HUD_DRAG_POLL_MS * 3);
    expect(win.getBounds()).toMatchObject({ x: start.x + 50, y: start.y + 20 });

    timers.advance(HUD_SAVE_DEBOUNCE_MS);
    const saved = JSON.parse(fs.readFileSync(path.join(dir, "hud-state.json"), "utf8"));
    expect(saved).toEqual(win.getBounds());

    // Reopened later: the saved spot is reused.
    hud.close();
    hud.open("s1", null);
    expect((created[1] as FakeWindow).getBounds()).toEqual(saved);
  });

  it("an edge resize opens the size gate for the call and keeps the minimum", () => {
    hud.open("s1", null);
    const win = created[0] as FakeWindow;
    screen.cursor = { x: 0, y: 0 };
    hud.dragFrom(win.webContents.id, "start", "se");
    screen.cursor = { x: -1000, y: -1000 };
    timers.advance(HUD_DRAG_POLL_MS);
    expect(win.getBounds()).toMatchObject(HUD_MIN_SIZE);
    expect(win.resizable).toBe(false);
  });

  it("a press that never reports its release stops being followed after 30 s", () => {
    hud.open("s1", null);
    const win = created[0] as FakeWindow;
    hud.dragFrom(win.webContents.id, "start", "move");
    timers.advance(HUD_DRAG_MAX_MS + HUD_DRAG_POLL_MS * 2);
    const at = win.getBounds();
    screen.cursor = { x: 900, y: 900 };
    timers.advance(HUD_DRAG_POLL_MS * 4);
    expect(win.getBounds()).toEqual(at);
  });

  it("reset puts it back at the default spot of the pointer's display and saves", () => {
    hud.open("s1", null);
    const win = created[0] as FakeWindow;
    win.setResizable(true);
    win.setBounds({ x: 5, y: 5, width: 700, height: 500 });
    win.setResizable(false);
    expect(hud.resetLayout()).toBe(true);
    expect(win.getBounds()).toEqual(defaultHudBounds(AREA));
    expect(JSON.parse(fs.readFileSync(path.join(dir, "hud-state.json"), "utf8"))).toEqual(
      defaultHudBounds(AREA),
    );
  });

  it("the saved spot is ignored when its display is gone", () => {
    fs.writeFileSync(
      path.join(dir, "hud-state.json"),
      JSON.stringify({ x: 4000, y: 100, width: 500, height: 200 }),
    );
    hud.open(null, null);
    expect((created[0] as FakeWindow).getBounds()).toEqual(defaultHudBounds(AREA));
  });

  it("snap-to-pointer moves an open HUD; nothing happens with none open", () => {
    hud.snapToPointer();
    hud.open("s1", null);
    screen.cursor = { x: 960, y: 540 };
    hud.snapToPointer();
    const win = created[0] as FakeWindow;
    expect(win.getBounds()).toMatchObject({ x: 650, y: 500 });
  });
});
