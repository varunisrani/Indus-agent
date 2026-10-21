import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  placeWindow,
  readWindowState,
  sanitizeWindowState,
  writeWindowState,
} from "./windowState.ts";

const primary = { workArea: { x: 0, y: 25, width: 1920, height: 1055 } };
const second = { workArea: { x: 1920, y: 0, width: 1280, height: 1024 } };

describe("window placement", () => {
  it("off-screen saved bounds (monitor unplugged) are centred on the primary display", () => {
    const placed = placeWindow(
      { x: 3500, y: 200, width: 1000, height: 700, maximized: false },
      [primary],
      primary,
    );
    expect(placed.bounds).toEqual({ width: 1000, height: 700, x: 460, y: 203 });
  });

  it("a sliver under 48 px visible is not trusted", () => {
    const placed = placeWindow(
      { x: 1920 - 40, y: 100, width: 800, height: 700, maximized: false },
      [primary],
      primary,
    );
    expect(placed.bounds.x).toBe(560);
  });

  it("a position on the second display is kept, size clamped to its work area", () => {
    const placed = placeWindow(
      { x: 2000, y: 10, width: 4000, height: 3000, maximized: true },
      [primary, second],
      primary,
    );
    expect(placed).toEqual({
      bounds: { x: 2000, y: 10, width: 1280, height: 1024 },
      maximized: true,
    });
  });

  it("no saved state: default size centred", () => {
    expect(placeWindow(null, [primary], primary).bounds).toEqual({
      width: 1280,
      height: 860,
      x: 320,
      y: 123,
    });
  });

  it("the minimum size wins even on a tiny display", () => {
    const tiny = { workArea: { x: 0, y: 0, width: 300, height: 500 } };
    expect(placeWindow(null, [tiny], tiny).bounds).toMatchObject({ width: 400, height: 620 });
  });
});

describe("sanitize", () => {
  it.each([
    [null],
    ["x"],
    [{}],
    [{ width: "800", height: 600 }],
    [{ width: Number.NaN, height: 600 }],
    [{ width: -1, height: 600 }],
  ])("garbage %j → null", (raw) => {
    expect(sanitizeWindowState(raw)).toBeNull();
  });

  it("raises small sizes, keeps a finite position pair and a strict maximized flag", () => {
    expect(
      sanitizeWindowState({ width: 10.4, height: 20, x: 5.6, y: 7, maximized: "yes" }),
    ).toEqual({
      width: 400,
      height: 620,
      x: 6,
      y: 7,
      maximized: false,
    });
    expect(sanitizeWindowState({ width: 900, height: 700, x: 5 })).toEqual({
      width: 900,
      height: 700,
      maximized: false,
    });
  });
});

describe("window-state.json", () => {
  const dirs: string[] = [];
  afterEach(() => {
    for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
  });

  it("round trips and treats a corrupt file as no state", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-winstate-"));
    dirs.push(dir);
    const file = path.join(dir, "window-state.json");
    expect(readWindowState(file)).toBeNull();
    writeWindowState(file, { x: 1, y: 2, width: 1000, height: 800, maximized: true });
    expect(readWindowState(file)).toEqual({
      x: 1,
      y: 2,
      width: 1000,
      height: 800,
      maximized: true,
    });
    fs.writeFileSync(file, "{not json");
    expect(readWindowState(file)).toBeNull();
    expect(fs.readdirSync(dir)).toEqual(["window-state.json"]);
  });
});
