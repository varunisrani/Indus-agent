import { describe, expect, it, vi } from "vitest";
import {
  type IndicatorWindowLike,
  indicatorBounds,
  WAKE_INDICATOR_SIZE,
  WakeIndicatorController,
} from "./wakeIndicator.ts";

let nextId = 900;
function fakeWindow() {
  const closed: Array<() => void> = [];
  let destroyed = false;
  return {
    webContents: { id: nextId++ },
    on: (event: string, fn: () => void) => {
      if (event === "closed") closed.push(fn);
    },
    setBounds: vi.fn(),
    hide: vi.fn(),
    show: vi.fn(),
    showInactive: vi.fn(),
    focus: vi.fn(),
    close: vi.fn(() => {
      destroyed = true;
      for (const fn of closed) fn();
    }),
    isDestroyed: () => destroyed,
  };
}

function setup() {
  const made: ReturnType<typeof fakeWindow>[] = [];
  const emitted: string[] = [];
  const kinds = new Map<number, string>([
    [1, "main"],
    [2, "hud"],
    [3, "quick"],
  ]);
  const controller = new WakeIndicatorController({
    create: () => {
      const win = fakeWindow();
      made.push(win);
      kinds.set(win.webContents.id, "wake");
      return win as unknown as IndicatorWindowLike;
    },
    screen: {
      getPrimaryDisplay: () => ({ workArea: { x: 100, y: 30, width: 1600, height: 900 } }),
    },
    kindOf: (id) => kinds.get(id) ?? null,
    emit: (_win, state) => emitted.push(state),
  });
  return { controller, made, emitted };
}

describe("wake indicator", () => {
  it("sits at the top centre of the work area", () => {
    expect(indicatorBounds({ workArea: { x: 100, y: 30, width: 1600, height: 900 } })).toEqual({
      ...WAKE_INDICATOR_SIZE,
      x: 100 + (1600 - WAKE_INDICATOR_SIZE.width) / 2,
      y: 38,
    });
    const tiny = indicatorBounds({ workArea: { x: 0, y: 0, width: 100, height: 20 } });
    expect(tiny).toEqual({ x: 0, y: 0, width: 100, height: 20 });
  });

  it("is made on the first visible state, hidden (not closed) on hidden, never focused", () => {
    const { controller, made, emitted } = setup();
    controller.set("hidden");
    expect(made).toHaveLength(0);
    controller.set("listening");
    controller.set("heard");
    controller.set("heard");
    controller.set("capturing");
    controller.set("hidden");
    controller.set("listening");
    expect(made).toHaveLength(1);
    const win = made[0] as ReturnType<typeof fakeWindow>;
    expect(win.hide).toHaveBeenCalledTimes(1);
    expect(win.showInactive).toHaveBeenCalledTimes(3);
    expect(win.focus).not.toHaveBeenCalled();
    expect(emitted).toEqual(["listening", "heard", "capturing", "hidden", "listening"]);
  });

  it("only app windows set it and only the indicator reads it", () => {
    const { controller, made } = setup();
    expect(() => controller.requireApp(1)).not.toThrow();
    expect(() => controller.requireApp(2)).not.toThrow();
    expect(() => controller.requireApp(3)).toThrow(/only an app window/);
    expect(() => controller.requireApp(77)).toThrow(/only an app window/);
    controller.set("listening");
    const id = made[0]?.webContents.id as number;
    expect(() => controller.requireIndicator(id)).not.toThrow();
    expect(() => controller.requireIndicator(1)).toThrow(/only the wake indicator/);
  });

  it("close forgets the window and the state", () => {
    const { controller, made } = setup();
    controller.set("capturing");
    controller.close();
    expect(made[0]?.close).toHaveBeenCalled();
    expect(controller.state()).toBe("hidden");
    controller.set("listening");
    expect(made).toHaveLength(2);
  });
});
