import { act, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type PageRoute, ROUTES } from "../shell/routes.ts";
import {
  exposePluginSdk,
  FIRST_RENDER_WAIT_MS,
  loadPlugins,
  mergePluginRoutes,
  type PluginManifest,
  PluginRegistry,
  pluginAssetUrl,
} from "./registry.ts";
import { buildDashboardSdk, PLUGIN_SOCKET_PURPOSE } from "./sdk.ts";
import { KNOWN_SLOTS, PluginPage, PluginSlot } from "./slots.tsx";

function manifest(name: string, extra: Partial<PluginManifest> = {}): PluginManifest {
  return {
    name,
    label: name.toUpperCase(),
    tab: { path: `/${name}` },
    entry: "index.js",
    ...extra,
  };
}

const Hello = () => createElement("p", null, "hello page");
const Other = () => createElement("p", null, "other page");

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  for (const el of document.querySelectorAll("[data-indus-plugin]")) el.remove();
  delete window.__INDUS_PLUGINS__;
  delete window.__INDUS_DASHBOARD_SDK__;
});

describe("registry", () => {
  it("register / registerSlot notify subscribers; a throwing subscriber does not stop others", () => {
    const registry = new PluginRegistry();
    const seen: number[] = [];
    registry.subscribe(() => {
      throw new Error("bad listener");
    });
    registry.subscribe(() => seen.push(registry.snapshot()));
    registry.register("hello", Hello);
    registry.registerSlot("hello", "sidebar", Hello);
    registry.registerSlot("hello", "sidebar", Other); // same (plugin, slot): replaced
    expect(seen).toEqual([1, 2, 3]);
    expect(registry.component("hello")).toBe(Hello);
    expect(registry.slotEntries("sidebar").map((e) => e.component)).toEqual([Other]);
  });

  it("exposes the host API and the SDK on window", () => {
    const registry = new PluginRegistry();
    const sdk = buildDashboardSdk();
    exposePluginSdk(sdk, registry);
    expect(window.__INDUS_DASHBOARD_SDK__).toBe(sdk);
    window.__INDUS_PLUGINS__?.register("hello", Hello);
    expect(registry.component("hello")).toBe(Hello);
    expect(sdk.version).toBe("1.0.0");
    expect(typeof sdk.React.createElement).toBe("function");
    expect(Object.isFrozen(sdk)).toBe(true);
  });
});

describe("loadPlugins", () => {
  it("injects CSS then the script, with integrity and crossorigin for an SRI manifest", async () => {
    const registry = new PluginRegistry();
    const wanted = manifest("hello", {
      css: "dist/style.css",
      entry: "dist/index.js",
      integrity: "sha384-abc",
    });
    vi.useFakeTimers();
    const done = loadPlugins([wanted], { registry });
    const link = document.querySelector('link[data-indus-plugin="hello"]') as HTMLLinkElement;
    const script = document.querySelector('script[data-indus-plugin="hello"]') as HTMLScriptElement;
    expect(link.href).toContain("/dashboard-plugins/hello/dist/style.css");
    expect(script.src).toContain("/dashboard-plugins/hello/dist/index.js");
    expect(script.integrity).toBe("sha384-abc");
    expect(script.crossOrigin).toBe("anonymous");
    registry.register("hello", Hello);
    await done; // every plugin registered: no need to wait the 2 s
  });

  it("an SRI mismatch (script error) blocks the plugin and only warns", async () => {
    const registry = new PluginRegistry();
    const warn = vi.fn();
    const done = loadPlugins([manifest("hello", { integrity: "sha384-wrong" })], {
      registry,
      warn,
    });
    const script = document.querySelector('script[data-indus-plugin="hello"]') as HTMLScriptElement;
    script.dispatchEvent(new Event("error"));
    await done;
    expect(registry.component("hello")).toBeUndefined();
    expect(registry.loadError("hello")).toBe("LOAD_FAILED");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("integrity"));
  });

  it("stops waiting after 2 s; a late registration still renders", async () => {
    vi.useFakeTimers();
    const registry = new PluginRegistry();
    let settled = false;
    const done = loadPlugins([manifest("late")], { registry }).then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(FIRST_RENDER_WAIT_MS - 1);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await done;
    expect(settled).toBe(true);
    vi.useRealTimers();
    const late = manifest("late");
    render(createElement(PluginPage, { manifest: late, registry }));
    expect(screen.getByText(/has not registered/)).toBeTruthy();
    act(() => registry.register("late", Hello));
    expect(screen.getByText("hello page")).toBeTruthy();
  });

  it("resolves at once with nothing to load", async () => {
    await loadPlugins([], { registry: new PluginRegistry() });
  });

  it("asset URLs encode every segment", () => {
    expect(pluginAssetUrl("hello", "dist/a b.js")).toBe("/dashboard-plugins/hello/dist/a%20b.js");
  });
});

describe("mergePluginRoutes", () => {
  const lookup = (name: string) => (name === "missing" ? undefined : Hello);

  it("refuses an override of /plugins and keeps the page", () => {
    const routes = mergePluginRoutes(
      ROUTES,
      [manifest("takeover", { tab: { path: "/takeover", override: "/plugins" } })],
      lookup,
    );
    expect(routes.find((r) => r.path === "/plugins")?.page).not.toBe(Hello);
    expect(routes.find((r) => r.path === "/takeover")).toBeUndefined();
  });

  it("an override replaces a built-in page's component", () => {
    const routes = mergePluginRoutes(
      ROUTES,
      [manifest("notes", { tab: { path: "/notes", override: "/logs" } })],
      lookup,
    );
    const logs = routes.find((r) => r.path === "/logs") as PageRoute;
    expect(logs.page).toBe(Hello);
    expect(logs.title).toBe("Logs");
    expect(routes).toHaveLength(ROUTES.length);
  });

  it("inserts tabs by position; unknown targets go to the end; hidden and unregistered add none", () => {
    const routes = mergePluginRoutes(
      ROUTES,
      [
        manifest("after", { tab: { path: "/after", position: "after:skills" } }),
        manifest("before", { tab: { path: "/before", position: "before:sessions" } }),
        manifest("tail", { tab: { path: "/tail", position: "after:nowhere" } }),
        manifest("ghost", { tab: { path: "/ghost", hidden: true } }),
        manifest("missing"),
        manifest("clash", { tab: { path: "/logs" } }),
      ],
      lookup,
    );
    const paths = routes.map((r) => r.path);
    expect(paths[paths.indexOf("/skills") + 1]).toBe("/after");
    expect(paths[paths.indexOf("/sessions") - 1]).toBe("/before");
    expect(paths.at(-1)).toBe("/tail");
    expect(paths).not.toContain("/ghost");
    expect(paths).not.toContain("/missing");
    expect(paths.filter((p) => p === "/logs")).toHaveLength(1);
    expect(routes.find((r) => r.path === "/after")?.title).toBe("AFTER");
  });
});

describe("slots", () => {
  it("render in registration order and re-render on a late registration", () => {
    const registry = new PluginRegistry();
    render(
      createElement(PluginSlot, {
        name: "sessions:top",
        registry,
        fallback: createElement("span", null, "empty"),
      }),
    );
    expect(screen.getByText("empty")).toBeTruthy();
    act(() => registry.registerSlot("a", "sessions:top", Hello));
    act(() => registry.registerSlot("b", "sessions:top", Other));
    const texts = [...document.querySelectorAll("p")].map((p) => p.textContent);
    expect(texts).toEqual(["hello page", "other page"]);
  });

  it("a plugin that throws while rendering hides itself, not the page", () => {
    const registry = new PluginRegistry();
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const warned = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const Broken = () => {
      throw new Error("boom");
    };
    registry.registerSlot("bad", "sidebar", Broken);
    registry.registerSlot("good", "sidebar", Hello);
    render(createElement(PluginSlot, { name: "sidebar", registry }));
    expect(screen.getByText("hello page")).toBeTruthy();
    quiet.mockRestore();
    warned.mockRestore();
  });

  it("lists the shell slots and page top/bottom slots", () => {
    expect(KNOWN_SLOTS).toContain("header-banner");
    expect(KNOWN_SLOTS).toContain("overlay");
    expect(KNOWN_SLOTS).toContain("cron:bottom");
    expect(KNOWN_SLOTS).toContain("chat:top");
  });
});

describe("sdk sockets", () => {
  it("go to the plugin's namespace with the credential as a subprotocol, never in the URL", async () => {
    const made: { url: string; protocols: string[] }[] = [];
    class RecordingSocket {
      constructor(url: string, protocols: string[]) {
        made.push({ url, protocols });
      }
    }
    vi.stubGlobal("WebSocket", RecordingSocket);
    const token = ["plug", "in-", "token-for-tests"].join("");
    window.__INDUS_SESSION_TOKEN__ = token;
    await buildDashboardSdk().openSocket("hello", "/live", { room: "1" });
    expect(made[0]?.url).toMatch(/\/api\/ext\/hello\/live\?room=1$/);
    expect(made[0]?.url).not.toContain(token);
    expect(made[0]?.protocols).toEqual(["indus-dashboard-v1", `indus-dashboard-token.${token}`]);
  });

  it("asks the ticket route for a plugin-socket ticket in gated mode", async () => {
    window.__INDUS_AUTH_REQUIRED__ = true;
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ticket: "t".repeat(43) })));
    vi.stubGlobal("fetch", fetchMock);
    const made: string[][] = [];
    vi.stubGlobal(
      "WebSocket",
      class {
        constructor(_url: string, protocols: string[]) {
          made.push(protocols);
        }
      },
    );
    await buildDashboardSdk().openSocket("hello", "live");
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({ purpose: PLUGIN_SOCKET_PURPOSE });
    expect(made[0]).toEqual(["indus-dashboard-v1", `indus-dashboard-ticket.${"t".repeat(43)}`]);
  });
});
