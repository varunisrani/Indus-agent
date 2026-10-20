import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { SkillsPage } from "./SkillsPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.skills = [
    { name: "summarise", description: "Short summaries", category: "writing", enabled: true },
  ];
  mock.state.toolsets = [
    {
      name: "web",
      label: "Web",
      description: "Search and fetch",
      enabled: true,
      tools: ["web_search", "fetch"],
    },
  ];
  mock.state.hub = [
    {
      identifier: "hub/tidy",
      name: "tidy",
      description: "Tidy notes",
      source: "hub",
      verdict: "safe",
      installed: false,
      update_available: false,
    },
    {
      identifier: "hub/wiper",
      name: "wiper",
      description: "Wipes things",
      source: "hub",
      verdict: "dangerous",
      installed: false,
      update_available: false,
    },
    {
      identifier: "hub/old",
      name: "oldskill",
      description: "Old",
      source: "hub",
      verdict: "caution",
      installed: true,
      update_available: true,
    },
  ];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

describe("skills page", () => {
  it("toggles an installed skill", async () => {
    renderPage(<SkillsPage />, "/skills");
    fireEvent.click(await screen.findByRole("switch", { name: "Enable summarise" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/skills/toggle")[0]?.body).toEqual({
        name: "summarise",
        enabled: false,
      }),
    );
  });

  it("lists toolsets with their tools", async () => {
    renderPage(<SkillsPage />, "/skills");
    fireEvent.click(await screen.findByRole("button", { name: "Toolsets" }));
    expect(await screen.findByText("web_search, fetch")).toBeTruthy();
  });

  it("shows verdicts, blocks dangerous installs and installs safe ones", async () => {
    renderPage(<SkillsPage />, "/skills");
    fireEvent.click(await screen.findByRole("button", { name: "Hub" }));
    fireEvent.change(screen.getByLabelText("Search the skills hub"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Search the skills hub"), { target: { value: "i" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    const wiper = within(await screen.findByTestId("hub-hub/wiper"));
    expect(wiper.getByText("scan: dangerous")).toBeTruthy();
    expect(
      (wiper.getByRole("button", { name: "Blocked by the scanner" }) as HTMLButtonElement).disabled,
    ).toBe(true);

    const tidy = within(screen.getByTestId("hub-hub/tidy"));
    fireEvent.click(tidy.getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/skills/hub/install")[0]?.body).toEqual({
        identifier: "hub/tidy",
      }),
    );

    const old = within(screen.getByTestId("hub-hub/old"));
    expect(old.getByText("scan: caution")).toBeTruthy();
    fireEvent.click(old.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(mock.callsTo("POST", "/api/skills/hub/update")).toHaveLength(1));
  });

  it("shows the server's verdict when it refuses an install", async () => {
    mock.state.hub = [
      {
        identifier: "hub/sly",
        name: "sly",
        description: "Looks fine",
        source: "hub",
        verdict: "safe",
        installed: false,
        update_available: false,
      },
    ];
    mock.failNext("POST", "/api/skills/hub/install", {
      status: 409,
      body: {
        detail: {
          code: "scan_blocked",
          message: "The scanner rated this skill dangerous.",
          verdict: "dangerous",
        },
      },
    });
    renderPage(<SkillsPage />, "/skills");
    fireEvent.click(await screen.findByRole("button", { name: "Hub" }));
    fireEvent.change(screen.getByLabelText("Search the skills hub"), { target: { value: "sly" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const row = within(await screen.findByTestId("hub-hub/sly"));
    fireEvent.click(row.getByRole("button", { name: "Install" }));
    expect((await row.findByRole("alert")).textContent).toContain("rated this skill dangerous");
  });
});
