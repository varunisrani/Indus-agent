// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { isChunkLoadFailure, runtimeAhead, watchChunkFailures } from "./skew.ts";

describe("renderer skew", () => {
  it("recognises a missing lazy chunk and nothing else", () => {
    expect(
      isChunkLoadFailure(
        new TypeError("Failed to fetch dynamically imported module: file:///x.js"),
      ),
    ).toBe(true);
    expect(isChunkLoadFailure({ name: "ChunkLoadError", message: "" })).toBe(true);
    expect(isChunkLoadFailure("Importing a module script failed.")).toBe(true);
    expect(isChunkLoadFailure(new Error("Cannot read properties of undefined"))).toBe(false);
    expect(isChunkLoadFailure(null)).toBe(false);
  });

  it("a lazy-chunk 404 (Vite's preload error) → one skew notice, the error consumed", () => {
    let notices = 0;
    const stop = watchChunkFailures(window, () => {
      notices += 1;
    });
    const preload = new Event("vite:preloadError", { cancelable: true });
    window.dispatchEvent(preload);
    expect(preload.defaultPrevented).toBe(true);
    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));
    expect(notices).toBe(1);
    stop();
  });

  it("an unhandled chunk rejection is a notice; another rejection is left alone", () => {
    let notices = 0;
    const stop = watchChunkFailures(window, () => {
      notices += 1;
    });
    const other = new Event("unhandledrejection", { cancelable: true }) as PromiseRejectionEvent;
    Object.defineProperty(other, "reason", { value: new Error("boom") });
    window.dispatchEvent(other);
    expect(other.defaultPrevented).toBe(false);
    const chunk = new Event("unhandledrejection", { cancelable: true }) as PromiseRejectionEvent;
    Object.defineProperty(chunk, "reason", {
      value: new TypeError("error loading dynamically imported module"),
    });
    window.dispatchEvent(chunk);
    expect(chunk.defaultPrevented).toBe(true);
    expect(notices).toBe(1);
    stop();
  });

  it("app behind runtime only for a packaged app", () => {
    expect(runtimeAhead("1.0.0", "1.1.0", true)).toBe(true);
    expect(runtimeAhead("1.0.0", "1.1.0", false)).toBe(false);
    expect(runtimeAhead("1.1.0", "1.1.0", true)).toBe(false);
    expect(runtimeAhead("1.0.0", null, true)).toBe(false);
  });
});
