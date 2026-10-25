import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Pressure, StatusResponse } from "../lib/restTypes.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderApp } from "../testing/renderApp.tsx";
import {
  DISMISSED_KEY,
  forgetRecovered,
  PressureBannerView,
  pickBanner,
  type Trigger,
} from "./PressureBanner.tsx";
import { STATUS_REFRESH_MS } from "./shellStatus.ts";

function status(memory: Pressure, disk: Pressure): StatusResponse {
  return {
    version: "0.1.0",
    daemon: { state: "running" },
    overall: "ok",
    memory: { pressure: memory, available_percent: 7 },
    disk: { pressure: disk, used_percent: 90, free_gb: 1 },
  };
}

function shown(): string | null {
  return screen.queryByTestId("pressure-banner")?.getAttribute("data-trigger") ?? null;
}

describe("banner choice", () => {
  it("picks one banner by priority", () => {
    const table: Array<[Pressure, Pressure, boolean, Trigger | null]> = [
      ["ok", "ok", false, null],
      ["unknown", "unknown", false, null],
      ["unknown", "unknown", true, "oom-restart"],
      ["elevated", "ok", false, "memory-elevated"],
      ["ok", "elevated", false, "disk-elevated"],
      ["elevated", "elevated", false, "disk-elevated"],
      ["elevated", "elevated", true, "oom-restart"],
      ["critical", "elevated", true, "memory-critical"],
      ["critical", "critical", true, "disk-critical"],
      ["ok", "critical", false, "disk-critical"],
    ];
    for (const [memory, disk, oomSuspected, expected] of table) {
      expect(pickBanner({ memory, disk, oomSuspected }, "pid-1", [])).toBe(expected);
    }
  });

  it("skips a dismissed notice of this boot only, and shows the next one", () => {
    const input = {
      memory: "critical" as Pressure,
      disk: "elevated" as Pressure,
      oomSuspected: false,
    };
    expect(pickBanner(input, "pid-1", ["memory-critical:pid-1"])).toBe("disk-elevated");
    expect(pickBanner(input, "pid-2", ["memory-critical:pid-1"])).toBe("memory-critical");
  });

  it("forgets a domain's dismissals once it is ok, not when unknown", () => {
    const keys = ["memory-elevated:pid-1", "disk-elevated:pid-1", "oom-restart:pid-1"];
    expect(forgetRecovered(keys, { memory: "ok", disk: "unknown" })).toEqual([
      "disk-elevated:pid-1",
      "oom-restart:pid-1",
    ]);
    expect(forgetRecovered(keys, { memory: "unknown", disk: "unknown" })).toEqual(keys);
  });
});

describe("banner view", () => {
  it("shows nothing for unknown readings", () => {
    render(
      <PressureBannerView
        status={status("unknown", "unknown")}
        boot="pid-1"
        oomSuspected={false}
      />,
    );
    expect(shown()).toBeNull();
  });

  it("reopens a dismissed elevated banner when it turns critical", () => {
    const view = render(
      <PressureBannerView status={status("elevated", "ok")} boot="pid-1" oomSuspected={false} />,
    );
    expect(shown()).toBe("memory-elevated");
    expect(screen.getByRole("status").textContent).toContain("Memory is getting low (7 % free)");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(shown()).toBeNull();
    expect(JSON.parse(window.sessionStorage.getItem(DISMISSED_KEY) ?? "[]")).toEqual([
      "memory-elevated:pid-1",
    ]);
    view.rerender(
      <PressureBannerView status={status("critical", "ok")} boot="pid-1" oomSuspected={false} />,
    );
    expect(shown()).toBe("memory-critical");
    expect(screen.getByRole("alert").textContent).toContain("almost exhausted");
  });

  it("shows a dismissed banner again after the daemon restarts", () => {
    const view = render(
      <PressureBannerView status={status("ok", "critical")} boot="pid-1" oomSuspected={false} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(shown()).toBeNull();
    // Still dismissed after a remount in the same boot (kept in storage).
    view.unmount();
    const again = render(
      <PressureBannerView status={status("ok", "critical")} boot="pid-1" oomSuspected={false} />,
    );
    expect(shown()).toBeNull();
    again.rerender(
      <PressureBannerView status={status("ok", "critical")} boot="pid-2" oomSuspected={false} />,
    );
    expect(shown()).toBe("disk-critical");
    expect(screen.getByRole("alert").textContent).toContain("1 GB free");
  });

  it("shows the next episode after a recovery", () => {
    const view = render(
      <PressureBannerView status={status("ok", "elevated")} boot="pid-1" oomSuspected={false} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    view.rerender(
      <PressureBannerView status={status("ok", "ok")} boot="pid-1" oomSuspected={false} />,
    );
    view.rerender(
      <PressureBannerView status={status("ok", "elevated")} boot="pid-1" oomSuspected={false} />,
    );
    expect(shown()).toBe("disk-elevated");
  });
});

describe("banner in the shell", () => {
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

  it("suspects an out-of-memory restart when the daemon changes under critical memory", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    mock.state.status = status("critical", "ok");
    renderApp("/sessions");
    await waitFor(() => expect(shown()).toBe("memory-critical"));
    await waitFor(() => expect(mock.callsTo("GET", "/api/daemon").length).toBeGreaterThan(0));

    // The daemon comes back as a new process and memory has recovered.
    mock.state.status = status("ok", "ok");
    mock.state.daemon = { ...mock.state.daemon, pid: 9001 };
    act(() => {
      vi.advanceTimersByTime(STATUS_REFRESH_MS);
    });
    await waitFor(() => expect(shown()).toBe("oom-restart"));
    expect(screen.getByRole("alert").textContent).toContain("may have stopped it");
  });

  it("does not suspect anything for a restart under normal memory", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderApp("/sessions");
    await waitFor(() => expect(mock.callsTo("GET", "/api/daemon").length).toBeGreaterThan(0));
    mock.state.daemon = { ...mock.state.daemon, pid: 9001 };
    act(() => {
      vi.advanceTimersByTime(STATUS_REFRESH_MS);
    });
    await waitFor(() => expect(mock.callsTo("GET", "/api/daemon").length).toBeGreaterThan(1));
    expect(shown()).toBeNull();
  });
});
