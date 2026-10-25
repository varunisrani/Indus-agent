import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderApp } from "../testing/renderApp.tsx";
import { STATUS_REFRESH_MS } from "./shellStatus.ts";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
  vi.useRealTimers();
});

function strip() {
  return within(screen.getByLabelText("Status"));
}

describe("status strip", () => {
  it("shows the daemon, sessions, channels and version", async () => {
    mock.state.status = {
      ...mock.state.status,
      active_sessions: 2,
      components: {
        store: { status: "ok" },
        daemon: { status: "ok", state: "running" },
        platforms: { status: "degraded", configured: 3, connected: 2 },
      },
    };
    renderApp("/sessions");
    await waitFor(() =>
      expect(strip().getByTestId("status-daemon").textContent).toBe("Daemon status: Running"),
    );
    expect(strip().getByTestId("status-sessions").textContent).toBe("Active sessions: 2");
    expect(strip().getByTestId("status-platforms").textContent).toBe("Channels: 2/3 connected");
    expect(screen.getByTestId("sidebar-version").textContent).toBe("v0.1.0Indus Agent");
  });

  it("refreshes every 5 s", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderApp("/sessions");
    await waitFor(() =>
      expect(strip().getByTestId("status-daemon").textContent).toBe("Daemon status: Running"),
    );
    const first = mock.callsTo("GET", "/api/status").length;

    mock.state.status = { ...mock.state.status, daemon: { state: "startup_failed" } };
    act(() => {
      vi.advanceTimersByTime(STATUS_REFRESH_MS - 1);
    });
    expect(mock.callsTo("GET", "/api/status")).toHaveLength(first);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    await waitFor(() =>
      expect(strip().getByTestId("status-daemon").textContent).toBe(
        "Daemon status: Failed to start",
      ),
    );
    expect(mock.callsTo("GET", "/api/status")).toHaveLength(first + 1);
  });

  it("says so when the probe cannot be read", async () => {
    mock.failNext("GET", "/api/status", { status: 500, body: { detail: "broken" } });
    renderApp("/sessions");
    await waitFor(() =>
      expect(screen.getByLabelText("Status").textContent).toBe("Status unavailable"),
    );
  });
});
