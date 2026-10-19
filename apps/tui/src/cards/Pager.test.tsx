import { describe, expect, it, vi } from "vitest";
import { KEY, mount } from "./inkHarness.ts";
import { isLongOutput, Pager, pagerLines, pagerStep, pageSizeFor } from "./Pager.tsx";

const lines = (n: number): string[] => Array.from({ length: n }, (_, i) => `row ${i + 1}`);

describe("when output pages", () => {
  it("pages over 180 characters", () => {
    expect(isLongOutput("a".repeat(180))).toBe(false);
    expect(isLongOutput("a".repeat(181))).toBe(true);
  });

  it("pages over two non-empty lines", () => {
    expect(isLongOutput("one\n\ntwo\n")).toBe(false);
    expect(isLongOutput("one\ntwo\nthree")).toBe(true);
  });

  it("page size is rows - 6, at least 5", () => {
    expect(pageSizeFor(30)).toBe(24);
    expect(pageSizeFor(8)).toBe(5);
  });

  it("splits text into lines without a trailing blank", () => {
    expect(pagerLines("a\nb\n\n")).toEqual(["a", "b"]);
  });
});

describe("pager keys", () => {
  const step = (input: string, key: object, offset: number) =>
    pagerStep(input, key, offset, 40, 10);

  it("moves a line with arrows and j/k, clamped", () => {
    expect(step("", { downArrow: true }, 0)).toEqual({ type: "move", offset: 1 });
    expect(step("j", {}, 0)).toEqual({ type: "move", offset: 1 });
    expect(step("k", {}, 5)).toEqual({ type: "move", offset: 4 });
    expect(step("", { upArrow: true }, 0)).toEqual({ type: "none" });
    expect(step("j", {}, 30)).toEqual({ type: "none" });
  });

  it("pages with PgDn/Space/Enter and back with PgUp/b", () => {
    expect(step(" ", {}, 0)).toEqual({ type: "move", offset: 10 });
    expect(step("", { pageDown: true }, 10)).toEqual({ type: "move", offset: 20 });
    expect(step("", { return: true }, 25)).toEqual({ type: "move", offset: 30 });
    expect(step("b", {}, 20)).toEqual({ type: "move", offset: 10 });
    expect(step("", { pageUp: true }, 4)).toEqual({ type: "move", offset: 0 });
  });

  it("closes when paging past the last page", () => {
    expect(step(" ", {}, 30)).toEqual({ type: "close" });
    expect(step("", { return: true }, 30)).toEqual({ type: "close" });
  });

  it("g and G jump to the ends", () => {
    expect(step("G", {}, 0)).toEqual({ type: "move", offset: 30 });
    expect(step("g", {}, 17)).toEqual({ type: "move", offset: 0 });
  });

  it("Esc, q and Ctrl+C close", () => {
    expect(step("", { escape: true }, 3)).toEqual({ type: "close" });
    expect(step("q", {}, 3)).toEqual({ type: "close" });
    expect(step("c", { ctrl: true }, 3)).toEqual({ type: "close" });
  });
});

describe("pager on screen", () => {
  it("walks the pages and closes after the last", async () => {
    const onClose = vi.fn();
    const app = await mount(
      <Pager title="/tools" lines={lines(25)} rows={16} onClose={onClose} />,
      {
        rows: 16,
      },
    );
    let frame = app.lastFrame();
    expect(frame).toContain("/tools");
    expect(frame).toContain("row 10");
    expect(frame).not.toContain("row 11");
    await app.press(KEY.space);
    frame = app.lastFrame();
    expect(frame).toContain("row 11");
    expect(frame).toContain("row 20");
    await app.press("G");
    frame = app.lastFrame();
    expect(frame).toContain("row 25");
    expect(frame).toContain("end of 25 lines");
    expect(onClose).not.toHaveBeenCalled();
    await app.press(KEY.enter);
    expect(onClose).toHaveBeenCalledTimes(1);
    app.unmount();
  });

  it("q closes on the first page", async () => {
    const onClose = vi.fn();
    const app = await mount(<Pager lines={lines(40)} rows={20} onClose={onClose} />);
    await app.press("q");
    expect(onClose).toHaveBeenCalledTimes(1);
    app.unmount();
  });
});
