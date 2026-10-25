import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { mcpBody, ProfileBuilderPage } from "./ProfileBuilderPage.tsx";
import { profileNameProblem } from "./profiles/profileName.ts";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.profiles = {
    active: "default",
    profiles: [
      { name: "default", is_default: true },
      { name: "work", is_default: false },
    ],
  };
  mock.state.skills = [
    { name: "web-search", description: "", category: "web", enabled: true },
    { name: "notes", description: "", category: "notes", enabled: true },
  ];
  mock.state.hub = [
    {
      identifier: "hub/papers",
      name: "papers",
      description: "Read papers",
      source: "hub",
      verdict: null,
      installed: false,
      update_available: false,
    },
  ];
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

function next(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Next" }) as HTMLButtonElement;
}

async function typeName(value: string) {
  fireEvent.change(screen.getByLabelText("Profile name"), { target: { value } });
}

describe("profile builder", () => {
  it("cannot advance with an invalid name", async () => {
    renderPage(<ProfileBuilderPage />, "/profiles/new");
    await waitFor(() => expect(mock.callsTo("GET", "/api/profiles").length).toBeGreaterThan(0));
    expect(next().disabled).toBe(true);
    for (const bad of ["no spaces", "-dash-first", "default", "x".repeat(65), "a/b", "Work"]) {
      await typeName(bad);
      await waitFor(() => expect(next().disabled).toBe(true));
    }
    expect(screen.getByText(/already exists/)).toBeTruthy();
    await typeName("Research_2");
    await waitFor(() => expect(next().disabled).toBe(false));
    // A later step cannot be reached through the step list either while the name is bad.
    await typeName("bad name");
    const steps = within(screen.getByRole("list", { name: "Steps" }));
    expect((steps.getByRole("button", { name: "5. Review" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((steps.getByRole("button", { name: "1. Identity" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("walks the steps and writes the profile, its model, skills and servers", async () => {
    renderPage(<ProfileBuilderPage />, "/profiles/new");
    await waitFor(() => expect(mock.callsTo("GET", "/api/profiles").length).toBeGreaterThan(0));
    await typeName("research");
    await screen.findByRole("option", { name: "A copy of work's settings" });
    fireEvent.change(screen.getByLabelText("Start from"), { target: { value: "work" } });
    fireEvent.change(screen.getByLabelText("Description (optional)"), {
      target: { value: "Papers and notes" },
    });
    fireEvent.click(next());

    const fake = await screen.findByRole("button", { name: /Fake provider/ });
    fireEvent.click(fake);
    expect(fake.getAttribute("aria-pressed")).toBe("true");
    expect(next().disabled).toBe(true); // a provider needs a model
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "fake-large" } });
    fireEvent.click(next());

    const keepAll = await screen.findByRole("checkbox", {
      name: "Keep every bundled skill (recommended)",
    });
    expect(screen.queryByRole("checkbox", { name: "web-search" })).toBeNull();
    fireEvent.click(keepAll);
    fireEvent.click(await screen.findByRole("checkbox", { name: "web-search" }));
    fireEvent.change(screen.getByLabelText("Add from the skills hub"), {
      target: { value: "pap" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add papers" }));
    expect(
      within(screen.getByRole("list", { name: "Hub skills to add" })).getByText("papers"),
    ).toBeTruthy();
    fireEvent.click(next());

    fireEvent.change(await screen.findByLabelText("Server name"), { target: { value: "papers" } });
    const transport = within(screen.getByRole("group", { name: "Transport" }));
    fireEvent.click(transport.getByRole("button", { name: "stdio" }));
    expect(screen.getByLabelText("Command")).toBeTruthy();
    fireEvent.click(transport.getByRole("button", { name: "HTTP/SSE" }));
    fireEvent.change(screen.getByLabelText("Server name"), { target: { value: "papers" } });
    fireEvent.change(screen.getByLabelText("URL"), {
      target: { value: "https://mcp.example.org/papers" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    expect(
      within(screen.getByRole("list", { name: "Servers to add" })).getByText(
        "https://mcp.example.org/papers",
      ),
    ).toBeTruthy();
    expect(screen.getByText("1 configured")).toBeTruthy();
    fireEvent.click(next());

    expect(screen.getByText("Bundled, without web-search")).toBeTruthy();
    expect(mock.callsTo("POST", "/api/profiles")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));

    const results = within(await screen.findByRole("list", { name: "Create results" }));
    expect(results.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "✓ Profile research",
      "✓ Model fake-large",
      "✓ Skill web-search off",
      "✓ Hub skill papers",
      "✓ MCP server papers",
    ]);
    expect(mock.callsTo("POST", "/api/profiles")[0]?.body).toEqual({
      name: "research",
      clone_from: "work",
      description: "Papers and notes",
    });
    expect(mock.callsTo("PUT", "/api/profiles/research/model")[0]?.body).toEqual({
      provider: "fake",
      model: "fake-large",
    });
    const toggle = mock.callsTo("POST", "/api/skills/toggle")[0];
    expect(toggle?.query.get("profile")).toBe("research");
    expect(toggle?.body).toEqual({ name: "web-search", enabled: false });
    const install = mock.callsTo("POST", "/api/skills/hub/install")[0];
    expect(install?.query.get("profile")).toBe("research");
    expect(install?.body).toEqual({ identifier: "hub/papers" });
    const add = mock.callsTo("POST", "/api/mcp/servers")[0];
    expect(add?.query.get("profile")).toBe("research");
    expect(add?.body).toEqual({ name: "papers", url: "https://mcp.example.org/papers" });
  });

  it("starts without bundled skills and reports a refused create", async () => {
    mock.failNext("POST", "/api/profiles", {
      status: 400,
      body: { detail: { code: "profile_command_failed", message: "disk is read-only" } },
    });
    renderPage(<ProfileBuilderPage />, "/profiles/new");
    await waitFor(() => expect(mock.callsTo("GET", "/api/profiles").length).toBeGreaterThan(0));
    await typeName("lean");
    fireEvent.click(next());
    fireEvent.click(next());
    fireEvent.click(await screen.findByRole("checkbox", { name: "Start with no bundled skills" }));
    fireEvent.click(next());
    fireEvent.click(next());
    expect(screen.getByText("No bundled skills")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
    const results = within(await screen.findByRole("list", { name: "Create results" }));
    expect(results.getByText(/disk is read-only/)).toBeTruthy();
    expect(mock.callsTo("POST", "/api/profiles")[0]?.body).toEqual({
      name: "lean",
      no_skills: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "Change and retry" }));
    expect(await screen.findByRole("button", { name: "Create profile" })).toBeTruthy();
  });

  it("checks names and server drafts like the server does", () => {
    expect(profileNameProblem("  Mine ")).toBeNull();
    expect(profileNameProblem("")).toMatch(/Give/);
    expect(profileNameProblem("tmp")).toMatch(/reserved/);
    expect(profileNameProblem("a".repeat(64))).toBeNull();
    expect(
      mcpBody({ name: "x", transport: "stdio", command: "", args: "", env: "", url: "" }),
    ).toMatch(/command/);
    const http = { name: "x", transport: "http", command: "", args: "", env: "" } as const;
    expect(mcpBody({ ...http, url: "https://h.example/mcp", auth: "oauth" })).toEqual({
      name: "x",
      url: "https://h.example/mcp",
      auth: "oauth",
    });
    expect(mcpBody({ ...http, url: "https://h.example/mcp", auth: "none" })).toEqual({
      name: "x",
      url: "https://h.example/mcp",
    });
    expect(
      mcpBody({
        name: "x",
        transport: "stdio",
        command: "run",
        args: "-v 'a b'",
        env: "K=1",
        url: "",
      }),
    ).toEqual({ name: "x", command: "run", args: ["-v", "a b"], env: { K: "1" } });
  });
});
