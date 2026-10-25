import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { installProblem, PluginsPage } from "./PluginsPage.tsx";

let mock: MockApi;
let restore: () => void;
const fullSha = () => "0123456789abcdef".repeat(3).slice(0, 40);

beforeEach(() => {
  mock = createMockApi();
  mock.state.plugins = [
    {
      name: "weather",
      version: "1.2.0",
      description: "Forecasts",
      enabled: false,
      source: "https://example.org/w.git",
      sha: fullSha(),
    },
  ];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

describe("plugins page", () => {
  it("refuses a short SHA without sending anything", async () => {
    renderPage(<PluginsPage />, "/plugins");
    await screen.findByText("weather");
    fireEvent.change(screen.getByLabelText("Git URL or owner/repo"), {
      target: { value: "https://example.org/team/p.git" },
    });
    fireEvent.change(screen.getByLabelText("Commit SHA"), { target: { value: "0123abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect((await screen.findByRole("alert")).textContent).toContain("40-character");
    expect(mock.callsTo("POST", "/api/plugins/install")).toHaveLength(0);
  });

  it("installs with a full SHA", async () => {
    renderPage(<PluginsPage />, "/plugins");
    await screen.findByText("weather");
    fireEvent.change(screen.getByLabelText("Git URL or owner/repo"), {
      target: { value: " https://example.org/team/p.git " },
    });
    fireEvent.change(screen.getByLabelText("Commit SHA"), {
      target: { value: fullSha().toUpperCase() },
    });
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/plugins/install")[0]?.body).toEqual({
        url: "https://example.org/team/p.git",
        sha: fullSha(),
        force: false,
      }),
    );
  });

  it("enables an installed plugin", async () => {
    renderPage(<PluginsPage />, "/plugins");
    const card = within(await screen.findByTestId("plugin-weather"));
    expect(card.getByText("disabled")).toBeTruthy();
    fireEvent.click(card.getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/plugins/weather/enable")).toHaveLength(1),
    );
    await waitFor(() => expect(card.getByText("enabled")).toBeTruthy());
  });

  it("saves the memory provider and the context engine", async () => {
    mock.state.memory = { active: "", providers: ["vectors"], builtin_files: { "MEMORY.md": 1 } };
    mock.state.config = { context: { engine: "builtin", keep: 3 } };
    renderPage(<PluginsPage />, "/plugins");
    const memory = (await screen.findByLabelText("Memory provider")) as HTMLSelectElement;
    await waitFor(() => expect(memory.disabled).toBe(false));
    expect(screen.getByText("active")).toBeTruthy();
    fireEvent.change(memory, { target: { value: "vectors" } });
    expect(screen.queryByText("active")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save memory provider" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/memory/provider")[0]?.body).toEqual({ provider: "vectors" }),
    );
    await waitFor(() =>
      expect((screen.getByLabelText("Context engine") as HTMLSelectElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save context engine" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/config")[0]?.body).toEqual({
        config: { context: { engine: "builtin", keep: 3 } },
      }),
    );
  });

  it("checks the URL shape too", () => {
    expect(installProblem("ftp://x", fullSha())).toMatch(/git URL/);
    expect(installProblem("git@example.org:t/p.git", fullSha())).toBeNull();
    expect(installProblem("team/plugin", fullSha())).toBeNull();
    expect(installProblem("team/plugins/weather", fullSha())).toBeNull();
    expect(installProblem("justaname", fullSha())).toMatch(/owner\/repo/);
    expect(installProblem("https://example.org/p.git", `${fullSha()}0`)).toMatch(/40-character/);
  });
});
