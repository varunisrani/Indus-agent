import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { AnalyticsPage } from "./AnalyticsPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
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

describe("analytics page", () => {
  it("defaults to 30 days and switches range", async () => {
    renderPage(<AnalyticsPage />, "/analytics");
    expect(await screen.findByRole("img", { name: "Tokens per day" })).toBeTruthy();
    expect(mock.callsTo("GET", "/api/analytics/usage")[0]?.query.get("days")).toBe("30");
    expect(screen.getByText("3,000")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() => expect(screen.getByText("700")).toBeTruthy());
    expect(mock.callsTo("GET", "/api/analytics/usage").at(-1)?.query.get("days")).toBe("7");
    expect(screen.getByRole("img", { name: "Cost per day" }).querySelectorAll("rect")).toHaveLength(
      7,
    );
  });

  it("says when there is no usage", async () => {
    mock.state.usage = [];
    renderPage(<AnalyticsPage />, "/analytics");
    expect(await screen.findByText("No usage recorded in this range yet.")).toBeTruthy();
  });
});
