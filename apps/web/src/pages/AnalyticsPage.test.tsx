import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { AnalyticsPage } from "./AnalyticsPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.config = { ...mock.state.config, dashboard: { show_token_analytics: true } };
  mock.state.usage = Array.from({ length: 40 }, (_, i) => ({
    day: `2026-09-${String((i % 28) + 1).padStart(2, "0")}-${i}`,
    input_tokens: 100,
    output_tokens: 10,
    cost: 0.5,
    sessions: 1,
  }));
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

const totals = () => within(screen.getByLabelText("Usage totals"));

function figure(label: string): string {
  const term = totals().getByText(label);
  return term.closest("div")?.querySelector("dd")?.textContent ?? "";
}

describe("analytics page", () => {
  it("defaults to 30 days and switches range from the header", async () => {
    renderPage(<AnalyticsPage />, "/analytics");
    const chart = await screen.findByRole("img", { name: "Daily token usage" });
    expect(chart.querySelectorAll("[data-day]")).toHaveLength(30);
    expect(mock.callsTo("GET", "/api/analytics/usage")[0]?.query.get("days")).toBe("30");
    expect(figure("Input")).toBe("3.0K");
    expect(figure("Total tokens")).toBe("3.3K");
    expect(figure("Total sessions")).toBe("30 (~1.0/DAY AVG)");
    expect(figure("Est. cost")).toBe("$15.00");

    const header = within(screen.getByTestId("page-header-after-title"));
    expect(header.getByRole("button", { name: "30d" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(header.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(figure("Input")).toBe("700"));
    expect(mock.callsTo("GET", "/api/analytics/usage").at(-1)?.query.get("days")).toBe("7");
    expect(
      screen.getByRole("img", { name: "Daily token usage" }).querySelectorAll("[data-day]"),
    ).toHaveLength(7);
    expect(
      screen.getByRole("table", { name: "Daily breakdown" }).querySelectorAll("tbody tr"),
    ).toHaveLength(7);
    expect(screen.queryByRole("img", { name: "Cost per day" })).toBeNull();
  });

  it("sorts the daily table by a header and flips the direction", async () => {
    mock.state.usage = [
      { day: "2026-10-20", input_tokens: 50, output_tokens: 5, cost: 0, sessions: 2 },
      { day: "2026-10-21", input_tokens: 900, output_tokens: 9, cost: 0, sessions: 1 },
      { day: "2026-10-22", input_tokens: 300, output_tokens: 3, cost: 0, sessions: 3 },
    ];
    renderPage(<AnalyticsPage />, "/analytics");
    const table = await screen.findByRole("table", { name: "Daily breakdown" });
    const dates = () =>
      [...table.querySelectorAll("tbody tr")].map((tr) => tr.querySelector("td")?.textContent);
    expect(dates()).toEqual(["Oct 22", "Oct 21", "Oct 20"]);
    const input = within(table).getByRole("button", { name: "Input" });
    fireEvent.click(input);
    expect(dates()).toEqual(["Oct 21", "Oct 22", "Oct 20"]);
    expect(input.closest("th")?.getAttribute("aria-sort")).toBe("descending");
    fireEvent.click(input);
    expect(dates()).toEqual(["Oct 20", "Oct 22", "Oct 21"]);
    expect(input.closest("th")?.getAttribute("aria-sort")).toBe("ascending");
  });

  it("says when there is no usage", async () => {
    mock.state.usage = [];
    renderPage(<AnalyticsPage />, "/analytics");
    expect(await screen.findByText("No usage recorded in this range yet.")).toBeTruthy();
  });

  it("explains the flag and fetches nothing while it is off", async () => {
    mock.state.config = { ...mock.state.config, dashboard: { show_token_analytics: false } };
    renderPage(<AnalyticsPage />, "/analytics");
    expect(await screen.findByRole("heading", { name: "Token analytics are off" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Config" }).getAttribute("href")).toBe("/config");
    expect(screen.queryByRole("button", { name: "30d" })).toBeNull();
    expect(mock.callsTo("GET", "/api/analytics/usage")).toHaveLength(0);
  });
});
