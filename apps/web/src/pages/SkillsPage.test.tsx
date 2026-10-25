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
  it("counts enabled skills, groups categories and filters by search", async () => {
    mock.state.skills = [
      { name: "summarise", description: "Short summaries", category: "writing", enabled: true },
      { name: "deploy", description: "Ship it", category: "software-development", enabled: false },
      { name: "notes", description: "Keep notes", category: "", enabled: true },
    ];
    renderPage(<SkillsPage />, "/skills");
    expect(await screen.findByText("2/3 enabled")).toBeTruthy();
    const filters = within(screen.getByRole("navigation", { name: "Skill filters" }));
    fireEvent.click(filters.getByRole("button", { name: /Software Development/ }));
    expect(screen.getByRole("heading", { name: "Software Development" })).toBeTruthy();
    expect(screen.queryByText("summarise")).toBeNull();
    expect(screen.getByText("deploy")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search skills"), { target: { value: "notes" } });
    expect(screen.getByText("1 results")).toBeTruthy();
    expect(screen.getByText("notes")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search skills"), { target: { value: "zzz" } });
    expect(screen.getByText("No skills match")).toBeTruthy();
  });

  it("opens the skill editor with a SKILL.md template", async () => {
    renderPage(<SkillsPage />, "/skills");
    fireEvent.click(await screen.findByRole("button", { name: "New skill" }));
    const dialog = within(await screen.findByRole("dialog", { name: "New skill" }));
    expect((dialog.getByLabelText("SKILL.md") as HTMLTextAreaElement).value).toContain(
      "name: my-skill",
    );
    fireEvent.click(dialog.getByRole("button", { name: "Create skill" }));
    expect((await dialog.findByRole("alert")).textContent).toContain("Name the skill");
  });

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
    fireEvent.click(await screen.findByRole("button", { name: /^Toolsets/ }));
    const tools = await screen.findByRole("list", { name: "web tools" });
    expect(
      within(tools)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["web_search", "fetch"]);
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));
    const drawer = within(await screen.findByRole("dialog", { name: "Web" }));
    expect(drawer.getByRole("link", { name: "Open Keys" }).getAttribute("href")).toBe("/env");
  });

  it("shows verdicts, blocks dangerous installs and installs safe ones", async () => {
    renderPage(<SkillsPage />, "/skills");
    fireEvent.click(await screen.findByRole("button", { name: "Browse hub" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "Browse hub" }));
    fireEvent.change(screen.getByLabelText("Search the skills hub"), { target: { value: "sly" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const row = within(await screen.findByTestId("hub-hub/sly"));
    fireEvent.click(row.getByRole("button", { name: "Install" }));
    expect((await row.findByRole("alert")).textContent).toContain("rated this skill dangerous");
  });
});
