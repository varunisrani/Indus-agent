// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n/index.tsx";
import { installErrorForwarding, REPORTS_PER_MINUTE } from "./errorForwarding.ts";
import { LogsViewer } from "./LogsViewer.tsx";

describe("renderer error forwarding", () => {
  it("forwards uncaught errors and unhandled rejections with the window's source", () => {
    const target = new EventTarget();
    const send = vi.fn();
    const stop = installErrorForwarding(target, send, { source: "renderer:main", now: () => 0 });
    const error = new TypeError("x is undefined");
    target.dispatchEvent(
      new ErrorEvent("error", {
        error,
        message: error.message,
        filename: "app.js",
        lineno: 3,
        colno: 9,
      }),
    );
    const rejection = new Event("unhandledrejection") as Event & { reason: unknown };
    rejection.reason = "socket closed";
    target.dispatchEvent(rejection);
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0]?.[0]).toMatchObject({
      message: "error: TypeError: x is undefined",
      source: "renderer:main",
    });
    expect(String(send.mock.calls[0]?.[0].stack)).toContain("at app.js:3:9");
    expect(send.mock.calls[1]?.[0]).toEqual({
      message: "unhandled rejection: socket closed",
      source: "renderer:main",
    });
    stop();
    target.dispatchEvent(new ErrorEvent("error", { message: "after stop" }));
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("drops repeats within 5 s and caps a minute's reports", () => {
    const target = new EventTarget();
    const send = vi.fn();
    let now = 0;
    installErrorForwarding(target, send, { source: "renderer:main", now: () => now });
    for (let i = 0; i < 3; i += 1)
      target.dispatchEvent(new ErrorEvent("error", { message: "same" }));
    expect(send).toHaveBeenCalledTimes(1);
    now = 6_000;
    target.dispatchEvent(new ErrorEvent("error", { message: "same" }));
    expect(send).toHaveBeenCalledTimes(2);
    for (let i = 0; i < 50; i += 1)
      target.dispatchEvent(new ErrorEvent("error", { message: `e${i}` }));
    expect(send).toHaveBeenCalledTimes(REPORTS_PER_MINUTE);
    now = 70_000;
    target.dispatchEvent(new ErrorEvent("error", { message: "new minute" }));
    expect(send).toHaveBeenCalledTimes(REPORTS_PER_MINUTE + 1);
  });

  it("the browser's ResizeObserver loop notice is not an error (W48-c)", () => {
    // Chromium reports it as an error event with no error object and no position; it says size
    // changes were delivered in the next frame, nothing failed.
    const target = new EventTarget();
    const send = vi.fn();
    installErrorForwarding(target, send, { source: "renderer:main", now: () => 0 });
    for (const message of [
      "ResizeObserver loop completed with undelivered notifications.",
      "ResizeObserver loop limit exceeded",
    ]) {
      target.dispatchEvent(
        new ErrorEvent("error", {
          message,
          filename: "file:///tmp/indus-e2e-app-0cLphi/app/dist/index.html",
          lineno: 0,
          colno: 0,
        }),
      );
    }
    expect(send).not.toHaveBeenCalled();
    // a real error that mentions it still goes out
    target.dispatchEvent(new ErrorEvent("error", { error: new Error("ResizeObserver loop") }));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("no run folder in the log: no position, no location line; file URLs keep their file name", () => {
    const target = new EventTarget();
    const send = vi.fn();
    installErrorForwarding(target, send, { source: "renderer:main", now: () => 0 });
    target.dispatchEvent(
      new ErrorEvent("error", {
        message: "Script error.",
        filename: "file:///tmp/indus-e2e-app-0cLphi/app/dist/index.html",
        lineno: 0,
        colno: 0,
      }),
    );
    expect(send.mock.calls[0]?.[0]).toEqual({
      message: "error: Script error.",
      source: "renderer:main",
    });
    const error = new TypeError("boom");
    error.stack = [
      "TypeError: boom",
      "    at render (file:///home/me/.cache/run-7/app/dist/assets/index-Ab12.js:120:33)",
      "    at http://localhost:5173/src/main.tsx:4:1",
    ].join("\n");
    target.dispatchEvent(
      new ErrorEvent("error", {
        error,
        message: error.message,
        filename: "file:///home/me/.cache/run-7/app/dist/assets/index-Ab12.js",
        lineno: 120,
        colno: 33,
      }),
    );
    const stack = String(send.mock.calls[1]?.[0].stack);
    expect(stack).not.toContain("/home/me/.cache/run-7");
    expect(stack).toContain("at render (index-Ab12.js:120:33)");
    expect(stack).toContain("http://localhost:5173/src/main.tsx:4:1");
    expect(stack.split("\n").at(-1)).toBe("at index-Ab12.js:120:33");
  });

  it("a throwing bridge never breaks the page", () => {
    const target = new EventTarget();
    installErrorForwarding(
      target,
      () => {
        throw new Error("bridge gone");
      },
      { source: "renderer:main" },
    );
    expect(() => target.dispatchEvent(new ErrorEvent("error", { message: "x" }))).not.toThrow();
  });
});

describe("logs viewer", () => {
  function bridge() {
    return {
      logs: {
        recent: vi.fn(async () => ({
          lines: ["2026-10-21 08:00:00.000 INFO desktop: [boot] ready"],
        })),
        reveal: vi.fn(async () => ({ ok: true as const })),
        rendererError: vi.fn(),
      },
      diagnostics: { copy: vi.fn(async () => ({ copied: true, bytes: 1200 })) },
    };
  }

  it("shows recent lines, reveals the log file and copies diagnostics", async () => {
    const fake = bridge();
    render(
      <I18nProvider>
        <LogsViewer bridge={fake} />
      </I18nProvider>,
    );
    expect(await screen.findByText(/\[boot\] ready/)).toBeTruthy();
    expect(fake.logs.recent).toHaveBeenCalledWith({ limit: 500 });
    fireEvent.click(screen.getByRole("button", { name: "Show log file" }));
    expect(fake.logs.reveal).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toMatch(/Secrets were removed/),
    );
  });

  it("explains itself outside the desktop app", () => {
    render(
      <I18nProvider>
        <LogsViewer bridge={null} />
      </I18nProvider>,
    );
    expect(screen.getByText("The log is available in the desktop app only.")).toBeTruthy();
  });
});
