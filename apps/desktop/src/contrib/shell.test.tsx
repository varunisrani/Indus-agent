// @vitest-environment jsdom
// The shell reads the contribution registry (P24-32 wiring, D-16.799): a plugin's route, sidebar
// entry, pane and settings page show in the real shell and leave live when it is turned off.
import type { ClientEvent } from "@indus/client";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { createElement as h, type ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../app/App.tsx";
import { ThemeProvider } from "../app/theme.tsx";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "../chat/transcriptStore.ts";
import type { IndusPluginFile } from "../global.d.ts";
import { paneRegistry } from "../panes/PaneShell.tsx";
import { moduleUrl, PluginLoader } from "./loader.ts";
import { PluginsPage } from "./PluginsPage.tsx";
import { ContributionRegistryProvider } from "./react.tsx";
import { ContributionRegistry } from "./registry.ts";
import { definePlugin, type PluginContext } from "./sdk.ts";
import { mirrorPaneContributions, type PluginPaneContext } from "./shell.tsx";
import { fakeHost } from "./testHost.ts";

function store(): TranscriptStore {
  const source: TranscriptSource = {
    onEvent: (_listener: (event: ClientEvent) => void) => () => undefined,
    request: (async () => ({ messages: [] })) as unknown as TranscriptSource["request"],
    watchSession: async () => undefined,
    unwatchSession: () => undefined,
  };
  return new TranscriptStore(source);
}

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

const where = () => screen.getByTestId("where").textContent;

function mount(path: string, registry: ContributionRegistry, extra?: ReactNode) {
  return render(
    <ThemeProvider>
      <TranscriptStoreProvider store={store()}>
        <ContributionRegistryProvider registry={registry}>
          <MemoryRouter initialEntries={[path]}>
            <AppRoutes />
            <Where />
            {extra}
          </MemoryRouter>
        </ContributionRegistryProvider>
      </TranscriptStoreProvider>
    </ThemeProvider>,
  );
}

const statsFile: IndusPluginFile = {
  id: "stats",
  kind: "disk",
  file: "/home/u/.indus/desktop-plugins/stats.js",
  size: 10,
  hash: "b".repeat(64),
  status: "ready",
};

const statsModule = {
  default: definePlugin({
    id: "stats",
    name: "Stats",
    register(ctx: PluginContext) {
      ctx.register({
        id: "page",
        area: "route",
        path: "/stats/overview",
        render: () => h("h2", null, "Plugin stats"),
      });
      ctx.register({ id: "nav", area: "sidebar-nav", label: "Stats", path: "/stats/overview" });
      ctx.register({
        id: "pane",
        area: "pane",
        title: "Stats pane",
        render: (view: PluginPaneContext) => h("p", null, `pane for ${String(view.profile)}`),
      });
      ctx.register({
        id: "settings",
        area: "plugin-settings-page",
        title: "Stats settings",
        render: () => h("p", null, "stats options"),
      });
    },
  }),
};

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

const stops: Array<() => void> = [];

afterEach(() => {
  for (const stop of stops.splice(0)) stop();
  vi.restoreAllMocks();
});

describe("the shell reads the contribution registry", () => {
  it("a disk plugin's page, sidebar entry, pane and settings show, and leave live when turned off", async () => {
    const registry = new ContributionRegistry();
    stops.push(mirrorPaneContributions(registry, paneRegistry));
    const storage = memoryStorage();
    const loader = new PluginLoader({
      host: fakeHost(),
      registry,
      importModule: async (url) => {
        if (url !== moduleUrl(statsFile)) throw new Error(`no module ${url}`);
        return statsModule;
      },
      storage: () => storage,
    });
    await loader.sync([statsFile]);
    mount(
      "/stats/overview",
      registry,
      <PluginsPage loader={loader} rescan={async () => ({ files: [statsFile] })} />,
    );
    expect(screen.getByRole("heading", { name: "Plugin stats" })).toBeTruthy();
    const statsLink = () =>
      screen.getByRole("navigation", { name: "Main" }).querySelector('a[href="/stats/overview"]');
    expect(statsLink()?.textContent).toBe("Stats");
    expect(paneRegistry.get("stats:pane")?.title).toBe("Stats pane");
    expect(screen.getByRole("button", { name: "Open Stats pane" })).toBeTruthy();
    expect(screen.getByText("stats options")).toBeTruthy();

    // Settings › Advanced › Plugins: the switch turns it off; everything it added leaves at once
    const toggle = screen.getByRole("switch", { name: "Use Stats" }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    await act(async () => {
      fireEvent.click(toggle);
    });
    expect(screen.queryByRole("heading", { name: "Plugin stats" })).toBeNull();
    expect(where()).toBe("/");
    expect(statsLink()).toBeNull();
    expect(paneRegistry.get("stats:pane")).toBeUndefined();
    expect(screen.queryByRole("button", { name: "Open Stats pane" })).toBeNull();
    expect(registry.bySource("plugin:stats")).toEqual([]);
    expect(loader.inventory()[0]?.status).toBe("disabled");
    expect(storage.getItem("indus.desktop.plugins.decisions")).toBe('{"stats":false}');

    // and back on
    await act(async () => {
      fireEvent.click(screen.getByRole("switch", { name: "Use Stats" }));
    });
    expect(statsLink()?.textContent).toBe("Stats");
    expect(paneRegistry.get("stats:pane")).toBeDefined();
  });

  it("a contributed route never shadows a built-in page or overlay", () => {
    const registry = new ContributionRegistry();
    registry.registerMany([
      {
        id: "x:settings",
        area: "route",
        source: "plugin:x",
        path: "/settings",
        render: () => h("h2", null, "fake settings"),
      },
      {
        id: "x:caps",
        area: "route",
        source: "plugin:x",
        path: "/capabilities",
        render: () => h("h2", null, "fake caps"),
      },
    ]);
    mount("/capabilities", registry);
    expect(screen.queryByText("fake caps")).toBeNull();
    expect(screen.getByRole("heading", { name: "Capabilities" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeTruthy();
    expect(screen.queryByText("fake settings")).toBeNull();
  });

  it("the Bots page is served only while its contribution is on", async () => {
    const registry = new ContributionRegistry();
    const off = registry.registerMany([
      {
        id: "bots.route.bots",
        area: "route",
        source: "app",
        path: "/bots",
        render: () => h("h2", null, "Bots here"),
      },
      { id: "bots.nav.bots", area: "sidebar-nav", source: "app", label: "Bots", path: "/bots" },
    ]);
    mount("/bots", registry);
    expect(screen.getByRole("heading", { name: "Bots here" })).toBeTruthy();
    // W52-k: Bots is the sidebar's BOTS tab (the roster in place), not a nav link
    const links = [...screen.getByRole("navigation", { name: "Main" }).querySelectorAll("a")];
    expect(links.filter((a) => a.getAttribute("href") === "/bots")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Bots" })).toBeTruthy();
    // W53: one "Main" nav whichever panel the strip shows
    fireEvent.click(screen.getByRole("button", { name: "Bots" }));
    expect(screen.getAllByRole("navigation", { name: "Main" })).toHaveLength(1);
    await act(async () => off());
    expect(where()).toBe("/");
    expect(screen.queryByRole("button", { name: "Bots" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Bots" })).toBeNull();
  });

  it("an overlay over a contributed page returns to it; a throwing page leaves the shell up", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const registry = new ContributionRegistry();
    registry.registerMany([
      {
        id: "y:ok",
        area: "route",
        source: "plugin:y",
        path: "/y/report",
        render: () => h("h2", null, "Report"),
      },
      {
        id: "y:bad",
        area: "route",
        source: "plugin:y",
        path: "/y/broken",
        render: () => {
          throw new Error("broken plugin page");
        },
      },
    ]);
    mount("/y/report", registry);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(where()).toBe("/settings");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(where()).toBe("/y/report");
    expect(screen.getByRole("heading", { name: "Report" })).toBeTruthy();
    screen.getByRole("main"); // the shell itself
    const second = mount("/y/broken", registry);
    expect(second.getAllByRole("navigation", { name: "Main" }).length).toBeGreaterThan(0);
    expect(warn).toHaveBeenCalled();
  });
});
