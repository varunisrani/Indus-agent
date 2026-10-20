import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { LOG_REFRESH_MS, LogsPage, lineLevel } from "./LogsPage.tsx";

let mock: MockApi;
let restore: () => void;
let hidden = false;

function setHidden(value: boolean) {
  hidden = value;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

beforeEach(() => {
  mock = createMockApi();
  mock.state.logs.agent = [
    "2026-10-20 10:00:00 DEBUG agent: thinking",
    "2026-10-20 10:00:01 INFO agent: turn started",
    "2026-10-20 10:00:02 WARNING tools: slow tool",
    "2026-10-20 10:00:03 ERROR gateway: send failed",
  ];
  restore = mock.install();
  hidden = false;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  restore();
  Reflect.deleteProperty(document, "hidden");
});

const logCalls = () => mock.callsTo("GET", "/api/logs").length;

describe("logs", () => {
  it("refreshes every 5 s, and not while the tab is hidden", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(<LogsPage />, "/logs");
    await screen.findByRole("list", { name: "Log lines" });
    expect(logCalls()).toBe(1);

    act(() => {
      vi.advanceTimersByTime(LOG_REFRESH_MS - 1);
    });
    expect(logCalls()).toBe(1);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    await waitFor(() => expect(logCalls()).toBe(2));
    act(() => {
      vi.advanceTimersByTime(LOG_REFRESH_MS);
    });
    await waitFor(() => expect(logCalls()).toBe(3));

    setHidden(true);
    act(() => {
      vi.advanceTimersByTime(LOG_REFRESH_MS * 4);
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(logCalls()).toBe(3);

    // Back in view: one refresh at once, then the 5 s rhythm again.
    setHidden(false);
    await waitFor(() => expect(logCalls()).toBe(4));
    act(() => {
      vi.advanceTimersByTime(LOG_REFRESH_MS);
    });
    await waitFor(() => expect(logCalls()).toBe(5));
  });

  it("sends the chosen file, level, component and length", async () => {
    renderPage(<LogsPage />, "/logs");
    await screen.findByRole("list", { name: "Log lines" });
    const group = (name: string) => within(screen.getByRole("group", { name }));
    fireEvent.click(group("File").getByRole("button", { name: "gateway" }));
    fireEvent.click(group("Level").getByRole("button", { name: "ERROR" }));
    fireEvent.click(group("Component").getByRole("button", { name: "cron" }));
    fireEvent.click(group("Lines").getByRole("button", { name: "500" }));
    await waitFor(() => {
      const query = mock.callsTo("GET", "/api/logs").at(-1)?.query;
      expect(query?.get("file")).toBe("gateway");
      expect(query?.get("level")).toBe("ERROR");
      expect(query?.get("component")).toBe("cron");
      expect(query?.get("lines")).toBe("500");
    });
    // "everything" sends no filter at all.
    const first = mock.callsTo("GET", "/api/logs")[0]?.query;
    expect(first?.has("level")).toBe(false);
    expect(first?.has("component")).toBe(false);
  });

  it("opens the file named in the URL", async () => {
    renderPage(<LogsPage />, "/logs?file=errors");
    await waitFor(() =>
      expect(mock.callsTo("GET", "/api/logs")[0]?.query.get("file")).toBe("errors"),
    );
  });

  it("colours rows by severity", async () => {
    renderPage(<LogsPage />, "/logs");
    const list = await screen.findByRole("list", { name: "Log lines" });
    const levels = [...list.querySelectorAll("li")].map((li) => li.getAttribute("data-level"));
    expect(levels).toEqual(["debug", "info", "warning", "error"]);
    expect(lineLevel("plain text")).toBe("plain");
    expect(lineLevel("x CRITICAL y")).toBe("error");
  });
});
