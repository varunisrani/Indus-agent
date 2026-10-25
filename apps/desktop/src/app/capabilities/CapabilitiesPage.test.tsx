// @vitest-environment jsdom
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../../i18n/index.tsx";
import { fakeRest } from "../../settings/fakeRest.ts";
import { CapabilitiesPage, CapabilitiesRoute } from "./CapabilitiesPage.tsx";
import { type DesktopPluginRow, type DesktopPluginSource, mergePlugins } from "./tabs.tsx";

// The Desktop column's source (in the app: `contrib/host.ts` over the plugin loader, P24-32).
class StubDesktopPlugins implements DesktopPluginSource {
  calls = 0;
  private readonly rows: DesktopPluginRow[];

  constructor(rows: DesktopPluginRow[]) {
    this.rows = rows;
  }

  async list(): Promise<DesktopPluginRow[]> {
    this.calls += 1;
    return this.rows.map((row) => ({ ...row }));
  }
}

const SHA = "a".repeat(40);

function backend() {
  const api = fakeRest();
  api.route("GET", "/api/profiles", {
    profiles: [{ name: "default" }, { name: "work" }],
    active: "default",
  });
  api.route("GET", "/api/skills", (request) => ({
    status: 200,
    body: {
      skills: [
        {
          name: request.profile === "work" ? "work-notes" : "brew-tea",
          description: "about",
          category: "",
          enabled: true,
          source: "hub",
        },
      ],
    },
  }));
  api.route("GET", "/api/tools/toolsets", {
    toolsets: [
      { name: "web", label: "Web", description: "", enabled: true, tools: ["web_search"] },
    ],
  });
  api.route("GET", "/api/mcp/servers", { servers: [] });
  api.route("GET", "/api/plugins", {
    plugins: [
      { name: "weather", version: "1.0.0", description: "", enabled: true, sha: SHA },
      { name: "tides", version: "0.1.0", description: "", enabled: false, sha: null },
    ],
  });
  api.route("POST", "/api/skills/hub/install", { ok: true, name: "zen" });
  return api;
}

const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
};

function wrap(node: ReactNode) {
  return render(<I18nProvider>{node}</I18nProvider>);
}

describe("capabilities page", () => {
  it("a scope switch reloads the profile-scoped tabs only", async () => {
    const api = backend();
    const desktop = new StubDesktopPlugins([{ id: "d1", name: "weather", enabled: true }]);
    wrap(<CapabilitiesPage api={api} desktopPlugins={desktop} profile="default" />);
    await flush();
    expect(screen.getAllByText("brew-tea").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("tab", { name: /Plugins/ }));
    await flush();
    const pluginCalls = () => api.calls.filter((c) => c.path === "/api/plugins");
    expect(pluginCalls().map((c) => c.profile)).toEqual(["default"]);
    expect(desktop.calls).toBe(1);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "work" } });
    await flush();
    expect(pluginCalls().map((c) => c.profile)).toEqual(["default", "work"]);
    expect(desktop.calls).toBe(1); // app-level: never re-read on a switch
    fireEvent.click(screen.getByRole("tab", { name: /Skills/ }));
    await flush();
    expect(screen.getAllByText("work-notes").length).toBeGreaterThan(0);
    const skillProfiles = api.calls.filter((c) => c.path === "/api/skills").map((c) => c.profile);
    expect(skillProfiles).toEqual(["default", "work"]);
    expect(api.calls.filter((c) => c.path === "/api/profiles")).toHaveLength(1);
  });

  it("an install deep link shows the confirmation before any call", async () => {
    const api = backend();
    wrap(
      <MemoryRouter initialEntries={["/capabilities?tab=skills&install=hub%2Fzen"]}>
        <CapabilitiesRoute api={api} profile="default" />
      </MemoryRouter>,
    );
    await flush();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/hub\/zen/)).toBeTruthy();
    expect(api.calls.filter((c) => c.method === "POST")).toEqual([]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.calls.filter((c) => c.method === "POST")).toEqual([]);
  });

  it("confirming installs once into the chosen profile", async () => {
    const api = backend();
    wrap(<CapabilitiesPage api={api} profile="default" pendingInstall="hub/zen" />);
    await flush();
    const button = within(screen.getByRole("dialog")).getByRole("button", {
      name: "Install skill",
    });
    fireEvent.click(button);
    fireEvent.click(button);
    await flush();
    const posts = api.calls.filter((c) => c.method === "POST");
    expect(posts).toEqual([
      expect.objectContaining({
        path: "/api/skills/hub/install",
        body: { identifier: "hub/zen" },
        profile: "default",
      }),
    ]);
    expect(screen.getByRole("status").textContent).toContain("zen was installed");
  });

  it("plugin rows badge agent, desktop and both from fixture data", async () => {
    const api = backend();
    const desktop = new StubDesktopPlugins([
      { id: "d1", name: "weather", version: "2.0.0", enabled: true },
      { id: "d2", name: "canvas", enabled: true },
    ]);
    wrap(
      <CapabilitiesPage
        api={api}
        desktopPlugins={desktop}
        profile="default"
        initialTab="plugins"
      />,
    );
    await flush();
    const badge = (name: string) =>
      screen
        .getByTestId(`plugin-${name}`)
        .querySelector("[data-badge]")
        ?.getAttribute("data-badge");
    expect(badge("weather")).toBe("both");
    expect(badge("tides")).toBe("agent");
    expect(badge("canvas")).toBe("desktop");
  });

  it("a plugin install needs a full commit pin", async () => {
    const api = backend();
    api.route("POST", "/api/plugins/install", { ok: true, name: "x", sha: SHA });
    wrap(<CapabilitiesPage api={api} profile="default" initialTab="plugins" />);
    await flush();
    // the form lives in the "Install from Git" dialog (W52-k)
    fireEvent.click(screen.getByRole("button", { name: "Install from Git" }));
    fireEvent.change(screen.getByRole("textbox", { name: /Repository/ }), {
      target: { value: "acme/tool" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Commit/ }), {
      target: { value: "abc123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    await flush();
    expect(screen.getByRole("alert").textContent).toContain("40-character");
    expect(api.calls.some((c) => c.path === "/api/plugins/install")).toBe(false);
    fireEvent.change(screen.getByRole("textbox", { name: /Commit/ }), { target: { value: SHA } });
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    await flush();
    expect(api.calls.find((c) => c.path === "/api/plugins/install")?.body).toEqual({
      url: "acme/tool",
      sha: SHA,
    });
  });

  it("toggles and counts", async () => {
    const api = backend();
    api.route("POST", "/api/skills/toggle", { ok: true });
    const summary = vi.fn(async () => ({
      skills: { total: 3, enabled: 2 },
      toolsets: { total: 9, enabled: 4 },
      mcp_servers: { total: 0, enabled: 0 },
      plugins: { total: 2, enabled: 1 },
    }));
    wrap(<CapabilitiesPage api={api} profile="default" summary={summary} />);
    await flush();
    // W52-k: one number per tab, the total, as a superscript after the label
    expect(screen.getByRole("tab", { name: /Skills/ }).querySelector("sup")?.textContent).toBe("3");
    expect(screen.getByRole("tab", { name: /Tools/ }).querySelector("sup")?.textContent).toBe("9");
    expect(screen.getByRole("tab", { name: /Connectors/ }).querySelector("sup")).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "brew-tea is on" }));
    await flush();
    expect(api.calls.find((c) => c.path === "/api/skills/toggle")?.body).toEqual({
      name: "brew-tea",
      enabled: false,
    });
  });

  it("mergePlugins keeps one row per name", () => {
    const rows = mergePlugins(
      [{ name: "b", version: null, description: "", enabled: true, sha: null }],
      [
        { id: "1", name: "a", enabled: true },
        { id: "2", name: "b", enabled: false },
      ],
    );
    expect(rows.map((r) => [r.name, r.badge])).toEqual([
      ["a", "desktop"],
      ["b", "both"],
    ]);
  });

  it("joins usage and the learned origin, archives after a confirm, reverts a failed toggle", async () => {
    const api = backend();
    api.route("GET", "/api/learning/graph", {
      nodes: [{ id: "skill:brew-tea", useCount: 4, createdBy: "agent" }],
      edges: [],
    });
    api.route("POST", "/api/skills/toggle", () => ({ status: 500, body: { detail: "nope" } }));
    api.route("DELETE", "/api/learning/nodes/skill%3Abrew-tea", { ok: true, message: "archived" });
    wrap(<CapabilitiesPage api={api} profile="default" />);
    await flush();
    expect(screen.getByText("\u00d74")).toBeTruthy();
    expect(screen.getAllByText("learned").length).toBeGreaterThan(0);
    const toggle = screen.getByRole("switch", { name: "brew-tea is on" }) as HTMLInputElement;
    fireEvent.click(toggle);
    await flush();
    expect(toggle.checked).toBe(true); // the failed write flipped it back
    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Archive" }),
    );
    await flush();
    expect(api.calls.find((c) => c.method === "DELETE")?.path).toBe(
      "/api/learning/nodes/skill%3Abrew-tea",
    );
  });

  it("a connector card opens its dialog; Test reports the tools", async () => {
    const api = backend();
    api.route("GET", "/api/mcp/servers", {
      servers: [{ name: "files", enabled: true, command: "mcp-files" }],
    });
    api.route("POST", "/api/mcp/servers/files/test", { tools: [{}, {}] });
    wrap(<CapabilitiesPage api={api} profile="default" initialTab="connectors" />);
    await flush();
    expect(screen.getByText("Local")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Details: files" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Test" }));
    await flush();
    expect(within(screen.getByRole("dialog")).getByRole("status").textContent).toBe(
      "2 tools answer.",
    );
  });
});
